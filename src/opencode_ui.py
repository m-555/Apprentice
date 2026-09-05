"""Versioned JSON-lines / terminal frontends for the OpenCode task controller."""
from __future__ import annotations

import json
import queue
import re
import sys
import threading
import uuid
from pathlib import Path

try:
    from . import chat_ui, deliver, opencode_config, paths, workspace
    from .opencode_client import Cancelled
    from .opencode_tasks import Task
except ImportError:
    import chat_ui, deliver, opencode_config, paths, workspace
    from opencode_client import Cancelled
    from opencode_tasks import Task

ROLES = ["general", "explorer", "implementer", "reviewer"]


def new_session(repo, cfg, provider, model="", verify="", test_cmd="", resume=""):
    root = str(Path(repo).resolve())
    session = {"id": resume or uuid.uuid4().hex[:12], "repo": root, "provider": provider,
        "model": model, "verify": verify or cfg.get("agent_chat", {}).get("verify", "tests"),
        "test_cmd": chat_ui._resolve_test_cmd(root, cfg, test_cmd), "protocol_version": 2,
        "mode": "ask", "role": "general", "events": [], "first_task": "", "deliveries": [],
        "usage": {"tokens_in": 0, "tokens_out": 0, "est_cost_usd": 0.0, "turns": 0}}
    if resume:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", resume):
            raise ValueError("Invalid session identifier.")
        old = json.loads((paths.ROOT / "sessions" / f"{resume}.json").read_text(encoding="utf-8"))
        if Path(old["repo"]).resolve() != Path(root):
            raise ValueError("This session belongs to another repository. Open that repository to resume it.")
        if old.get("protocol_version") == 2:
            session.update(old)
        else:
            readable = [m for m in old.get("messages", []) if m.get("role") in ("user", "assistant") and m.get("content")]
            session["legacy_handoff"] = "\n".join(m["role"] + ": " + m["content"][:2000] for m in readable[-6:])
            session["events"] = [{"type": "notice", "text": "Legacy session imported as a conversation handoff; this task now uses OpenCode."}]
            session["events"] += [{"type": "user" if m["role"] == "user" else "text", "text": m["content"][:4000]} for m in readable[-6:]]
    selected = opencode_config.resolve(cfg, session["provider"], session["model"])
    session["model"] = selected["model"]
    return session


def save(session):
    folder = paths.ROOT / "sessions"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{session['id']}.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(session, ensure_ascii=False), encoding="utf-8")
    temporary.replace(target)
    return target


