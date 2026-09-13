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

import os
import re
import signal
import subprocess
import tempfile
import threading
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


# Grace period for a tree kill and for the reader thread to notice EOF.
_KILL_GRACE_S = 5


def _kill_process_tree(proc: "subprocess.Popen[str]") -> None:
    """Kill the child AND everything it spawned.

    `Popen.kill()` only stops the DIRECT child — here the `cmd.exe`/`sh` wrapper. The
    grandchild that actually runs the tests survives, keeps the inherited stdout pipe
    write-handle open, and any later read/`communicate()` then blocks forever: one hung
    test wedges the whole server instead of timing out. Windows has no process groups,
    so walk the tree with `taskkill /T`; POSIX kills the session group (the child is
    started with start_new_session).
    """
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=_KILL_GRACE_S)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _drain(stream, sink: list[str]) -> None:
    """Read a child's output to EOF on a background thread, so a child that fills the
    pipe buffer can never deadlock us and partial output survives a timeout kill."""
    try:
        for line in iter(stream.readline, ""):
            sink.append(line)
    except (OSError, ValueError):
        pass


def run_test_cmd(repo: str, test_cmd: str, timeout_s: int = 300) -> tuple[int | None, str]:
    """Run the project's acceptance command in `repo`.

    Returns (returncode, output). **returncode None means the command produced NO
    verdict** — it timed out or could not be started. That is an INFRASTRUCTURE fault,
    not a failing test: callers must not report it to a worker as a code defect.

    Executed via a script file (not `cmd /c <string>`) so quoted multi-word args
    survive Windows re-tokenization. This function is required to RETURN: output is
    drained on a background thread and the whole process tree is killed on timeout, so
    a hung or orphaned test runner cannot block the caller (see _kill_process_tree).
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        if os.name == "nt":
            script = Path(tmp) / "qwen_test.cmd"
            script.write_text("@echo off\r\n" + test_cmd + "\r\n", encoding="utf-8")
            argv, extra = ["cmd", "/c", str(script)], {}
        else:
            script = Path(tmp) / "qwen_test.sh"
            script.write_text("#!/bin/sh\n" + test_cmd + "\n", encoding="utf-8")
            script.chmod(0o700)
            argv, extra = ["/bin/sh", str(script)], {"start_new_session": True}
        try:
            # stdin=DEVNULL: never let a test inherit (and block on) the server's own
            # stdin — that is the MCP JSON-RPC pipe, which never reaches EOF.
            proc = subprocess.Popen(argv, cwd=repo, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, errors="replace", **extra)
        except (OSError, ValueError) as exc:
            return None, f"not found: {exc}"

        chunks: list[str] = []
        reader = threading.Thread(target=_drain, args=(proc.stdout, chunks), daemon=True)
        reader.start()
        timed_out = False
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process_tree(proc)
            try:
                proc.wait(timeout=_KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                pass
        # Bounded join: if some stubborn grandchild still holds the pipe, the daemon
        # thread is abandoned rather than allowed to hold up the caller.
        reader.join(timeout=_KILL_GRACE_S)
        try:
            if proc.stdout:
                proc.stdout.close()
        except OSError:
            pass
        out = "".join(chunks)
        if timed_out:
            return None, (f"test_cmd timed out after {timeout_s}s; the process tree was "
                          f"killed. Partial output:\n{out}")
        return proc.returncode, out


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
