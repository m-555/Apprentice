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

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator

try:
    from . import (budgets, chat_providers, corrections, metering, retrieval,
                   tools as tools_mod, verify as verify_mod)
except ImportError:
    import budgets
    import chat_providers
    import corrections
    import metering
    import retrieval
    import tools as tools_mod
    import verify as verify_mod


@dataclass
class Event:
    """Something a UI may want to show. `kind` is one of:
    text, tool_call, tool_result, verify_failed, verify_passed, escalated, stopped.

    `to_dict()` is the WIRE FORMAT for `--json` mode — a stable protocol any frontend
    (VS Code extension, web UI, CI script) can consume. Keep it additive: add fields,
    never rename or repurpose existing ones.
    """
    kind: str
    text: str = ""
    name: str = ""
    args: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.kind}
        if self.kind in ("tool_call", "tool_result"):
            out["tool"] = self.name
        elif self.kind in ("verify_failed", "verify_passed"):
            # verify_passed carries the check in `text`; verify_failed in `name`.
            out["check"] = self.name or self.text
        if self.args is not None:
            out["args"] = self.args
        if self.text and not (self.kind == "verify_passed"):
            out["text"] = self.text
        return out


def _call_signature(call) -> str:
    """Identity of a tool call for thrash detection: name + arguments."""
    try:
        return f"{call.name}:{json.dumps(call.args, sort_keys=True)}"
    except (TypeError, ValueError):
        return f"{call.name}:{call.args!r}"


#: Marks where the retrieval preamble ends and the user's own words begin. Shared so
#: display code can strip it back off (see user_text_for_display).
LESSONS_SEPARATOR = "--- END OF PAST LESSONS ---\n\nNow, the actual request:\n"


def _with_past_corrections(session, cfg: dict[str, Any], user_text: str) -> str:
    """Prepend lessons from this project's corrections store to the user's request.

    The pipeline already learns: every `log_correction` and every machine-verified
    worker fix is embedded into `corrections/index.jsonl`. Until now only `delegate`
    used it — so the agent kept re-making mistakes the project had already recorded.
    Retrieval is fail-safe: any problem here must never block the turn.
    """
    if not cfg.get("agent_chat", {}).get("use_corrections", True):
        return user_text
    try:
        # Agent turns aren't tied to one role, so search across roles for this provider.
        rcfg = dict(cfg)
        rcfg["retrieval"] = {**cfg.get("retrieval", {}), "role_filter": False}
        hits = retrieval.retrieve(user_text, session.provider, "", rcfg)
        block = retrieval.format_fewshot(hits, max_solution_chars=600)
        if not block:
            return user_text
        return f"{block}\n{LESSONS_SEPARATOR}{user_text}"
    except Exception:
        return user_text