class UI:
    def __init__(self, session: dict, cfg: dict, json_mode: bool, auto_yes=False):
        self.session, self.cfg, self.json_mode, self.auto_yes = session, cfg, json_mode, auto_yes
        self.inputs: queue.Queue = queue.Queue()
        self.pending: dict[str, queue.Queue] = {}
        self.task: Task | None = None
        self.lock = threading.RLock()
        self.closed = False
        self.last_text = {}

    def emit(self, event: dict):
        event = {"session_id": self.session["id"], **event}
        with self.lock:
            if event["type"] in ("message_part", "tool_part"):
                existing = next((i for i, value in enumerate(self.session["events"]) if value.get("id") == event["id"]), None)
                if existing is None:
                    self.session["events"].append(event)
                else:
                    self.session["events"][existing] = event
            elif event["type"] in ("user", "text", "notice", "verify_passed", "verify_failed", "delivery", "stopped", "error") or (event["type"] == "ack" and "reverted" in event):
                self.session["events"].append(event)
            elif event["type"] == "message_remove":
                self.session["events"] = [e for e in self.session["events"] if e.get("message_id") != event["message_id"]]
            elif event["type"] == "part_remove":
                self.session["events"] = [e for e in self.session["events"] if e.get("id") != event["id"]]
            if self.json_mode:
                chat_ui.emit(event)
            elif event["type"] == "message_part":
                text, key = event.get("text", ""), event["id"]
                previous = self.last_text.get(key, "")
                if text.startswith(previous):
                    print(text[len(previous):], end="", flush=True)
                self.last_text[key] = text
            elif event["type"] not in ("tool_part", "catalog", "history_v2"):
                print("\n" + (event.get("text") or event.get("status") or event["type"]), flush=True)

    def pump(self):
        try:
            for raw in sys.stdin:
                if len(raw) > 1024 * 1024:
                    self.emit({"type": "error", "text": "Input exceeds 1 MB."})
                    continue
                try:
                    value = json.loads(raw) if raw.lstrip().startswith("{") else {"type": "user", "text": raw.rstrip("\r\n")}
                except ValueError:
                    value = {"type": "user", "text": raw.rstrip("\r\n")}
                if not isinstance(value, dict):
                    continue
                if value.get("type") == "cancel":
                    if self.task:
                        self.task.cancelled.set()
                        self.emit({"type": "task_status", "status": "stopping"})
                    else:
                        self.emit({"type": "ack", "command": "stop", "text": "No active task."})
                    # Stop also discards queued requests, so cleanup cannot start another model call.
                    while not self.inputs.empty():
                        try:
                            self.inputs.get_nowait()
                        except queue.Empty:
                            break
                    continue
                if value.get("type") == "answer":
                    with self.lock:
                        waiter = self.pending.get(value.get("request_id", ""))
                    if waiter:
                        waiter.put(value.get("answers", value.get("allow", False)))
                    continue
                self.inputs.put(value)
                if self.task:
                    self.emit({"type": "notice", "text": "Message queued for the next turn. Use Stop to interrupt this task first."})
        finally:
            self.closed = True
            if self.task:
                self.task.cancelled.set()
            self.inputs.put(None)

    def ask(self, request):
        if self.auto_yes and request.get("tool") not in ("paid model",):
            # Only explicit --yes enables this. Headless no longer silently grants it.
            return request["type"] != "question_request"
        if not self.json_mode:
            if request["type"] == "question_request":
                return [[input(q["question"] + " ")] for q in request["questions"]]
            return input(request.get("detail", "Proceed?") + " [y/N] ").strip().lower() in ("y", "yes")
        key = request["request_id"]
        waiter: queue.Queue = queue.Queue()
        with self.lock:
            self.pending[key] = waiter
        try:
            self.emit(request)
            while not self.closed and not (self.task and self.task.cancelled.is_set()):
                try:
                    return waiter.get(timeout=.2)
                except queue.Empty:
                    pass
            return False
        finally:
            with self.lock:
                self.pending.pop(key, None)
            self.emit({"type": "approval_resolved", "request_id": key})

    def command(self, text: str):
        name, _, value = text[1:].partition(" ")
        value = value.strip()
        reply = {"type": "ack", "command": name}
        if name in ("mode", "plan"):
            mode = value if name == "mode" else ("ask" if value == "off" else "plan")
            if mode not in ("ask", "plan", "build"):
                raise ValueError("Use /mode ask|plan|build.")
            self.session["mode"] = mode
            reply.update(mode=mode, text=f"Mode: {mode}")
        elif name == "role":
            if value not in ROLES:
                raise ValueError("Unknown role: " + value)
            self.session["role"] = value
            reply.update(role=value, text=f"Role: {value}")
        elif name in ("provider", "model"):
            provider = value if name == "provider" else self.session["provider"]
            model = value if name == "model" else ""
            model = opencode_config.resolve(self.cfg, provider, model)["model"]
            self.session.update(provider=provider, model=model)
            reply.update(provider=provider, model=model)
        elif name == "verify":
            if value not in ("off", "gate", "tests"):
                raise ValueError("Use /verify off|gate|tests.")
            self.session["verify"] = value
            reply.update(verify=value)
        elif name == "undo":
            deliveries = self.session.get("deliveries", [])
            if deliveries:
                manifest = Path(deliveries[-1]).resolve()
                if not manifest.is_relative_to(paths.OUTPUTS_DIR.resolve()):
                    raise ValueError("Invalid task artifact location.")
                reply["reverted"] = workspace.undo(manifest, self.session["repo"])
                deliveries.pop()
        elif name == "files":
            changed = set()
            for event in self.session["events"]:
                if event["type"] == "delivery" and event.get("applied"):
                    changed.update(event.get("files_changed", []))
                elif event["type"] == "ack":
                    changed.difference_update(event.get("reverted", []))
            reply["files_changed"] = sorted(changed)
        elif name == "cost":
            reply["usage"] = self.session["usage"]
        elif name == "save":
            reply["transcript"] = str(save(self.session))
        else:
            reply["text"] = "/mode ask|plan|build, /role general|explorer|implementer|reviewer, /provider, /model, /verify, /undo, /files, /cost, /save, /quit"
        self.emit(reply)

    def settings(self, values: dict):
        provider = values.get("provider", self.session["provider"])
        model = values.get("model", "" if provider != self.session["provider"] else self.session["model"])
        selected = opencode_config.resolve(self.cfg, provider, model)
        mode, role = values.get("mode", self.session["mode"]), values.get("role", self.session["role"])
        if mode not in ("ask", "plan", "build") or role not in ROLES:
            raise ValueError("Unknown mode or role.")
        # Validate the whole selection before changing any field.
        selection = dict(provider=provider, model=selected["model"], mode=mode, role=role)
        self.session.update(selection)
        self.emit({"type": "ack", "command": "settings", **selection})

    def turn(self, text: str, mode: str | None = None):
        if not self.session["first_task"]:
            self.session["first_task"] = text[:80]
        self.emit({"type": "user", "text": text})
        self.task = Task(self.cfg, self.emit, self.ask)
        try:
            effective_mode = mode or self.session["mode"]
            if self.session["role"] in ("explorer", "reviewer"):
                effective_mode = "ask"
            result = self.task.run(self.session, text, mode=effective_mode)
            if effective_mode == "build":
                self.emit({"type": "delivery", **result, "answer": ""})
                self.emit({"type": "notice", "text": ("Changes applied (verification was off)." if result.get("check") == "not checked" else "Verified changes applied.") if result["applied"] else
                           (result.get("apply_error") or "No changes applied. Candidate patch retained for review.")})
            return result
        except (Exception, KeyboardInterrupt) as exc:
            if isinstance(exc, KeyboardInterrupt):
                self.task.cancel()
            self.emit({"type": "stopped" if isinstance(exc, (Cancelled, KeyboardInterrupt)) else "error", "text": str(exc) or "Stopped."})
            return {"done_passed": False, "files_changed": [], "applied": False}
        finally:
            self.task = None
            save(self.session)
            self.emit({"type": "turn_end", "usage": self.session["usage"]})


