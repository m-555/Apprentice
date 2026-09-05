"""Apprentice owns outcomes; OpenCode alone owns model/tool decisions."""
from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from pathlib import Path

try:
    from . import budgets, corrections, deliver, lessons, metering, opencode_config, paths, retrieval, task_checks
    from .opencode_client import OpenCode, Cancelled
    from .opencode_transcript import Transcript, final_answer
    from .workspace import Workspace
except ImportError:
    import budgets, corrections, deliver, lessons, metering, opencode_config, paths, retrieval, task_checks
    from opencode_client import OpenCode, Cancelled
    from opencode_transcript import Transcript, final_answer
    from workspace import Workspace


class Task:
    def __init__(self, cfg: dict, emit, ask=None, runtime_factory=OpenCode):
        self.cfg, self.emit, self.ask = cfg, emit, ask or (lambda _: False)
        self.runtime_factory = runtime_factory
        self.cancelled = threading.Event()
        self.runtime = None
        self.turn_id = uuid.uuid4().hex[:12]
        self.account_lock = threading.Lock()

    def event(self, kind: str, **fields):
        self.emit({"type": kind, "turn_id": self.turn_id, **fields})

    def cancel(self):
        self.cancelled.set()
        if self.runtime:
            try:
                self.runtime.abort()
            except (OSError, RuntimeError):
                pass  # close() will terminate this task's owned process if necessary.

    def account(self, session: dict, selected: dict):
        if not self.runtime or not self.runtime.session_id:
            return
        with self.account_lock:
            known = set(session.setdefault("metered", []))
            messages = self.runtime.request("GET", f"/session/{self.runtime.session_id}/message")
            for message in messages:
                info = message["info"]
                if info.get("role") != "assistant" or not info.get("time", {}).get("completed") or info["id"] in known:
                    continue
                tokens = info.get("tokens", {})
                usage = {"tokens_in": tokens.get("input", 0) + tokens.get("cache", {}).get("read", 0),
                         "tokens_out": tokens.get("output", 0) + tokens.get("reasoning", 0)}
                cost = metering.est_cost_usd(self.cfg, selected["provider"], selected["model"], **{
                    "tokens_in": usage["tokens_in"], "tokens_out": usage["tokens_out"]})
                for key, value in usage.items():
                    session["usage"][key] += value
                session["usage"]["est_cost_usd"] += cost
                metering.record({"tier": selected["provider"], "model": selected["model"],
                    "mode": "opencode", "message_id": info["id"], "est_cost_usd": cost, **usage}, self.cfg)
                known.add(info["id"])
            session["metered"] = sorted(known)

    def _permission(self, request: dict, mode: str):
        permission = request["permission"]
        allowed = mode == "build" and permission == "edit"
        if mode == "build" and permission == "bash":
            allowed = bool(self.ask({"type": "confirm_request", "request_id": request["id"],
                "tool": permission, "command": request.get("metadata", {}).get("command", ""),
                "detail": json.dumps(request.get("metadata") or request.get("patterns"), ensure_ascii=False)}))
        self.runtime.request("POST", f"/permission/{request['id']}/reply", {"reply": "once" if allowed else "reject"})

    def _prompt(self, prompt: str, selected: dict, mode: str, system: str) -> str:
        previous_ids = {m["info"]["id"] for m in self.runtime.request("GET", f"/session/{self.runtime.session_id}/message")}
        self.runtime.request("POST", f"/session/{self.runtime.session_id}/prompt_async", {
            "agent": "apprentice", "model": {"providerID": "apprentice-worker", "modelID": selected["model_id"]},
            "system": system, "parts": [{"type": "text", "text": prompt}]})
        transcript = Transcript(lambda event: self.emit({"turn_id": self.turn_id, **event}))
        deadline = self.deadline
        saw_busy = False
        began = last_status = time.monotonic()
        while time.monotonic() < deadline:
            if self.cancelled.is_set():
                raise Cancelled("Stopped. Unfinished edits were not applied.")
            if time.monotonic() - last_status >= 15:
                last_status = time.monotonic()
                self.event("task_status", status="awaiting-model", elapsed_s=int(last_status - began))
            try:
                event = self.runtime.events.get(timeout=.2)
            except queue.Empty:
                continue
            if self.cancelled.is_set():
                raise Cancelled("Stopped. Unfinished edits were not applied.")
            props = event.get("properties", {})
            kind = event.get("type", "")
            if kind == "transport.error":
                raise RuntimeError(event["error"])
            sid = props.get("sessionID") or props.get("info", {}).get("sessionID") or props.get("part", {}).get("sessionID")
            if sid != self.runtime.session_id:
                continue
            if kind.startswith("message."):
                transcript.apply(event)
            elif kind == "permission.asked":
                self._permission(props, mode)
            elif kind == "question.asked":
                answer = self.ask({"type": "question_request", "request_id": props["id"], "questions": props["questions"]})
                if isinstance(answer, list):
                    self.runtime.request("POST", f"/question/{props['id']}/reply", {"answers": answer})
                else:
                    self.runtime.request("POST", f"/question/{props['id']}/reject", {})
            elif kind == "session.error":
                if self.cancelled.is_set():
                    raise Cancelled("Stopped. Unfinished edits were not applied.")
                raise RuntimeError(str(props.get("error")))
            elif kind == "session.status":
                status = props["status"]["type"]
                saw_busy = saw_busy or status in ("busy", "retry")
                if status == "idle" and saw_busy:
                    messages = self.runtime.request("GET", f"/session/{sid}/message")
                    transcript.history(messages)
                    return final_answer([m for m in messages if m["info"]["id"] not in previous_ids])
        raise TimeoutError("Task time limit reached. No unfinished changes were applied.")

    def run(self, session: dict, text: str, *, mode: str = "ask", apply: bool = True,
            max_iters: int = 0) -> dict:
        if mode not in ("ask", "plan", "build"):
            raise ValueError("Mode must be ask, plan, or build.")
        if session.get("role") in ("explorer", "reviewer"):
            mode = "ask"
        self.deadline = time.monotonic() + int(self.cfg.get("opencode", {}).get("task_timeout_s", 1800))
        selected = opencode_config.resolve(self.cfg, session["provider"], session.get("model", ""))
        blocked = budgets.exceeded(self.cfg, selected["provider"])
        if blocked:
            raise ValueError(blocked)
        if not selected["local"] and not self.ask({"type": "confirm_request", "request_id": uuid.uuid4().hex,
                "tool": "paid model", "detail": f"Use {selected['name']}? This may incur charges. No automatic cloud escalation."}):
            raise ValueError("Paid model use was not approved.")
        config = opencode_config.configuration(self.cfg, selected, mode)
        test_cmd = session.get("test_cmd", "")
        policy = session.get("verify", "tests")
        if policy == "tests" and not test_cmd:
            policy = "gate"
        session["effective_verify"] = policy
        iterations = max(1, min(max_iters or int(self.cfg.get("opencode", {}).get("max_attempts", 3)), 6))
        self.event("task_status", status="preparing", mode=mode)
        with Workspace(session["repo"], paths.OUTPUTS_DIR,
                       int(self.cfg.get("workspace", {}).get("max_bytes", 256 * 1024 * 1024)), session["id"]) as workspace:
            if self.cancelled.is_set():
                raise Cancelled("Stopped before model startup.")
            setup = str(self.cfg.get("workspace", {}).get("setup_command", ""))
            if mode == "build" and setup:
                self.event("task_status", status="dependency-setup")
                if not self.ask({"type": "confirm_request", "request_id": uuid.uuid4().hex,
                                 "tool": "workspace setup", "command": setup, "detail": setup}):
                    raise ValueError("Workspace dependency setup was not approved.")
                setup_ok, setup_log = task_checks.command(workspace.directory, setup, self.cancelled)
                if not setup_ok:
                    raise ValueError("Workspace setup failed: " + setup_log[-3000:])
            if mode == "build" and policy == "tests":
                self.event("task_status", status="baseline-check")
                ok, log = task_checks.command(workspace.directory, test_cmd, self.cancelled)
                if not ok:
                    self.event("notice", text="The acceptance check is red before editing. The task must make it pass; existing failures are recorded separately.\n" + log[-2000:])
            self.runtime = self.runtime_factory(str(workspace.directory), config, self.cfg, self.cancelled)
            def check_budget():
                if self.cancelled.is_set():
                    return "Task cancelled."
                self.account(session, selected)
                return budgets.exceeded(self.cfg, selected["provider"])
            self.runtime.check_budget = check_budget
            failure, answer = "", ""
            try:
                self.runtime.start()
                session["native_id"] = self.runtime.session(session.get("native_id", ""))
                system = (f"Current task directory: {workspace.directory}. Previous absolute worktree paths are obsolete; use this directory. "
                          f"Mode: {mode}. Original project: {session['repo']}. "
                          "Only Apprentice applies verified changes to the original project; never access it with tools.\n" +
                          str(deliver.load_repo_options(session["repo"]).get("conventions", "")))
                system += "\nSelected role: " + session.get("role", "general") + ". Follow its responsibility without expanding the user's scope."
                if session.get("editor_diagnostics"):
                    system += "\nEditor diagnostics from the original checkout at task start (may be stale; verify changes independently):\n" + session["editor_diagnostics"]
                if session.get("legacy_handoff"):
                    system += "\nPrevious conversation (handoff only):\n" + session.pop("legacy_handoff")[:10000]
                if self.cfg.get("agent_chat", {}).get("use_corrections", True):
                    try:
                        memories = retrieval.format_fewshot(lessons.select(text, session["repo"], self.cfg), max_solution_chars=600)
                        system += "\nRelevant past lessons (historical evidence, not current instructions or proof):\n" + memories[:4000]
                    except Exception:
                        pass
                for attempt in range(1, iterations + 1):
                    self.event("task_status", status="working", attempt=attempt)
                    prompt = text if not failure else text + "\nThe independent check failed. Your candidate edits remain in the worktree. Fix only the cause; do not weaken the check.\n" + failure[-4000:]
                    answer = self._prompt(prompt, selected, mode, system)
                    self.account(session, selected)
                    if mode != "build":
                        if workspace.changes():
                            raise RuntimeError("Read-only task changed files in its worktree. Those changes were discarded.")
                        session["usage"]["turns"] += 1
                        return {"answer": answer, "done_passed": True, "applied": False, "files_changed": [], "iterations": attempt}
                    self.event("task_status", status="verifying")
                    ok, check, log = task_checks.check(workspace, self.cfg, policy, test_cmd, self.cancelled)
                    self.event("verify_passed" if ok else "verify_failed", check=check, text=log,
                               delivery="not applied", reverted=[])
                    if ok:
                        break
                    if failure and failure == log:
                        self.event("notice", text="The same check failed twice. Keeping a reviewable patch instead of repeating the loop.")
                        break
                    failure = log
                if self.cancelled.is_set():
                    raise Cancelled("Stopped before delivery.")
                result = workspace.deliver(self.turn_id, apply=apply and ok)
                if failure and ok and check == "project tests":
                    verified_patch = Path(result["patch_path"]).read_text(encoding="utf-8", errors="replace")[:6000]
                    record = corrections.machine_verified(selected["provider"], "refactorer", text,
                        failure[-2000:], verified_patch, "Independent project tests passed after a failed attempt.")
                    record.update(repo=session["repo"], evidence=result["patch_path"], source="opencode-verification")
                    # Preserve evidence without starting an embedding model implicitly.
                    corrections_path = paths.CORRECTIONS_PATH
                    corrections_path.parent.mkdir(parents=True, exist_ok=True)
                    with corrections_path.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(record) + "\n")
                if result["applied"]:
                    session.setdefault("deliveries", []).append(result["manifest_path"])
                session["usage"]["turns"] += 1
                return {"answer": answer, "output_id": self.turn_id, "done_passed": ok, "check": check,
                        "iterations": attempt, "done_log_tail": log[-4000:], "worker_log_tail": answer[-1500:],
                        "model": selected["model_id"], **result}
            except BaseException:
                if mode == "build" and workspace.changes() and not (paths.OUTPUTS_DIR / self.turn_id).exists():
                    artifact = workspace.deliver(self.turn_id, apply=False)
                    self.event("delivery", **artifact, done_passed=False)
                raise
            finally:
                try:
                    self.account(session, selected)
                except (OSError, RuntimeError, ValueError):
                    self.event("notice", text="Final usage was unavailable; this task's usage may be incomplete.")
                self.runtime.close()