def user_text_for_display(content: str) -> str:
    """The user's own words, with any retrieval preamble stripped back off.

    History stores the wrapped text (the model needs the lessons), so anything that
    shows a past message to a HUMAN — the session picker, the panel's resume replay —
    must undo the wrapping or it displays a wall of unrelated code examples.
    """
    _, sep, after = content.partition(LESSONS_SEPARATOR)
    return after if sep else content


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
             user_text: str | None = None, on_delta=None, ask=None,
             steering=None) -> Iterator[Event]:
    """Drive one user request to completion. Yields Events; mutates `session` in place.

    `on_delta(text)` is a SIDE CHANNEL for streaming: the provider calls it with each
    token fragment the moment it arrives. It cannot be a yielded event — a generator
    can't yield from inside a callback — and buffering fragments to yield later would
    defeat the point. The complete reply still arrives as a normal `text` event.
    """
    cfg = session.cfg
    chat_cfg = cfg.get("agent_chat", {})
    max_steps = int(chat_cfg.get("max_steps", 40))
    deadline = time.monotonic() + int(chat_cfg.get("max_seconds", 1800))
    escalate_after = int(chat_cfg.get("escalate_after_failed_verifies", 2))

    if user_text is not None:
        session.add_user(_with_past_corrections(session, cfg, user_text), raw=user_text)

    schemas = tools_mod.schemas(registry)
    failed_verifies = 0
    repeats: dict[str, int] = {}          # tool+args signature -> times seen this turn
    repeat_limit = int(chat_cfg.get("repeat_limit", 3))

    for step in range(1, max_steps + 1):
        if time.monotonic() > deadline:
            yield Event("stopped", "Time limit reached — stopping. Ask me to continue.")
            return
        blocked = budgets.exceeded(cfg, session.provider)
        if blocked:
            yield Event("stopped", blocked)
            return

        # STEERING: anything the user typed while the agent was working is picked up
        # here, between steps — so "no, use the existing helper" lands immediately
        # instead of after the whole task finishes.
        if steering is not None:
            note = steering()
            if note:
                yield Event("steered", note)
                session.add_system_note(
                    f"[The user interrupted with new instructions — follow them now, "
                    f"they take priority over your current approach:]\n{note}")

        session.maybe_compact()
        usage: dict[str, Any] = {}
        try:
            turn = chat_providers.chat(session.messages, schemas, cfg, session.provider,
                                       session.model, usage,
                                       on_delta if chat_cfg.get("stream", True) else None)
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
                    if (yield from _try_escalate(session, cfg, ask)):
                        failed_verifies = 0
                continue
            return

        done = False
        # Announce every call before running any, then execute. Independent read-only
        # calls run CONCURRENTLY (models routinely emit read_file + list_files together);
        # anything that writes, needs approval, or ends the turn stays strictly ordered.
        for call in turn.tool_calls:
            yield Event("tool_call", name=call.name, args=call.args)
        outcomes = _dispatch_calls(registry, turn.tool_calls, chat_cfg)

        for call, result in zip(turn.tool_calls, outcomes):
            # Thrash guard. A weak model that gets stuck will re-issue the SAME call
            # forever (observed live: 40 turns of identical run_cmd). It can't notice
            # the pattern from the transcript, so we point it out in the one place it
            # always reads — the tool result.
            sig = _call_signature(call)
            repeats[sig] = repeats.get(sig, 0) + 1
            if repeats[sig] >= repeat_limit:
                nudge = (
                    f"\n\n--- YOU ARE REPEATING YOURSELF ---\n"
                    f"You have now called `{call.name}` with these exact arguments "
                    f"{repeats[sig]} times and gotten the same result each time. Repeating "
                    f"it again will not help. Change approach: read the relevant file in "
                    f"full, re-read the task, or explain what is blocking you and stop.")
                result += nudge
                yield Event("nudge", f"repeated {call.name} x{repeats[sig]}",
                            name=call.name)

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
                if (yield from _try_escalate(session, cfg, ask)):
                    failed_verifies = 0

    yield Event("stopped", f"Step limit ({max_steps}) reached — stopping.")


def _parallel_safe(registry: dict[str, tools_mod.Tool], call) -> bool:
    """Can this call run concurrently with its siblings?

    Only pure reads. Anything that writes must stay ordered (the verifier snapshots in
    call order), anything needing approval must stay ordered (one prompt at a time),
    `finish` ends the turn, and `run_tests` shells out against the whole repo — running
    two of those at once is at best pointless and at worst racy.
    """
    tool = registry.get(call.name)
    return bool(tool and not tool.mutating and not tool.needs_confirm
                and call.name not in ("finish", "run_tests"))


def _dispatch_calls(registry: dict[str, tools_mod.Tool], calls: list,
                    chat_cfg: dict[str, Any]) -> list[str]:
    """Run a turn's tool calls, returning results in the ORIGINAL order.

    Results must line up with `calls` positionally: the transcript pairs each tool
    message with the assistant's call, so a reordered list would silently corrupt the
    conversation.
    """
    results: list[str | None] = [None] * len(calls)
    if not calls:
        return []

    parallel = [i for i, c in enumerate(calls) if _parallel_safe(registry, c)]
    max_workers = int(chat_cfg.get("parallel_max", 4))
    if chat_cfg.get("parallel_tools", True) and len(parallel) > 1 and max_workers > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(max_workers, len(parallel))) as pool:
            futures = {pool.submit(tools_mod.dispatch, registry, calls[i].name,
                                   calls[i].args): i for i in parallel}
            for fut, i in futures.items():
                try:
                    results[i] = fut.result()
                except Exception as exc:            # never let one tool kill the turn
                    results[i] = f"ERROR: {exc}"

    for i, call in enumerate(calls):               # the rest, strictly in order
        if results[i] is None:
            results[i] = tools_mod.dispatch(registry, call.name, call.args)
    return [r or "" for r in results]


