"""The terminal REPL for `apprentice chat` — and the headless `apprentice run`.

Thin on purpose: all the behavior lives in loop/tools/verify/session. This module only
renders events, asks for confirmations, and handles slash commands.
"""

from __future__ import annotations

import subprocess
import sys
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


def _confirmer(auto_yes: bool):
    def confirm(tool_name: str, detail: str) -> bool:
        if auto_yes:
            _out(f"  [auto-approved] {detail}")
            return True
        _out(f"\n  [CONFIRM] The agent wants to run: {detail}")
        try:
            answer = input("  Allow? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer in ("y", "yes")
    return confirm


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
    elif ev.kind == "stopped":
        _out(f"\n  [STOPPED] {ev.text}")


def _make_runtime(sess, cfg: dict[str, Any], auto_yes: bool):
    """Build the verifier + verification-wrapped tool registry for a session."""
    verifier = verify_mod.Verifier(sess.repo, cfg, sess.verify_policy, sess.test_cmd)
    ctx = tools_mod.ToolContext(repo=sess.repo, cfg=cfg, test_cmd=sess.test_cmd,
                                confirm=_confirmer(auto_yes))
    registry = verify_mod.wrap_registry(tools_mod.build_tools(ctx), verifier)
    return verifier, registry


def chat(repo: str, cfg: dict[str, Any], provider: str, model: str = "",
         verify: str = "", test_cmd: str = "", auto_yes: bool = False,
         allow_dirty: bool = False, resume: str = "") -> int:
    repo = str(Path(repo).resolve())
    chat_cfg = cfg.get("agent_chat", {})

    if not chat_providers.supports_chat(cfg, provider):
        _out(f"Provider '{provider}' can't run the agent (no chat/tool support for its "
             f"kind). Configure providers.{provider}.kind, or pick another provider.")
        return 2

    is_git, dirty = _git_state(repo)
    if chat_cfg.get("require_clean_git", True) and not allow_dirty:
        if not is_git:
            _out(f"{repo} is not a git repository. The agent edits files in place — git "
                 f"is your undo. Run `git init`, or pass --allow-dirty to proceed anyway.")
            return 2
        if dirty:
            _out("Your working tree has uncommitted changes. Commit or stash them first "
                 "so you can tell the agent's edits from your own (or --allow-dirty).")
            return 2

    if resume:
        sess = session_mod.Session.load(resume, cfg)
        _out(f"Resumed session {sess.id} ({len(sess.messages)} messages).")
    else:
        sess = session_mod.Session(
            repo, cfg, provider, model,
            verify or chat_cfg.get("verify", "tests"),
            _resolve_test_cmd(repo, cfg, test_cmd))

    verifier, registry = _make_runtime(sess, cfg, auto_yes)

    _out(f"\nApprentice agent | repo={repo}")
    _out(f"provider={sess.provider}{('/' + sess.model) if sess.model else ''} | "
         f"verify={verifier.policy}"
         f"{(' | tests=' + sess.test_cmd) if sess.test_cmd else ' | (no test command)'}")
    if verifier.policy == "off":
        _out("verification is OFF — edits land immediately, nothing is checked.")
    _out("Describe what you want. /help for commands, /quit to exit.\n")

    while True:
        try:
            line = input("you > ").strip()
        except (EOFError, KeyboardInterrupt):
            _out()
            break
        if not line:
            continue

        if line.startswith("/"):
            cmd, _, arg = line[1:].partition(" ")
            arg = arg.strip()
            if cmd in ("quit", "exit", "q"):
                break
            if cmd == "help":
                _out(HELP)
            elif cmd == "undo":
                restored = verifier.undo_last()
                _out(f"  reverted: {', '.join(restored)}" if restored
                     else "  nothing to undo")
            elif cmd == "verify":
                if arg in verify_mod.POLICIES:
                    sess.verify_policy = arg
                    verifier, registry = _make_runtime(sess, cfg, auto_yes)
                    _out(f"  verification = {verifier.policy}")
                else:
                    _out(f"  usage: /verify {'|'.join(verify_mod.POLICIES)}")
            elif cmd == "provider":
                if chat_providers.supports_chat(cfg, arg):
                    sess.provider, sess.model = arg, ""
                    _out(f"  provider = {arg}")
                else:
                    _out(f"  unknown/unsupported provider: {arg!r}")
            elif cmd == "model":
                sess.model = arg
                _out(f"  model = {arg or '(provider default)'}")
            elif cmd == "cost":
                u = sess.usage
                _out(f"  turns={u['turns']} tokens in/out={u['tokens_in']}/"
                     f"{u['tokens_out']} est_cost=${u['est_cost_usd']:.4f}")
            elif cmd == "files":
                changed = sorted({verifier._rel(s.path) for snaps in verifier.history
                                  for s in snaps})
                _out("  " + (", ".join(changed) if changed else "(none yet)"))
            elif cmd == "save":
                _out(f"  saved -> {sess.save()}")
            else:
                _out(f"  unknown command {line!r}. /help for the list.")
            continue

        try:
            for ev in loop.run_turn(sess, verifier, registry, line):
                _render(ev)
        except KeyboardInterrupt:
            _out("\n  [STOPPED] interrupted")
        sess.save()
        _out()

    path = sess.save()
    u = sess.usage
    _out(f"Session {sess.id} saved -> {path}")
    _out(f"turns={u['turns']} tokens in/out={u['tokens_in']}/{u['tokens_out']} "
         f"est_cost=${u['est_cost_usd']:.4f}   resume with: apprentice chat --resume {sess.id}")
    return 0


def run_headless(repo: str, cfg: dict[str, Any], task: str, done_when: str,
                 provider: str, model: str = "", verify: str = "",
                 test_cmd: str = "") -> int:
    repo = str(Path(repo).resolve())
    if not chat_providers.supports_chat(cfg, provider):
        _out(f"Provider '{provider}' can't run the agent.")
        return 2
    sess = session_mod.Session(
        repo, cfg, provider, model,
        verify or cfg.get("agent_chat", {}).get("verify", "tests"),
        _resolve_test_cmd(repo, cfg, test_cmd))
    verifier, registry = _make_runtime(sess, cfg, auto_yes=True)

    _out(f"Apprentice headless | repo={repo} | provider={sess.provider} | "
         f"done_when={done_when}")
    result = loop.run_headless(sess, verifier, registry, task, done_when, _render)
    sess.save()
    _out(f"\ndone_passed={result['done_passed']} rounds={result['rounds']} "
         f"files_changed={result['files_changed']}")
    _out(f"tokens in/out={sess.usage['tokens_in']}/{sess.usage['tokens_out']} "
         f"est_cost=${sess.usage['est_cost_usd']:.4f} | session {sess.id}")
    if not result["done_passed"]:
        _out(f"last output:\n{result['done_log_tail']}")
    return 0 if result["done_passed"] else 1
