"""Explicitly opted-in GPU smoke test. Uses disposable code, never production edits."""
import argparse
import json
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import paths
from opencode_tasks import Task
from opencode_ui import new_session
from workspace import git


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", required=True)
    parser.add_argument("--allow-gpu", action="store_true", required=True)
    args = parser.parse_args()
    cfg = paths.load_config()
    cfg["agent_chat"]["use_corrections"] = False
    cfg["opencode"].update(max_steps=12, max_attempts=2, task_timeout_s=600)
    cfg["metering"] = {"enabled": False}
    provider = cfg["providers"][args.provider]
    base = provider["base_url"].removesuffix("/v1")
    def health():
        with urllib.request.urlopen(base + "/health", timeout=10) as response:
            return json.load(response)
    baseline = health()
    if baseline.get("activeRequests") or baseline.get("model"):
        raise RuntimeError("The router must be idle and unloaded before this test; do not interrupt another user's request.")
    reports = []
    with tempfile.TemporaryDirectory(prefix="apprentice-live-") as temp:
        root = Path(temp)
        repo = root / "repo"
        repo.mkdir()
        (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        git(repo, "init")
        git(repo, "add", "-A")
        git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture")
        session = new_session(str(repo), cfg, args.provider, verify="tests",
            test_cmd=f'"{sys.executable}" -c "from calc import add; assert add(2,3)==5; assert add(-1,1)==0"')
        try:
            with patch.object(paths, "OUTPUTS_DIR", root / "outputs"), patch.object(paths, "CORRECTIONS_PATH", root / "corrections.jsonl"):
                for mode, prompt in [
                    ("ask", "Without changing any code, inspect calc.py and explain how add currently works, including one numeric example. Answer in at most four sentences."),
                    ("build", "Fix add in calc.py so it returns the sum of its two arguments. Change only calc.py. Give a short final answer. Apprentice runs the acceptance command itself; you do not need a shell command."),
                ]:
                    began = time.monotonic()
                    events, first_text = [], []
                    def emit(event):
                        events.append(event)
                        if event["type"] == "message_part" and event.get("text") and not first_text:
                            first_text.append(time.monotonic() - began)
                        if event["type"] in ("task_status", "verify_passed", "verify_failed", "error"):
                            print(json.dumps({"provider": args.provider, "elapsed_s": round(time.monotonic()-began, 2), **event}), flush=True)
                    task = Task(cfg, emit)
                    result = task.run(session, prompt, mode=mode)
                    report = {"provider": args.provider, "model": provider["model"], "mode": mode,
                              "seconds": round(time.monotonic() - began, 2), "first_visible_text_s": round(first_text[0], 2) if first_text else None,
                              "done_passed": result["done_passed"], "applied": result["applied"], "answer": result["answer"],
                              "tools": sorted({e.get("tool") for e in events if e["type"] == "tool_part"}),
                              "usage_cumulative": dict(session["usage"])}
                    print(json.dumps(report, ensure_ascii=False), flush=True)
                    reports.append(report)
                    assert result["done_passed"], result
                    if mode == "ask":
                        assert "a - b" in (repo / "calc.py").read_text()
                    else:
                        assert result["applied"], result
                # Stop a real in-flight request and require the supervisor to release it.
                import threading
                task = Task(cfg, lambda _: None)
                cancellation = []
                def request():
                    try:
                        task.run(session, "Explain 100 edge cases for numerical addition in a long detailed list.", mode="ask")
                    except Exception as exc:
                        cancellation.append(str(exc))
                thread = threading.Thread(target=request)
                thread.start()
                deadline = time.monotonic() + 40
                while thread.is_alive() and not health().get("activeRequests") and time.monotonic() < deadline:
                    time.sleep(.2)
                active = health().get("activeRequests", 0)
                start = time.monotonic()
                task.cancel()
                thread.join(30)
                if thread.is_alive():
                    raise RuntimeError("Cancellation did not finish within 30 seconds.")
                assert active > 0, "The cancellation probe never reached an active model request."
                deadline = time.monotonic() + 10
                while health().get("activeRequests") and time.monotonic() < deadline:
                    time.sleep(.2)
                assert health().get("activeRequests") == 0
                reports.append({"mode": "cancel", "seconds": round(time.monotonic()-start, 2), "result": cancellation})
        finally:
            if health().get("activeRequests") == 0:
                request = urllib.request.Request(base + "/unload", method="POST", data=b"{}", headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=30) as response:
                    print("Unload: " + response.read().decode(), flush=True)
            else:
                print("Router still busy; did not forcibly unload it.", flush=True)
        report_path = paths.OUTPUTS_DIR / ("live-opencode-" + args.provider + ".json")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(reports, indent=2, ensure_ascii=False), encoding="utf-8")
        print("Report: " + str(report_path), flush=True)


if __name__ == "__main__":
    main()