def escalation_ladder(cfg: dict[str, Any]) -> list[dict[str, str]]:
    """The ordered list of tiers to climb when the current model keeps failing.

    Cheapest first — the point is to spend the least money that solves the problem:
        local (free)  ->  cheap cloud tier  ->  strong cloud tier
    Configured as `cascade.ladder`; falls back to a single rung built from the legacy
    `cascade.escalate_to` so existing configs keep working.
    """
    casc = cfg.get("cascade", {})
    ladder = casc.get("ladder")
    if isinstance(ladder, list) and ladder:
        return [r for r in ladder if isinstance(r, dict) and r.get("provider")]
    esc = casc.get("escalate_to", "")
    return [{"provider": esc, "model": ""}] if esc else []


def _rung_available(cfg: dict[str, Any], rung: dict[str, str]) -> str:
    """"" if the rung can be used, else why it can't (for the log/UI)."""
    prov = rung.get("provider", "")
    if not chat_providers.supports_chat(cfg, prov):
        return f"{prov} is not a chat-capable provider"
    if not cfg.get("providers", {}).get(prov, {}).get("enabled", False):
        return f"{prov} is not enabled in config"
    blocked = budgets.exceeded(cfg, prov)
    return blocked or ""


def next_rung(cfg: dict[str, Any], provider: str, model: str) -> dict[str, str] | None:
    """The next usable tier above (provider, model), or None if we're at the top."""
    ladder = escalation_ladder(cfg)
    start = -1
    for i, rung in enumerate(ladder):
        if rung.get("provider") == provider and (rung.get("model", "") or "") == (model or ""):
            start = i
            break
    for rung in ladder[start + 1:]:
        if rung.get("provider") == provider and (rung.get("model", "") or "") == (model or ""):
            continue
        if not _rung_available(cfg, rung):
            return rung
    return None


def _try_escalate(session, cfg: dict[str, Any], ask=None) -> Iterator[Event]:
    """Climb one rung of the escalation ladder after repeated failures.

    Cloud tiers cost real money, so by default the user is ASKED before the switch
    (`ask(question) -> bool`). Unattended runs pass no `ask` / set
    `agent_chat.auto_escalate`, and the climb happens automatically.
    Returns True (via StopIteration value) if the switch happened.
    """
    rung = next_rung(cfg, session.provider, session.model)
    if rung is None:
        return False
    prov, mdl = rung["provider"], rung.get("model", "")
    label = f"{prov}/{mdl}" if mdl else prov
    why = rung.get("why", "")
    old = f"{session.provider}/{session.model}" if session.model else session.provider

    auto = bool(cfg.get("agent_chat", {}).get("auto_escalate", False)) or ask is None
    if not auto:
        question = (f"{old} has failed verification repeatedly. Switch to {label}"
                    f"{' (' + why + ')' if why else ''} for the rest of this task?")
        yield Event("escalation_offer", question, name=label)
        if not ask(question, label):
            yield Event("text", f"Staying on {old}. Tell me how you'd like to proceed.")
            return False

    session.provider, session.model = prov, mdl
    yield Event("escalated", f"{old} kept failing verification — switching to "
                             f"'{label}' for the rest of this task.")
    session.add_system_note(
        f"[A stronger model ({label}) has taken over after repeated failures. Re-read the "
        f"relevant files before editing — do not assume the previous attempts were right.]")
    return True


def run_headless(session, verifier: verify_mod.Verifier,
                 registry: dict[str, tools_mod.Tool], task: str, done_when: str,
                 on_event: Callable[[Event], None] | None = None,
                 on_delta=None, ask=None, steering=None) -> dict[str, Any]:
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
        for ev in run_turn(session, verifier, registry, message, on_delta, ask,
                           steering):
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
