"""The agent's tool set — what the model is actually allowed to do in a repo.

Every tool is a `Tool`: an OpenAI-shaped JSON schema (what the model sees) plus a Python
implementation (what actually runs), and two policy flags:

  • `mutating`     — changes files, so the verification layer must snapshot/check it.
  • `needs_confirm`— asks the user before running (shell commands), unless auto-approved.

Safety is enforced HERE, not in the prompt — a model (or a prompt-injected file) must not
be able to reach outside the repo:
  • every path goes through `deliver.resolve_repo_path` (traversal-proof, repo-scoped),
  • `run_cmd` is checked against a denylist and an allowlist before it can run,
  • reads are size-capped so one huge file can't blow up the context.

Implementations return a plain string: exactly what the model sees as the tool result.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

try:
    from . import deliver
except ImportError:
    import deliver

# Directories never walked/searched — noise that would drown the repo map.
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".aider-venv",
              "dist", "build", "out", "target", ".pytest_cache", ".mypy_cache",
              ".ruff_cache", "Binaries", "Intermediate", ".next", ".idea", ".vscode"}


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    run: Callable[..., str]
    mutating: bool = False
    needs_confirm: bool = False

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": self.parameters}}


@dataclass
class ToolContext:
    """Everything a tool needs at call time. One per agent session."""
    repo: str
    cfg: dict[str, Any]
    test_cmd: str = ""
    max_read_kb: int = 48
    max_output_chars: int = 12000
    confirm: Callable[[str, str], bool] | None = None   # (tool_name, detail) -> allowed

    def chat_cfg(self) -> dict[str, Any]:
        return self.cfg.get("agent_chat", {})


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… (truncated at {limit} chars)"


def _iter_files(root: Path, max_files: int) -> list[str]:
    out: list[str] = []
    for p in sorted(root.rglob("*")):
        if len(out) >= max_files:
            break
        if p.is_dir() or any(part in _SKIP_DIRS for part in p.parts):
            continue
        out.append(str(p.relative_to(root)).replace("\\", "/"))
    return out


# --- implementations --------------------------------------------------------
def _list_files(ctx: ToolContext, dir: str = "", pattern: str = "") -> str:
    root = deliver.resolve_repo_path(ctx.repo, dir or ".")
    if not root.is_dir():
        return f"ERROR: not a directory: {dir!r}"
    files = _iter_files(root, int(ctx.chat_cfg().get("repo_map_max_files", 400)))
    if pattern:
        files = [f for f in files if fnmatch.fnmatch(f, pattern)]
    if not files:
        return "(no files)"
    return _truncate("\n".join(files), ctx.max_output_chars)


def _read_file(ctx: ToolContext, path: str, start: int = 0, end: int = 0) -> str:
    p = deliver.resolve_repo_path(ctx.repo, path)
    if not p.is_file():
        return f"ERROR: no such file: {path!r}"
    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    if start or end:
        s = max(1, int(start or 1))
        e = int(end) if end else len(lines)
        lines = lines[s - 1:e]
        numbered = [f"{s + i}\t{ln}" for i, ln in enumerate(lines)]
    else:
        numbered = [f"{i + 1}\t{ln}" for i, ln in enumerate(lines)]
    return _truncate("\n".join(numbered), ctx.max_read_kb * 1024)


def _search(ctx: ToolContext, pattern: str, glob: str = "", max_results: int = 60) -> str:
    root = Path(ctx.repo).resolve()
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return f"ERROR: bad regex: {exc}"
    hits: list[str] = []
    for rel in _iter_files(root, 5000):
        if glob and not fnmatch.fnmatch(rel, glob):
            continue
        fp = root / rel
        try:
            if fp.stat().st_size > 2_000_000:
                continue
            for i, line in enumerate(
                    fp.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                    if len(hits) >= max_results:
                        return _truncate("\n".join(hits) + "\n… (more matches)",
                                         ctx.max_output_chars)
        except OSError:
            continue
    return _truncate("\n".join(hits) if hits else "(no matches)", ctx.max_output_chars)


def _write_file(ctx: ToolContext, path: str, content: str) -> str:
    p = deliver.resolve_repo_path(ctx.repo, path)
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    p.write_text(content, encoding="utf-8")
    return (f"{'Overwrote' if existed else 'Created'} {path} "
            f"({len(content.splitlines())} lines).")


def _edit_file(ctx: ToolContext, path: str, old: str, new: str,
               replace_all: bool = False) -> str:
    """Exact-string replacement — the safest edit primitive: it fails loudly instead of
    guessing when the file doesn't look like the model expects."""
    p = deliver.resolve_repo_path(ctx.repo, path)
    if not p.is_file():
        return f"ERROR: no such file: {path!r}"
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        return (f"ERROR: `old` string not found in {path}. Read the file again and copy "
                f"the exact text (including indentation) you want to replace.")
    if count > 1 and not replace_all:
        return (f"ERROR: `old` appears {count} times in {path}. Include more surrounding "
                f"context to make it unique, or pass replace_all=true.")
    p.write_text(text.replace(old, new) if replace_all else text.replace(old, new, 1),
                 encoding="utf-8")
    return f"Edited {path} ({count if replace_all else 1} replacement(s))."


def command_allowed(cmd: str, chat_cfg: dict[str, Any]) -> tuple[bool, bool, str]:
    """Policy check for a shell command → (allowed, needs_confirm, reason).

    Denylist wins over everything (never runnable). Allowlist prefixes run without a
    prompt. Anything else is allowed only with confirmation.
    """
    lowered = " ".join(cmd.lower().split())
    for bad in chat_cfg.get("denied_cmd_patterns", []):
        if bad.lower() in lowered:
            return False, False, f"blocked by denied_cmd_patterns ({bad!r})"
    for ok in chat_cfg.get("allowed_cmds", []):
        if lowered.startswith(ok.lower()):
            return True, False, "allowlisted"
    return True, True, "not allowlisted — needs confirmation"