def chat(repo, cfg, provider, model="", verify="", test_cmd="", auto_yes=False,
         allow_dirty=False, resume="", json_mode=False, plan_mode=False, host_tools=False,
         mode="", role="general"):
    del allow_dirty, host_tools  # Task worktrees preserve dirty source files by design.
    session = new_session(repo, cfg, provider, model, verify, test_cmd, resume)
    if mode or plan_mode:
        session["mode"] = mode or "plan"
    if role != "general" or not resume:
        session["role"] = role
    ui = UI(session, cfg, json_mode, auto_yes)
    ui.emit({"type": "session_start", "protocol_version": 2, **{k: session[k] for k in ("repo", "provider", "model", "verify", "test_cmd", "mode", "role")}})
    ui.emit({"type": "catalog", "models": opencode_config.catalog(cfg), "roles": ROLES})
    if resume:
        ui.emit({"type": "history_v2", "events": list(session["events"])})
    if json_mode:
        threading.Thread(target=ui.pump, daemon=True).start()
    while True:
        try:
            item = ui.inputs.get() if json_mode else {"text": input("you > ")}
            if item is None:
                break
            text = str(item.get("text", "")).strip()
            if text in ("/quit", "/exit"):
                break
            if not text:
                if item.get("type") == "settings":
                    ui.settings({k: item[k] for k in ("provider", "model", "mode", "role") if isinstance(item.get(k), str)})
                    save(session)
                continue
            if text.startswith("/"):
                ui.command(text)
                save(session)
            else:
                session["editor_diagnostics"] = str(item.get("diagnostics", ""))[:6000]
                ui.turn(text)
        except (EOFError, KeyboardInterrupt):
            break
        except Exception as exc:
            ui.emit({"type": "error", "text": str(exc)})
    save(session)
    ui.emit({"type": "session_end", "usage": session["usage"], "files_changed": [], "transcript": str(save(session))})
    return 0


def run_headless(repo, cfg, task, done_when, provider, model="", verify="", test_cmd="",
                 json_mode=False, host_tools=False, auto_yes=False):
    session = new_session(repo, cfg, provider, model, verify or "tests", done_when)
    ui = UI(session, cfg, json_mode, auto_yes)
    ui.emit({"type": "session_start", "protocol_version": 2, "mode": "headless", **{k: session[k] for k in ("repo", "provider", "model", "verify", "test_cmd")}})
    if json_mode:
        threading.Thread(target=ui.pump, daemon=True).start()
    result = ui.turn(task, "build")
    ui.emit({"type": "session_end", "usage": session["usage"], "transcript": str(save(session)), **result})
    return 0 if result["done_passed"] and not result.get("apply_error") else 1
