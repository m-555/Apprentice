"""§6 Tier-2 — on-demand batched C++ verify via the §9 host-harness (NOT per-delegate).

Does the REAL work the fast per-delegate cpp lint (gate.py) can't: compiles the plugins
in the UE host project and (optionally) runs their UE automation tests, then reports
pass/fail + captured errors. It is SLOW (a real UE build / headless editor boot), so run
it after a BATCH of C++ changes or before a commit — never per delegation. It is
deliberately a standalone CLI, not wired into delegate(), so it can't collide with a
delegation or add per-task latency.

Editor-lock aware: if the interactive UnrealEditor is open it holds the plugin DLL lock,
so this refuses to build (use --force to override at your own risk).

Config: config/qwen.json -> "host_harness".
Usage:
    python src/host_verify.py               # build only
    python src/host_verify.py --tests       # build, then run UE automation tests
    python src/host_verify.py --tests-only   # skip build, just run tests
    python src/host_verify.py --force        # build even if the editor appears open
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _ROOT / "config" / "qwen.json"


def _cfg() -> dict[str, Any]:
    return json.loads(_CONFIG_PATH.read_text(encoding="utf-8")).get("host_harness", {})


def editor_running() -> bool:
    """True if the interactive UnrealEditor.exe is running (it locks the plugin DLL).
    Note: our own headless UnrealEditor-Cmd.exe is a different image, so it won't trip this."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq UnrealEditor.exe"],
            capture_output=True, text=True, timeout=15,
        ).stdout
        return "UnrealEditor.exe" in out
    except Exception:
        return False


def build(hc: dict[str, Any]) -> tuple[bool, str]:
    """Incremental host-harness build. Returns (ok, error_report). Incremental is
    seconds when the engine's Intermediate is warm; first build per version is slow."""
    bat = hc["engine_build_bat"]
    cmd = ["cmd", "/c", bat, hc["target"], hc.get("platform", "Win64"),
           hc.get("config", "Development"),
           f'-Project={hc["project"]}', "-WaitMutex", "-NoHotReload"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=int(hc.get("build_timeout_s", 1800)))
    except subprocess.TimeoutExpired:
        return False, "build timed out"
    out = (proc.stdout or "") + (proc.stderr or "")
    # MSVC/UBT error lines: `file(line): error C1234:` / `: error :` / UBT "Result: Failed".
    errs = [ln for ln in out.splitlines()
            if "): error " in ln or ": error " in ln.lower()
            or "Result: Failed" in ln or ln.strip().startswith("ERROR:")]
    ok = proc.returncode == 0 and "Result: Succeeded" in out
    tail = "\n".join(out.splitlines()[-12:])
    report = ("\n".join(errs[-60:]) if errs else "") + f"\n--- build tail ---\n{tail}"
    return ok, report.strip()


def parse_test_output(out: str, log_path: str = "") -> tuple[bool, str]:
    """Pure parse of automation output (separated so it's unit-testable without the editor).
    UE logs a pass as `Result={Success}` and a failure as `Result={Fail}`."""
    passed = out.count("Result={Success}")
    failed_lines = [ln for ln in out.splitlines() if "Result={Fail}" in ln]
    ran = passed + len(failed_lines)
    ok = ran > 0 and not failed_lines
    report = f"tests ran={ran} passed={passed} failed={len(failed_lines)}"
    if failed_lines:
        report += "\n" + "\n".join(failed_lines[-40:])
    if ran == 0:
        report += f"\n(WARN: no tests parsed — check filter/log{': ' + log_path if log_path else ''})"
    return ok, report


def run_tests(hc: dict[str, Any]) -> tuple[bool, str]:
    """Run the plugins' UE automation tests headless via the editor commandlet."""
    filt = hc.get("test_filter", "McpAutomationBridge+UnrealAgent")
    log_path = Path(tempfile.gettempdir()) / "qwen_host_verify_automation.log"
    cmd = [hc["editor_cmd"], hc["project"],
           f"-ExecCmds=Automation RunTests {filt};Quit",
           "-unattended", "-nopause", "-nosplash", "-nullrhi", "-nosound",
           "-log", f"-abslog={log_path}"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=int(hc.get("test_timeout_s", 900)))
    except subprocess.TimeoutExpired:
        return False, "automation test run timed out"
    out = (proc.stdout or "") + (proc.stderr or "")
    if log_path.exists():
        out += "\n" + log_path.read_text(encoding="utf-8", errors="replace")
    return parse_test_output(out, str(log_path))


def main(argv: list[str]) -> int:
    hc = _cfg()
    if not hc:
        print("No host_harness config in config/qwen.json.")
        return 2
    do_tests = "--tests" in argv or "--tests-only" in argv
    tests_only = "--tests-only" in argv
    force = "--force" in argv

    if not tests_only:
        if editor_running() and not force:
            print("REFUSING: UnrealEditor.exe appears to be open (it locks the plugin DLL).\n"
                  "Close the editor and re-run, or pass --force to try anyway.")
            return 3
        print("[host_verify] building host harness (incremental)…")
        ok, report = build(hc)
        print(report)
        print(f"[host_verify] BUILD {'PASSED' if ok else 'FAILED'}")
        if not ok:
            return 1

    if do_tests:
        print("[host_verify] running UE automation tests (headless)…")
        ok, report = run_tests(hc)
        print(report)
        print(f"[host_verify] TESTS {'PASSED' if ok else 'FAILED'}")
        if not ok:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
