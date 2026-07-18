"""Server-side context fetch + apply/test for `delegate` — the wave-2 token savers.

Why: the orchestrator's OUTPUT tokens are the expensive kind. Without this module it
pays twice per delegation — once to paste context INTO the tool call, once to ingest
the worker's code back OUT. Since the pipeline runs on the same machine as the target
repo, it can do both sides itself:

  • read_context(repo, specs)   — orchestrator sends file PATHS (~15 tokens), the
    server reads the content locally and builds the context block.
  • apply_code / revert_apply   — on a gate-passed output, the server writes the code
    into the real file; run_test_cmd runs the project's own acceptance command and the
    caller reverts on red. The orchestrator only ever sees the status footer.

SECURITY: every path is resolved and must stay inside `repo` (no traversal, no
absolute escapes); file and total sizes are capped. `test_cmd` is the orchestrator's
own command (same trust model as assign's done_when) and runs via a script file so
Windows cmd quoting survives.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

# "path/to/file.ts" or "path/to/file.ts:20-80" (1-based, inclusive line range)
_RANGE_RE = re.compile(r"^(?P<path>.+?):(?P<start>\d+)-(?P<end>\d+)$")


def resolve_repo_path(repo: str, rel: str) -> Path:
    """Resolve `rel` against `repo` and refuse anything that escapes the repo root."""
    root = Path(repo).resolve()
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise ValueError(f"path escapes the repo root: {rel!r}")
    return p


def read_context(repo: str, specs: list[str],
                 max_file_kb: int = 48, max_total_kb: int = 192) -> str:
    """Build a context block from repo-relative file specs (optional `:start-end` line
    ranges). Caps per-file and total size so a fat file can't blow up the worker prompt."""
    parts: list[str] = []
    total = 0
    max_file = max_file_kb * 1024
    max_total = max_total_kb * 1024
    for spec in specs:
        m = _RANGE_RE.match(spec)
        rel, start, end = (m.group("path"), int(m.group("start")), int(m.group("end"))) \
            if m else (spec, 0, 0)
        p = resolve_repo_path(repo, rel)
        if not p.is_file():
            raise ValueError(f"context file not found: {rel!r}")
        text = p.read_text(encoding="utf-8", errors="replace")
        label = rel
        if start:
            lines = text.splitlines()
            text = "\n".join(lines[start - 1:end])
            label = f"{rel} (lines {start}-{min(end, len(lines))})"
        if len(text) > max_file:
            text = text[:max_file] + "\n… (truncated: file cap reached)"
        chunk = f"--- {label} ---\n{text}"
        if total + len(chunk) > max_total:
            parts.append(f"--- {label} --- OMITTED (total context cap reached)")
            break
        parts.append(chunk)
        total += len(chunk)
    return "\n\n".join(parts)


def apply_code(repo: str, rel: str, code: str,
               mode: str = "append") -> tuple[str | None, Path]:
    """Write `code` into repo/rel. Returns (original_content_or_None, path) so the
    caller can revert. Modes: append (default; creates if missing), create (must not
    exist), overwrite (must exist)."""
    p = resolve_repo_path(repo, rel)
    original = p.read_text(encoding="utf-8") if p.exists() else None
    if mode == "create" and original is not None:
        raise ValueError(f"apply_mode=create but file exists: {rel!r}")
    if mode == "overwrite" and original is None:
        raise ValueError(f"apply_mode=overwrite but file missing: {rel!r}")
    if mode not in ("append", "create", "overwrite"):
        raise ValueError(f"unknown apply_mode: {mode!r} (append|create|overwrite)")
    p.parent.mkdir(parents=True, exist_ok=True)
    if mode == "append" and original is not None:
        body = original if original.endswith("\n") else original + "\n"
        p.write_text(body + "\n" + code.rstrip("\n") + "\n", encoding="utf-8")
    else:
        p.write_text(code.rstrip("\n") + "\n", encoding="utf-8")
    return original, p


def revert_apply(path: Path, original: str | None) -> None:
    """Undo apply_code: restore the original content, or delete a file we created."""
    if original is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(original, encoding="utf-8")


def run_test_cmd(repo: str, test_cmd: str, timeout_s: int = 300) -> tuple[int | None, str]:
    """Run the project's acceptance command in `repo`. Executed via a script file (not
    `cmd /c <string>`) so quoted multi-word args survive Windows re-tokenization."""
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "qwen_test.cmd"
        script.write_text("@echo off\r\n" + test_cmd + "\r\n", encoding="utf-8")
        try:
            proc = subprocess.run(["cmd", "/c", str(script)], cwd=repo,
                                  capture_output=True, text=True, timeout=timeout_s)
            return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except FileNotFoundError as exc:
            return None, f"not found: {exc}"
        except subprocess.TimeoutExpired:
            return None, f"test_cmd timed out after {timeout_s}s"


def load_repo_options(repo: str) -> dict[str, Any]:
    """Read <repo>/.qwen-pipeline.json (whole file, not just the agent block) — used
    for per-repo `conventions` and delegate overrides. Missing/broken file = {}."""
    import json
    p = Path(repo) / ".qwen-pipeline.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