def _run_shell(ctx: ToolContext, cmd: str, timeout_s: int) -> str:
    """Run via a script file so Windows cmd doesn't re-tokenize quoted arguments."""
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "agent_cmd.cmd"
        script.write_text("@echo off\r\n" + cmd + "\r\n", encoding="utf-8")
        try:
            proc = subprocess.run(["cmd", "/c", str(script)], cwd=ctx.repo,
                                  capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return f"TIMEOUT after {timeout_s}s: {cmd}"
        except OSError as exc:
            return f"ERROR running command: {exc}"
    out = (proc.stdout or "") + (proc.stderr or "")
    return f"exit={proc.returncode}\n{_truncate(out.strip(), ctx.max_output_chars)}"


def _run_cmd(ctx: ToolContext, cmd: str) -> str:
    chat_cfg = ctx.chat_cfg()
    allowed, needs_confirm, reason = command_allowed(cmd, chat_cfg)
    if not allowed:
        return f"REFUSED: {reason}. Command not run."
    if needs_confirm and chat_cfg.get("confirm_commands", True):
        if ctx.confirm is None or not ctx.confirm("run_cmd", cmd):
            return "REFUSED by the user. Try a different approach or ask them why."
    return _run_shell(ctx, cmd, int(chat_cfg.get("cmd_timeout_s", 300)))


def _run_tests(ctx: ToolContext) -> str:
    if not ctx.test_cmd:
        return ("ERROR: no test command configured for this repo. Set agent_chat.test_cmd "
                "or `test_cmd` in <repo>/.qwen-pipeline.json, or use run_cmd.")
    return _run_shell(ctx, ctx.test_cmd,
                      int(ctx.chat_cfg().get("test_timeout_s", 600)))


def _finish(ctx: ToolContext, summary: str) -> str:
    return summary


# --- registry ---------------------------------------------------------------
def build_tools(ctx: ToolContext) -> dict[str, Tool]:
    """The tool registry for one session, bound to its ToolContext."""
    def bind(fn):
        return lambda **kw: fn(ctx, **kw)

    defs: list[Tool] = [
        Tool("list_files", "List repo files (optionally under a directory / matching a "
             "glob). Use this first to orient yourself.",
             {"type": "object", "properties": {
                 "dir": {"type": "string", "description": "repo-relative directory"},
                 "pattern": {"type": "string", "description": "glob, e.g. src/**/*.ts"}},
              "required": []}, bind(_list_files)),
        Tool("read_file", "Read a repo file with line numbers. Optionally a line range. "
             "ALWAYS read a file before editing it.",
             {"type": "object", "properties": {
                 "path": {"type": "string"},
                 "start": {"type": "integer", "description": "first line (1-based)"},
                 "end": {"type": "integer"}},
              "required": ["path"]}, bind(_read_file)),
        Tool("search", "Regex-search the repo. Returns path:line: match.",
             {"type": "object", "properties": {
                 "pattern": {"type": "string", "description": "regular expression"},
                 "glob": {"type": "string", "description": "limit to matching paths"}},
              "required": ["pattern"]}, bind(_search)),
        Tool("write_file", "Create a file or replace its ENTIRE contents. For changing "
             "part of an existing file prefer edit_file.",
             {"type": "object", "properties": {
                 "path": {"type": "string"}, "content": {"type": "string"}},
              "required": ["path", "content"]}, bind(_write_file), mutating=True),
        Tool("edit_file", "Replace an exact string in a file. `old` must match the file "
             "byte-for-byte (copy it from read_file) and be unique unless replace_all.",
             {"type": "object", "properties": {
                 "path": {"type": "string"}, "old": {"type": "string"},
                 "new": {"type": "string"},
                 "replace_all": {"type": "boolean"}},
              "required": ["path", "old", "new"]}, bind(_edit_file), mutating=True),
        Tool("run_cmd", "Run a shell command in the repo root. Non-allowlisted commands "
             "require the user's approval.",
             {"type": "object", "properties": {"cmd": {"type": "string"}},
              "required": ["cmd"]}, bind(_run_cmd), needs_confirm=True),
        Tool("run_tests", "Run this project's configured test command.",
             {"type": "object", "properties": {}, "required": []}, bind(_run_tests)),
        Tool("finish", "Call when the task is complete, with a one-paragraph summary of "
             "what you changed.",
             {"type": "object", "properties": {"summary": {"type": "string"}},
              "required": ["summary"]}, bind(_finish)),
    ]
    return {t.name: t for t in defs}


def schemas(registry: dict[str, Tool]) -> list[dict[str, Any]]:
    return [t.schema() for t in registry.values()]


def dispatch(registry: dict[str, Tool], name: str, args: dict[str, Any]) -> str:
    """Run a tool by name. Unknown tools and bad arguments come back as text the model
    can recover from — never an exception that kills the session."""
    tool = registry.get(name)
    if tool is None:
        return (f"ERROR: unknown tool {name!r}. Available: "
                f"{', '.join(sorted(registry))}.")
    try:
        return tool.run(**args)
    except TypeError as exc:
        return f"ERROR: bad arguments for {name}: {exc}"
    except ValueError as exc:          # path guard rejections land here
        return f"ERROR: {exc}"
    except OSError as exc:
        return f"ERROR: {exc}"
