"""Independent, cancellable acceptance checks on a task worktree."""
from __future__ import annotations

import os
import fnmatch
import subprocess
import tempfile
import time
from pathlib import Path

try:
    from . import gate, verify
    from .opencode_client import Cancelled
except ImportError:
    import gate, verify
    from opencode_client import Cancelled


def terminate(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        import signal
        os.killpg(process.pid, signal.SIGTERM)
    process.wait(timeout=10)


def command(repo: Path, cmd: str, cancelled, timeout: int = 600) -> tuple[bool, str]:
    # This command was supplied/approved by the caller, never inferred from model prose.
    with tempfile.TemporaryDirectory(prefix="apprentice-check-") as tmp:
        script = Path(tmp) / ("check.cmd" if os.name == "nt" else "check.sh")
        script.write_text(("@echo off\r\n" if os.name == "nt" else "#!/bin/sh\n") + cmd + "\n", encoding="utf-8")
        with (Path(tmp) / "check.log").open("w+b") as log:
            process = subprocess.Popen(["cmd", "/d", "/c", str(script)] if os.name == "nt" else ["sh", str(script)],
                cwd=repo, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPYCACHEPREFIX": str(Path(tmp) / "bytecode")},
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                start_new_session=os.name != "nt")
            deadline = time.monotonic() + timeout
            try:
                while process.poll() is None:
                    if cancelled.wait(.1):
                        raise Cancelled("Stopped during verification; no changes were applied.")
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"Acceptance check exceeded {timeout} seconds.")
                log.seek(0, 2)
                log.seek(max(0, log.tell() - 10000))
                output = log.read().decode("utf-8", "replace")
                if process.returncode != 0 and not output.strip():
                    output = f"Acceptance command exited with code {process.returncode} (no output)."
                return process.returncode == 0, output
            finally:
                terminate(process)


def check(workspace, cfg: dict, policy: str, test_cmd: str, cancelled) -> tuple[bool, str, str]:
    if policy == "off":
        return True, "not checked", ""
    checks = []
    changes = workspace.changes()
    protected = cfg.get("opencode", {}).get("protected_checks", ["tests/*", "test_*", "*.test.*", "*.spec.*"])
    if not cfg.get("opencode", {}).get("allow_acceptance_edits", False):
        for name, values in changes.items():
            if values["before"]["data"] is not None and any(fnmatch.fnmatch(name, pattern) for pattern in protected):
                return False, "acceptance integrity", f"Existing acceptance file changed: {name}. Restore it; changes to acceptance tests require explicit allow_acceptance_edits."
    # Check project-coupled code in its own context, not as detached TS snippets.
    if policy == "tests" and test_cmd:
        ok, output = command(workspace.directory, test_cmd, cancelled,
                             int(cfg.get("agent_chat", {}).get("test_timeout_s", 600)))
        return ok, "project tests", output
    for name, values in changes.items():
        role = verify._EXT_ROLE.get(Path(name).suffix.lower())
        if not role or values["after"]["data"] is None:
            continue
        if cancelled.is_set():
            raise Cancelled("Stopped before verification.")
        code = (workspace.directory / name).read_text(encoding="utf-8", errors="replace")
        result = gate.run_gate("```\n" + code + "\n```", role, cfg)
        if result.status == "fail":
            return False, result.check, result.error_text
        if result.status == "pass":
            checks.append(result.check)
    if changes and not checks:
        return False, "no applicable checks", "No configured check can verify these edits. Configure a project test command or explicitly select verification off."
    return True, ", ".join(sorted(set(checks))) or "no changes", ""
