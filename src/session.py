"""Conversation state for the agent: system prompt, history, compaction, transcripts.

The system prompt is assembled once and kept byte-stable for the whole session (role text
+ repo conventions + repo map) so provider-side prompt caching stays warm.

Compaction is what lets a session run long without falling off the model's context window:
when the history exceeds the configured budget, the OLDEST middle turns are replaced by a
short digest, while the first user request and the most recent turns are kept verbatim —
those carry the intent and the current state, which is what the model actually needs.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

try:
    from . import deliver, paths, tools as tools_mod
except ImportError:
    import deliver
    import paths
    import tools as tools_mod

_AGENT_PREAMBLE = """You are a careful software engineer working directly in a user's \
repository, with tools to read, search, edit, and run things.

How to work:
- ORIENT FIRST: list/search/read before you edit. Never guess a file's contents.
- Make the smallest change that does the job, and match the surrounding code's style.
- Prefer edit_file (exact-string replacement) over rewriting a whole file.
- After changing code, RUN THE TESTS (run_tests) to prove it works.
- When the task is done, call finish with a short summary of what you changed.
- If a tool result says a change was REVERTED, the repo is back to its previous state: \
read the error, understand the real cause, and try a different fix.
- Do not invent requirements or refactor things you were not asked to touch."""


def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Cheap chars/4 heuristic — good enough to decide when to compact, and it costs
    nothing (a real tokenizer would differ per provider anyway)."""
    total = 0
    for m in messages:
        total += len(m.get("content") or "")
        for tc in (m.get("tool_calls") or []):
            total += len(json.dumps(tc))
    return total // 4


def build_repo_map(repo: str, cfg: dict[str, Any]) -> str:
    """A compact file listing so the model knows what exists without reading anything."""
    max_files = int(cfg.get("agent_chat", {}).get("repo_map_max_files", 400))
    root = Path(repo).resolve()
    files = tools_mod._iter_files(root, max_files)
    if not files:
        return "(empty repository)"
    listing = "\n".join(files)
    if len(files) >= max_files:
        listing += f"\n… (listing capped at {max_files} files — use list_files/search)"
    return listing


class Session:
    """Message history + persistence for one agent run."""

    def __init__(self, repo: str, cfg: dict[str, Any], provider: str, model: str = "",
                 verify_policy: str = "tests", test_cmd: str = "",
                 session_id: str = ""):
        self.repo = repo
        self.cfg = cfg
        self.provider = provider
        self.model = model
        self.verify_policy = verify_policy
        self.test_cmd = test_cmd
        self.id = session_id or uuid.uuid4().hex[:12]
        self.started = time.time()
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt()}]
        self.usage = {"tokens_in": 0, "tokens_out": 0, "est_cost_usd": 0.0, "turns": 0}

    # --- prompt -------------------------------------------------------------
    def _system_prompt(self) -> str:
        parts = [_AGENT_PREAMBLE]
        conventions = str(
            deliver.load_repo_options(self.repo).get("conventions", "")).strip()
        if conventions:
            parts.append(f"--- PROJECT CONVENTIONS (follow these) ---\n{conventions}")
        if self.test_cmd:
            parts.append(f"The project's test command is: {self.test_cmd}")
        if self.verify_policy != "off":
            parts.append(
                f"VERIFICATION IS ON ({self.verify_policy}): after each of your turns the "
                f"repo is checked, and any change that fails is automatically reverted.")
        parts.append(f"--- REPOSITORY FILES ---\n{build_repo_map(self.repo, self.cfg)}")
        return "\n\n".join(parts)

    # --- history ------------------------------------------------------------
    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_assistant(self, turn) -> None:
        msg: dict[str, Any] = {"role": "assistant", "content": turn.content or ""}
        if turn.tool_calls:
            msg["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.name, "arguments": json.dumps(tc.args)}}
                for tc in turn.tool_calls]
        self.messages.append(msg)

    def add_tool_result(self, call, result: str) -> None:
        self.messages.append({"role": "tool", "tool_call_id": call.id,
                              "name": call.name, "content": result})

    def add_system_note(self, text: str) -> None:
        """An out-of-band instruction to the model (e.g. a verification failure)."""
        self.messages.append({"role": "user", "content": text})

    # --- compaction ---------------------------------------------------------
    def maybe_compact(self) -> bool:
        """Digest the middle of the conversation when it outgrows the budget.
        Returns True if compaction happened."""
        budget = int(self.cfg.get("agent_chat", {}).get("context_budget_tokens", 60000))
        if _estimate_tokens(self.messages) <= budget:
            return False
        keep_recent = int(self.cfg.get("agent_chat", {}).get("compact_keep_recent", 8))
        system, rest = self.messages[0], self.messages[1:]
        if len(rest) <= keep_recent + 2:
            return False
        first_user = rest[0]
        middle, recent = rest[1:-keep_recent], rest[-keep_recent:]
        if not middle:
            return False

        lines = []
        for m in middle:
            role = m.get("role")
            if role == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    lines.append(f"- called {tc['function']['name']}")
            elif role == "tool":
                head = (m.get("content") or "").strip().splitlines()[:1]
                lines.append(f"  -> {head[0][:120] if head else ''}")
            elif role == "user":
                lines.append(f"- user said: {(m.get('content') or '')[:160]}")
            elif m.get("content"):
                lines.append(f"- assistant: {m['content'][:160]}")
        digest = ("[Earlier in this session, summarized to save context]\n"
                  + "\n".join(lines[-120:]))
        # Trim the recent tail so it never starts on an orphaned tool result (a tool
        # message with no preceding assistant tool_calls confuses strict providers).
        while recent and recent[0].get("role") == "tool":
            recent = recent[1:]
        self.messages = [system, first_user,
                         {"role": "user", "content": digest}] + recent
        return True

    # --- persistence --------------------------------------------------------
    def transcript_path(self) -> Path:
        return paths.ROOT / "sessions" / f"{self.id}.json"

    def save(self) -> Path:
        p = self.transcript_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "id": self.id, "repo": self.repo, "provider": self.provider,
            "model": self.model, "verify": self.verify_policy,
            "test_cmd": self.test_cmd, "started": self.started,
            "usage": self.usage, "messages": self.messages,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return p

    @classmethod
    def load(cls, session_id: str, cfg: dict[str, Any]) -> "Session":
        p = paths.ROOT / "sessions" / f"{session_id}.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        s = cls(data["repo"], cfg, data.get("provider", "qwen"), data.get("model", ""),
                data.get("verify", "tests"), data.get("test_cmd", ""), data["id"])
        s.messages = data["messages"]
        s.usage = data.get("usage", s.usage)
        return s

    @staticmethod
    def list_recent(limit: int = 10) -> list[dict[str, Any]]:
        d = paths.ROOT / "sessions"
        if not d.is_dir():
            return []
        out = []
        for p in sorted(d.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            first = next((m.get("content", "") for m in data.get("messages", [])
                          if m.get("role") == "user"), "")
            out.append({"id": data.get("id", p.stem), "repo": data.get("repo", ""),
                        "provider": data.get("provider", ""), "first_task": first[:80]})
            if len(out) >= limit:
                break
        return out
