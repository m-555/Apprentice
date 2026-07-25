"""The agent loop — model ↔ tools until the work is done and verified.

One engine, two entry points:
  • `run_turn(...)`   interactive: one user message → the agent works until it calls
    finish (or hits a cap). Yields events so a UI can show progress live.
  • `run_headless(...)` unattended: a task + a `done_when` command, run to green. This is
    the `assign` contract, so the same engine can replace the Aider-based agent later.

Everything expensive or dangerous is bounded: step count, wall clock, and the provider's
daily token/USD budget. When the model repeatedly fails verification, the loop escalates
to the next tier (config `cascade.escalate_to`) — a stronger model gets the failing state
and the verbatim error, exactly like the delegate cascade does.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator

try:
    from . import (budgets, chat_providers, corrections, metering, tools as tools_mod,
                   verify as verify_mod)
except ImportError:
    import budgets
    import chat_providers
    import corrections
    import metering
    import tools as tools_mod
    import verify as verify_mod


@dataclass
class Event:
    """Something the UI may want to show. `kind` is one of:
    text, tool_call, tool_result, verify_failed, verify_passed, escalated, stopped."""
    kind: str
    text: str = ""
    name: str = ""
    args: dict[str, Any] | None = None


def _record_usage(session, usage: dict[str, Any], provider: str, model: str) -> None:
    cost = metering.est_cost_usd(session.cfg, provider, model,
                                 int(usage.get("tokens_in", 0) or 0),
                                 int(usage.get("tokens_out", 0) or 0))
    session.usage["tokens_in"] += int(usage.get("tokens_in", 0) or 0)
    session.usage["tokens_out"] += int(usage.get("tokens_out", 0) or 0)
    session.usage["est_cost_usd"] = round(session.usage["est_cost_usd"] + cost, 6)
    session.usage["turns"] += 1
    metering.record({"tier": provider, "model": model, "mode": "agent",
                     **{k: usage.get(k, 0) for k in ("tokens_in", "tokens_out",
                                                     "duration_s")}}, session.cfg)


def run_turn(session, verifier: verify_mod.Verifier, registry: dict[str, tools_mod.Tool],
             user_text: str | None = None) -> Iterator[Event]:
    """Drive one user request to completion. Yields Events; mutates `session` in place."""
    cfg = session.cfg
    chat_cfg = cfg.get("agent_chat", {})
    max_steps = int(chat_cfg.get("max_steps", 40))
    deadline = time.monotonic() + int(chat_cfg.get("max_seconds", 1800))
    escalate_after = int(chat_cfg.get("escalate_after_failed_verifies", 2))

    if user_text is not None:
        session.add_user(user_text)

    schemas = tools_mod.schemas(registry)
    failed_verifies = 0

    for step in range(1, max_steps + 1):
        if time.monotonic() > deadline:
            yield Event("stopped", "Time limit reached — stopping. Ask me to continue.")
            return
        blocked = budgets.exceeded(cfg, session.provider)
        if blocked:
            yield Event("stopped", blocked)
            return

        session.maybe_compact()
        usage: dict[str, Any] = {}
        try:
            turn = chat_providers.chat(session.messages, schemas, cfg, session.provider,
                                       session.model, usage)
        except Exception as exc:                      # provider/network failure
            yield Event("stopped", f"Provider error: {exc}")
            return
        _record_usage(session, usage, session.provider, session.model)
        session.add_assistant(turn)

        if turn.content:
            yield Event("text", turn.content)

        if not turn.wants_tools:
            # No tool call and nothing left to do: the model is answering/finished.
            result = verifier.finish_turn()
            if not result.ok:
                failed_verifies += 1
                yield Event("verify_failed", result.error_text, name=result.check)
                session.add_system_note(result.as_tool_note())
                if failed_verifies >= escalate_after:
                    if (yield from _try_escalate(session, cfg)):
                        failed_verifies = 0
                continue
            return

        done = False
        for call in turn.tool_calls:
            yield Event("tool_call", name=call.name, args=call.args)
            result = tools_mod.dispatch(registry, call.name, call.args)
            session.add_tool_result(call, result)
            yield Event("tool_result", result, name=call.name)
            if call.name == "finish":
                done = True

        if done:
            outcome = verifier.finish_turn()
            if outcome.ok:
                yield Event("verify_passed", outcome.check or "none")
                return
            failed_verifies += 1
            yield Event("verify_failed", outcome.error_text, name=outcome.check)
            session.add_system_note(outcome.as_tool_note())
            if failed_verifies >= escalate_after:
                if (yield from _try_escalate(session, cfg)):
                    failed_verifies = 0

    yield Event("stopped", f"Step limit ({max_steps}) reached — stopping.")


def _try_escalate(session, cfg: dict[str, Any]) -> Iterator[Event]:
    """Switch the session to the next tier after repeated verification failures.
    Same guards as the delegate cascade: a different, enabled, under-budget provider.
    Returns True (via StopIteration value) if the switch happened."""
    esc = cfg.get("cascade", {}).get("escalate_to", "")
    if (not esc or esc == session.provider
            or not chat_providers.supports_chat(cfg, esc)
            or not cfg.get("providers", {}).get(esc, {}).get("enabled", False)
            or budgets.exceeded(cfg, esc)):
        return False
    old = session.provider
    session.provider, session.model = esc, ""
    yield Event("escalated", f"{old} kept failing verification — switching to '{esc}' "
                             f"for the rest of this task.")
    session.add_system_note(
        f"[A stronger model ({esc}) has taken over after repeated failures. Re-read the "
        f"relevant files before editing — do not assume the previous attempts were right.]")
    return True


def run_headless(session, verifier: verify_mod.Verifier,
                 registry: dict[str, tools_mod.Tool], task: str, done_when: str,
                 on_event: Callable[[Event], None] | None = None) -> dict[str, Any]:
    """Unattended mode: grind `task` until `done_when` exits 0 (the `assign` contract).

    Verification runs per turn as usual; `done_when` is the final objective gate. Returns
    a small summary — never the full diff — so an orchestrator stays cheap.
    """
    try:
        from . import deliver
    except ImportError:
        import deliver

    cfg = session.cfg
    max_rounds = int(cfg.get("agent_chat", {}).get("headless_max_rounds", 3))
    timeout = int(cfg.get("agent_chat", {}).get("test_timeout_s", 600))
    message = task
    done_passed, rounds, log = False, 0, ""

    for rounds in range(1, max_rounds + 1):
        for ev in run_turn(session, verifier, registry, message):
            if on_event:
                on_event(ev)
        rc, log = deliver.run_test_cmd(session.repo, done_when, timeout)
        if rc == 0:
            done_passed = True
            break
        message = (f"The acceptance check `{done_when}` still FAILS. Fix the code so it "
                   f"passes. Verbatim output:\n{log[-3000:]}")

    if done_passed and rounds > 1:
        corrections.write(corrections.machine_verified(
            provider=session.provider, role="agent", task=task, before="", after="",
            explanation=(f"Agent needed {rounds} rounds to satisfy `{done_when}`; "
                         f"the failure output was fed back each round."),
        ), cfg)

    return {"done_passed": done_passed, "rounds": rounds,
            "files_changed": sorted({verifier._rel(p) for snaps in verifier.history
                                     for p in (s.path for s in snaps)}),
            "usage": session.usage, "session_id": session.id,
            "done_log_tail": "\n".join(log.splitlines()[-15:])}
