"""The terminal REPL for `apprentice chat` — and the headless `apprentice run`.

Thin on purpose: all the behavior lives in loop/tools/verify/session. This module only
renders events, asks for confirmations, and handles slash commands.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from . import (chat_providers, deliver, loop, paths, session as session_mod,
                   tools as tools_mod, verify as verify_mod)
except ImportError:
    import chat_providers
    import deliver
    import loop
    import paths
    import session as session_mod
    import tools as tools_mod
    import verify as verify_mod

HELP = """Commands:
  /undo               revert the agent's last completed turn
  /verify off|gate|tests   change the verification policy
  /plan [off]         plan-then-approve before the agent may edit anything
  /provider <name>    switch model provider (e.g. qwen, gemini)  [/model <tier>]
  /cost               tokens + estimated spend for this session
  /files              files changed so far this session
  /save               write the transcript now (also saved on exit)
  /help  /quit"""


# A legacy Windows console is cp1252: model output (or a stray glyph) would raise
# UnicodeEncodeError mid-session. Switch stdout/stderr to UTF-8 where the runtime allows
# it, and keep _out()'s fallback correct for the cases where it doesn't.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, OSError, ValueError):
        pass


def _out(text: str = "") -> None:
    """Print without dying on a legacy Windows console codepage."""
    try:
        print(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(enc, "replace").decode(enc, "replace"))


def emit(obj: dict[str, Any]) -> None:
    """Write one protocol event as a JSON line (`--json` mode).

    One object per line, flushed immediately, so a frontend can stream it. Every event
    has `type` and `ts`; see docs/AGENT.md for the schema.
    """
    obj = {"ts": datetime.now(timezone.utc).isoformat(), **obj}
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def _render_json(ev: loop.Event) -> None:
    emit(ev.to_dict())


_EOF = object()   # sentinel: stdin closed


class StdinBroker:
    """Single owner of stdin for `--json` mode.

    Three things now arrive on stdin — user messages, answers to confirm/ask prompts,
    and replies to host requests — and they can interleave: a frontend may send "actually,
    do X instead" while the model is mid-turn. Reading stdin from several places would
    race, so one background thread reads every line and routes it:

      * a JSON object with `id` → a reply to that host request
      * anything else, while an answer is being awaited → that answer
      * anything else, otherwise → a STEERING message for the running turn

    Only used in JSON mode; the terminal REPL keeps plain blocking `input()`, since you
    can't usefully type mid-turn there anyway.
    """

    def __init__(self) -> None:
        self._answers: "queue.Queue[str]" = queue.Queue()
        self._steer: "queue.Queue[str]" = queue.Queue()
        self._host: dict[str, "queue.Queue[str]"] = {}
        self._awaiting = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        try:
            for raw in sys.stdin:
                line = raw.strip()
                if not line:
                    continue
                if line.startswith("{"):
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        obj = None
                    if isinstance(obj, dict) and obj.get("id"):
                        with self._lock:
                            q = self._host.pop(obj["id"], None)
                        if q is not None:
                            q.put(str(obj.get("result", "")))
                            continue
                (self._answers if self._awaiting.is_set() else self._steer).put(line)
        finally:
            # stdin closed: waiters must be released, or the session hangs forever
            # (plain input() used to raise EOFError here and callers rely on that).
            self._answers.put(_EOF)

    def ask_line(self, timeout: float | None = None, *,
                 accept_pending: bool = False) -> str:
        """Block for the user's answer to a prompt we just emitted.
        Raises EOFError when stdin has closed, mirroring `input()`.

        `accept_pending` also takes a line that arrived BEFORE we started awaiting.
        Lines are routed at arrival time, so anything sent while no prompt was open sits
        in the steering queue — including the very first message from a frontend that
        writes it straight after spawn, before Python has reached its first prompt (the
        VS Code panel does exactly that). With no turn running to drain steering, that
        line was stranded and the session blocked forever. Only the top-level chat
        prompt sets this: between turns there is nothing to steer, so an early line is
        plainly the user's message. Confirm prompts leave it False — a stale steering
        line must never stand in for a y/n answer.
        """
        self._awaiting.set()
        try:
            if accept_pending:
                try:
                    return self._steer.get_nowait()
                except queue.Empty:
                    pass
            value = self._answers.get(timeout=timeout)
        except queue.Empty:
            return ""
        finally:
            self._awaiting.clear()
        if value is _EOF:
            self._answers.put(_EOF)      # stay closed for every later caller
            raise EOFError("stdin closed")
        return value

    def take_steering(self) -> str:
        """Any message the user sent while the agent was working ("" if none)."""
        parts = []
        while True:
            try:
                parts.append(self._steer.get_nowait())
            except queue.Empty:
                break
        return "\n".join(parts)

    def host_request(self, kind: str, payload: dict[str, Any],
                     timeout: float = 20.0) -> str:
        """Ask the frontend for something only it can know; wait for its reply."""
        req_id = uuid.uuid4().hex[:8]
        q: "queue.Queue[str]" = queue.Queue()
        with self._lock:
            self._host[req_id] = q
        emit({"type": "host_request", "id": req_id, "kind": kind, **payload})
        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            with self._lock:
                self._host.pop(req_id, None)
            raise TimeoutError(f"the editor did not answer the {kind} request in time")


def _make_renderers(json_mode: bool, stream: bool):
    """Return (render_event, on_delta).

    They are built together because they must agree: in the terminal, text that was
    already streamed character-by-character must NOT be printed again when the
    complete `text` event arrives.
    """
    if json_mode:
        on_delta = (lambda piece: emit({"type": "text_delta", "text": piece})) \
            if stream else None
        return _render_json, on_delta

    if not stream:
        return _render, None

    state = {"streamed": False}

    def on_delta(piece: str) -> None:
        if not state["streamed"]:
            state["streamed"] = True
            print()                      # break away from the "you >" prompt line
        try:
            sys.stdout.write(piece)
            sys.stdout.flush()
        except UnicodeEncodeError:
            enc = getattr(sys.stdout, "encoding", None) or "ascii"
            sys.stdout.write(piece.encode(enc, "replace").decode(enc, "replace"))
            sys.stdout.flush()

    def render(ev: loop.Event) -> None:
        if ev.kind == "text" and state["streamed"]:
            state["streamed"] = False    # already shown live; just close the line
            _out()
            return
        _render(ev)

    return render, on_delta


def _git_state(repo: str) -> tuple[bool, bool]:
    """(is_git_repo, is_dirty)."""
    r = subprocess.run(["git", "-C", repo, "status", "--porcelain"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False, False
    return True, bool((r.stdout or "").strip())


def _resolve_test_cmd(repo: str, cfg: dict[str, Any], override: str = "") -> str:
    """Precedence: --test-cmd > <repo>/.qwen-pipeline.json test_cmd > agent_chat.test_cmd."""
    if override:
        return override
    repo_opts = deliver.load_repo_options(repo)
    return str(repo_opts.get("test_cmd")
               or cfg.get("agent_chat", {}).get("test_cmd", "") or "")


def _confirmer(auto_yes: bool, json_mode: bool = False, broker=None):
    """Approval gate for shell commands.

    In `--json` mode there is no prompt to render, so the protocol asks instead: we emit
    a `confirm_request` event and read ONE line from stdin as the answer (`y`/`yes`/
    `true`, or a JSON object with `"allow": true`). Anything else — including EOF — is a
    refusal, so an unattended frontend fails safe.
    """
    def confirm(tool_name: str, detail: str) -> bool:
        if auto_yes:
            if json_mode:
                emit({"type": "confirm_auto", "tool": tool_name, "detail": detail})
            else:
                _out(f"  [auto-approved] {detail}")
            return True
        if json_mode:
            emit({"type": "confirm_request", "tool": tool_name, "detail": detail})
        else:
            _out(f"\n  [CONFIRM] The agent wants to run: {detail}")
        try:
            answer = (broker.ask_line() if (json_mode and broker)
                      else input("" if json_mode else "  Allow? [y/N] ").strip())
        except (EOFError, KeyboardInterrupt):
            return False
        if json_mode and answer.startswith("{"):
            try:
                return bool(json.loads(answer).get("allow", False))
            except json.JSONDecodeError:
                return False
        return answer.lower() in ("y", "yes", "true")
    return confirm


PLAN_INSTRUCTION = (
    "PLAN FIRST — do not edit anything yet (your editing tools are disabled for this "
    "turn). Investigate with read_file/search/list_files, then reply with a short, "
    "concrete plan: which files you will change and what each change does. Number the "
    "steps. Do not write code in the plan.\n\n--- THE REQUEST ---\n")

EXECUTE_INSTRUCTION = (
    "The plan is approved. Carry it out now, exactly as described. Re-read any file "
    "before you edit it, then run the tests.")


def _asker(auto_yes: bool, json_mode: bool, broker=None):
    """Yes/no question for decisions that cost money or change how the run behaves
    (currently: climbing the escalation ladder, approving a plan).

    Same wire protocol as tool approval — a JSON frontend gets an event and answers with
    one stdin line — so a UI only has to implement one pattern.
    """
    def ask(question: str, detail: str = "") -> bool:
        if auto_yes:
            return True
        if json_mode:
            emit({"type": "ask", "question": question, "detail": detail})
        else:
            _out(f"\n  [ASK] {question}")
        try:
            answer = (broker.ask_line() if (json_mode and broker)
                      else input("" if json_mode else "  Proceed? [y/N] ").strip())
        except (EOFError, KeyboardInterrupt):
            return False
        if json_mode and answer.startswith("{"):
            try:
                return bool(json.loads(answer).get("allow", False))
            except json.JSONDecodeError:
                return False
        return answer.lower() in ("y", "yes", "true")
    return ask


def _render(ev: loop.Event) -> None:
    if ev.kind == "text":
        _out(f"\n{ev.text}")
    elif ev.kind == "tool_call":
        detail = ""
        if ev.args:
            for key in ("path", "cmd", "pattern", "dir", "summary"):
                if key in ev.args:
                    detail = str(ev.args[key])[:100]
                    break
        _out(f"  -> {ev.name}({detail})")
    elif ev.kind == "tool_result":
        first = (ev.text or "").strip().splitlines()
        if first:
            _out(f"    {first[0][:160]}")
    elif ev.kind == "verify_failed":
        _out(f"\n  [FAILED] verification ({ev.name}) - change REVERTED, agent retrying")
    elif ev.kind == "verify_passed":
        _out(f"\n  [OK] verified ({ev.text})")
    elif ev.kind == "escalated":
        _out(f"\n  [ESCALATED] {ev.text}")
    elif ev.kind == "escalation_offer":
        _out(f"\n  [ESCALATE?] {ev.text}")
    elif ev.kind == "nudge":
        _out(f"  [NUDGE] {ev.text} - told the model to change approach")
    elif ev.kind == "stopped":
        _out(f"\n  [STOPPED] {ev.text}")


def _make_runtime(sess, cfg: dict[str, Any], auto_yes: bool, json_mode: bool = False,
                  broker=None, host_tools: bool = False):
    """Build the verifier + verification-wrapped tool registry for a session."""
    verifier = verify_mod.Verifier(sess.repo, cfg, sess.verify_policy, sess.test_cmd)
    # `host` is only wired when a frontend advertised it (--host-tools): the editor is
    # the only thing that can answer a diagnostics request.
    host = (broker.host_request if (host_tools and broker) else None)
    ctx = tools_mod.ToolContext(repo=sess.repo, cfg=cfg, test_cmd=sess.test_cmd,
                                confirm=_confirmer(auto_yes, json_mode, broker),
                                session=sess, host=host)
    registry = verify_mod.wrap_registry(tools_mod.build_tools(ctx), verifier)
    return verifier, registry


def _session_start_event(sess, verifier) -> dict[str, Any]:
    return {"type": "session_start", "session_id": sess.id, "repo": sess.repo,
            "provider": sess.provider, "model": sess.model,
            "verify": verifier.policy, "test_cmd": sess.test_cmd}


def _history_event(sess, max_text: int = 4000) -> dict[str, Any]:
    """The conversation so far, flattened for a UI to re-render after `--resume`.

    A resumed session used to come up BLANK: the agent held the full history, but the
    protocol had no way to say what it was, so a frontend that cleared its transcript on
    resume had nothing to draw. Tool results are trimmed hard — this is for showing a
    human where they left off, not for reconstructing the model's exact context.
    """
    items: list[dict[str, Any]] = []
    for m in sess.messages:
        role = m.get("role")
        if role == "system":
            continue
        if role == "user":
            items.append({"role": "user",
                          "text": loop.user_text_for_display(m.get("content") or "")})
        elif role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "text": m.get("content") or ""}
            tools = [tc.get("function", {}).get("name", "")
                     for tc in (m.get("tool_calls") or [])]
            if tools:
                entry["tools"] = [t for t in tools if t]
            if entry["text"] or entry.get("tools"):
                items.append(entry)
        elif role == "tool":
            items.append({"role": "tool", "tool": m.get("name") or "",
                          "text": (m.get("content") or "")[:400]})
    total = 0
    for item in reversed(items):                 # keep the RECENT end under the cap
        total += len(item.get("text") or "")
        if total > max_text:
            items = items[items.index(item) + 1:]
            break
    return {"type": "history", "session_id": sess.id, "messages": items}


def _session_end_event(sess, verifier, extra: dict[str, Any] | None = None
                       ) -> dict[str, Any]:
    files = sorted({verifier._rel(s.path) for snaps in verifier.history for s in snaps})
    return {"type": "session_end", "session_id": sess.id, "files_changed": files,
            "usage": sess.usage, "transcript": str(sess.transcript_path()),
            **(extra or {})}


def chat(repo: str, cfg: dict[str, Any], provider: str, model: str = "",
         verify: str = "", test_cmd: str = "", auto_yes: bool = False,
         allow_dirty: bool = False, resume: str = "", json_mode: bool = False,
         plan_mode: bool = False, host_tools: bool = False) -> int:
    repo = str(Path(repo).resolve())
    chat_cfg = cfg.get("agent_chat", {})

    def fail(text: str) -> int:
        emit({"type": "error", "text": text}) if json_mode else _out(text)
        return 2

    if not chat_providers.supports_chat(cfg, provider):
        return fail(f"Provider '{provider}' can't run the agent (no chat/tool support "
                    f"for its kind). Configure providers.{provider}.kind, or pick "
                    f"another provider.")

    is_git, dirty = _git_state(repo)
    if chat_cfg.get("require_clean_git", True) and not allow_dirty:
        if not is_git:
            return fail(f"{repo} is not a git repository. The agent edits files in "
                        f"place — git is your undo. Run `git init`, or pass "
                        f"--allow-dirty to proceed anyway.")
        if dirty:
            return fail("Your working tree has uncommitted changes. Commit or stash "
                        "them first so you can tell the agent's edits from your own "
                        "(or --allow-dirty).")

    if resume:
        sess = session_mod.Session.load(resume, cfg)
        if not json_mode:
            _out(f"Resumed session {sess.id} ({len(sess.messages)} messages).")
    else:
        sess = session_mod.Session(
            repo, cfg, provider, model,
            verify or chat_cfg.get("verify", "tests"),
            _resolve_test_cmd(repo, cfg, test_cmd))

    broker = StdinBroker() if json_mode else None
    verifier, registry = _make_runtime(sess, cfg, auto_yes, json_mode, broker, host_tools)
    render, on_delta = _make_renderers(
        json_mode, bool(chat_cfg.get("stream", True)))
    ask = _asker(auto_yes, json_mode, broker)
    steering = broker.take_steering if broker else None
    ro_registry = tools_mod.readonly(registry)

    if json_mode:
        emit({**_session_start_event(sess, verifier), "resumed": bool(resume)})
        if resume:
            emit(_history_event(sess))
    else:
        _out(f"\nApprentice agent | repo={repo}")
        _out(f"provider={sess.provider}{('/' + sess.model) if sess.model else ''} | "
             f"verify={verifier.policy}"
             f"{(' | tests=' + sess.test_cmd) if sess.test_cmd else ' | (no test command)'}")
        if verifier.policy == "off":
            _out("verification is OFF — edits land immediately, nothing is checked.")
        _out("Describe what you want. /help for commands, /quit to exit.\n")

    while True:
        try:
            line = (broker.ask_line(accept_pending=True) if broker
                    else input("you > ")).strip()
        except (EOFError, KeyboardInterrupt):
            if not json_mode:
                _out()
            break
        if not line:
            continue

        if line.startswith("/"):
            cmd, _, arg = line[1:].partition(" ")
            arg = arg.strip()

            def reply(text: str, **fields: Any) -> None:
                """A command's answer: an `ack` event in JSON mode, else plain text."""
                if json_mode:
                    emit({"type": "ack", "command": cmd, **fields})
                else:
                    _out(text)

            if cmd in ("quit", "exit", "q"):
                break
            if cmd == "help":
                reply(HELP, commands=["undo", "verify", "provider", "model", "cost",
                                      "files", "save", "help", "quit"])
            elif cmd == "undo":
                restored = verifier.undo_last()
                reply(f"  reverted: {', '.join(restored)}" if restored
                      else "  nothing to undo", reverted=restored)
            elif cmd == "verify":
                if arg in verify_mod.POLICIES:
                    sess.verify_policy = arg
                    verifier, registry = _make_runtime(sess, cfg, auto_yes, json_mode,
                                                      broker, host_tools)
                    reply(f"  verification = {verifier.policy}", verify=verifier.policy)
                else:
                    reply(f"  usage: /verify {'|'.join(verify_mod.POLICIES)}",
                          error=f"unknown policy {arg!r}")
            elif cmd == "plan":
                plan_mode = arg.lower() not in ("off", "false", "0")
                reply(f"  plan mode = {'on' if plan_mode else 'off'}",
                      plan=plan_mode)
            elif cmd == "provider":
                if chat_providers.supports_chat(cfg, arg):
                    sess.provider, sess.model = arg, ""
                    reply(f"  provider = {arg}", provider=arg)
                else:
                    reply(f"  unknown/unsupported provider: {arg!r}",
                          error=f"unsupported provider {arg!r}")
            elif cmd == "model":
                sess.model = arg
                reply(f"  model = {arg or '(provider default)'}", model=arg)
            elif cmd == "cost":
                u = sess.usage
                reply(f"  turns={u['turns']} tokens in/out={u['tokens_in']}/"
                      f"{u['tokens_out']} est_cost=${u['est_cost_usd']:.4f}", usage=u)
            elif cmd == "files":
                changed = sorted({verifier._rel(s.path) for snaps in verifier.history
                                  for s in snaps})
                reply("  " + (", ".join(changed) if changed else "(none yet)"),
                      files_changed=changed)
            elif cmd == "save":
                reply(f"  saved -> {sess.save()}", transcript=str(sess.save()))
            else:
                reply(f"  unknown command {line!r}. /help for the list.",
                      error=f"unknown command {cmd!r}")
            continue

        if json_mode:
            emit({"type": "user", "text": line})
        try:
            if plan_mode:
                # Phase 1: investigate and propose, with editing tools REMOVED.
                for ev in loop.run_turn(sess, verifier, ro_registry,
                                        PLAN_INSTRUCTION + line, on_delta, ask,
                                        steering):
                    render(ev)
                if not ask("Execute this plan?", ""):
                    render(loop.Event("stopped", "Plan not executed."))
                    sess.save()
                    continue
                line = EXECUTE_INSTRUCTION
            for ev in loop.run_turn(sess, verifier, registry, line, on_delta, ask,
                                    steering):
                render(ev)
        except KeyboardInterrupt:
            render(loop.Event("stopped", "interrupted"))
        sess.save()
        if json_mode:
            emit({"type": "turn_end", "usage": sess.usage})
        else:
            _out()

    path = sess.save()
    u = sess.usage
    if json_mode:
        emit(_session_end_event(sess, verifier))
    else:
        _out(f"Session {sess.id} saved -> {path}")
        _out(f"turns={u['turns']} tokens in/out={u['tokens_in']}/{u['tokens_out']} "
             f"est_cost=${u['est_cost_usd']:.4f}   resume with: "
             f"apprentice chat --resume {sess.id}")
    return 0


def run_headless(repo: str, cfg: dict[str, Any], task: str, done_when: str,
                 provider: str, model: str = "", verify: str = "",
                 test_cmd: str = "", json_mode: bool = False,
                 host_tools: bool = False) -> int:
    repo = str(Path(repo).resolve())
    if not chat_providers.supports_chat(cfg, provider):
        msg = f"Provider '{provider}' can't run the agent."
        emit({"type": "error", "text": msg}) if json_mode else _out(msg)
        return 2
    sess = session_mod.Session(
        repo, cfg, provider, model,
        verify or cfg.get("agent_chat", {}).get("verify", "tests"),
        _resolve_test_cmd(repo, cfg, test_cmd))
    # Headless auto-approves everything and nobody steers it, so stdin is only worth
    # owning when the frontend can answer host requests.
    broker = StdinBroker() if (json_mode and host_tools) else None
    verifier, registry = _make_runtime(sess, cfg, auto_yes=True, json_mode=json_mode,
                                       broker=broker, host_tools=host_tools)
    render, on_delta = _make_renderers(
        json_mode, bool(cfg.get("agent_chat", {}).get("stream", True)))

    if json_mode:
        emit({**_session_start_event(sess, verifier), "mode": "headless",
              "task": task, "done_when": done_when})
    else:
        _out(f"Apprentice headless | repo={repo} | provider={sess.provider} | "
             f"done_when={done_when}")

    result = loop.run_headless(sess, verifier, registry, task, done_when, render,
                               on_delta, _asker(True, json_mode, broker),
                               broker.take_steering if broker else None)
    sess.save()

    if json_mode:
        emit(_session_end_event(sess, verifier, {
            "done_passed": result["done_passed"], "rounds": result["rounds"],
            "files_changed": result["files_changed"],
            "done_log_tail": result["done_log_tail"]}))
    else:
        _out(f"\ndone_passed={result['done_passed']} rounds={result['rounds']} "
             f"files_changed={result['files_changed']}")
        _out(f"tokens in/out={sess.usage['tokens_in']}/{sess.usage['tokens_out']} "
             f"est_cost=${sess.usage['est_cost_usd']:.4f} | session {sess.id}")
        if not result["done_passed"]:
            _out(f"last output:\n{result['done_log_tail']}")
    return 0 if result["done_passed"] else 1
