"""Machine verification — "nothing broken survives a turn".

This is what separates Apprentice's agent from a plain tool-calling loop. After the model
finishes a turn that touched files, the changes are checked; if the check fails, the files
are **restored to exactly what they were** and the verbatim failure is handed back to the
model as the tool result. The model then fixes it with real evidence instead of guessing —
and the user's tree is never left broken.

Policies (config `agent_chat.verify`, or --verify):
    off    no checking; edits land immediately (a normal fast agent).
    gate   each edited file must pass the mechanical gate (compile/lint) for its language.
    tests  gate, then the project's own test command. The strongest signal available.

Snapshots also power `/undo`: every mutation records the previous bytes (or "file did not
exist"), so any turn can be rolled back exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from . import deliver, gate, tools as tools_mod
except ImportError:
    import deliver
    import gate
    import tools as tools_mod

POLICIES = ("off", "gate", "tests")

# File extension -> the gate role whose language check applies.
_EXT_ROLE = {".py": "py_implementer", ".ts": "ts_implementer", ".tsx": "ts_implementer",
             ".js": "ts_implementer", ".jsx": "ts_implementer",
             ".cpp": "cpp_implementer", ".cc": "cpp_implementer",
             ".cxx": "cpp_implementer", ".h": "cpp_implementer",
             ".hpp": "cpp_implementer"}


@dataclass
class Snapshot:
    """One file's state before a mutation. `content is None` = the file did not exist."""
    path: Path
    content: str | None


@dataclass
class VerifyResult:
    ok: bool
    check: str = ""            # which check ran ("gate:py_compile", "tests", "none")
    error_text: str = ""       # verbatim checker/test output on failure
    reverted: list[str] = field(default_factory=list)

    def as_tool_note(self) -> str:
        """The message appended to the model's tool results after a failed verify."""
        files = ", ".join(self.reverted) or "the edited file(s)"
        return (f"\n\n--- VERIFICATION FAILED ({self.check}) ---\n"
                f"Your change to {files} did NOT pass and has been REVERTED — the repo is "
                f"back to its previous state. Fix the problem and apply the change again. "
                f"Verbatim output:\n{self.error_text}")


class Verifier:
    """Tracks mutations for the current turn and enforces the policy at turn end."""

    def __init__(self, repo: str, cfg: dict[str, Any], policy: str = "tests",
                 test_cmd: str = ""):
        if policy not in POLICIES:
            raise ValueError(f"unknown verify policy {policy!r} (use {'|'.join(POLICIES)})")
        # `tests` without a test command can't do more than `gate` — degrade honestly
        # instead of silently pretending the code was test-verified.
        if policy == "tests" and not test_cmd:
            policy = "gate"
        self.repo = repo
        self.cfg = cfg
        self.policy = policy
        self.test_cmd = test_cmd
        self._turn: list[Snapshot] = []      # snapshots for the turn in progress
        self.history: list[list[Snapshot]] = []   # committed turns (for /undo)

    # --- snapshotting -------------------------------------------------------
    def before_mutation(self, rel_path: str) -> None:
        """Record a file's current bytes before a tool changes it (first touch wins, so a
        revert restores the state from the START of the turn)."""
        try:
            p = deliver.resolve_repo_path(self.repo, rel_path)
        except ValueError:
            return
        if any(s.path == p for s in self._turn):
            return
        self._turn.append(Snapshot(p, p.read_text(encoding="utf-8", errors="replace")
                                   if p.is_file() else None))

    @property
    def touched(self) -> list[Path]:
        return [s.path for s in self._turn]

    def _restore(self, snaps: list[Snapshot]) -> list[str]:
        restored = []
        for s in snaps:
            if s.content is None:
                s.path.unlink(missing_ok=True)
            else:
                s.path.write_text(s.content, encoding="utf-8")
            restored.append(self._rel(s.path))
        return restored

    def _rel(self, p: Path) -> str:
        try:
            return str(p.relative_to(Path(self.repo).resolve())).replace("\\", "/")
        except ValueError:
            return str(p)

    # --- the policy ---------------------------------------------------------
    def _gate_files(self) -> VerifyResult:
        for snap in self._turn:
            role = _EXT_ROLE.get(snap.path.suffix.lower())
            if not role or not snap.path.is_file():
                continue
            code = snap.path.read_text(encoding="utf-8", errors="replace")
            res = gate.run_gate(f"```\n{code}\n```", role, self.cfg)
            if res.status == "fail":
                return VerifyResult(False, f"gate:{res.check}", res.error_text)
        return VerifyResult(True, "gate")

    def _run_tests(self) -> VerifyResult:
        timeout = int(self.cfg.get("agent_chat", {}).get("test_timeout_s", 600))
        rc, out = deliver.run_test_cmd(self.repo, self.test_cmd, timeout)
        if rc == 0:
            return VerifyResult(True, "tests")
        return VerifyResult(False, "tests",
                            f"`{self.test_cmd}` exited {rc}:\n{out[-4000:]}")

    def finish_turn(self) -> VerifyResult:
        """Check everything this turn touched. On failure the turn is reverted.
        On success the snapshots move to history (so `/undo` can still roll them back)."""
        if not self._turn or self.policy == "off":
            self._commit()
            return VerifyResult(True, "none")

        result = self._gate_files()
        if result.ok and self.policy == "tests":
            result = self._run_tests()

        if not result.ok:
            result.reverted = self._restore(self._turn)
            self._turn = []
            return result
        self._commit()
        return result

    def _commit(self) -> None:
        if self._turn:
            self.history.append(self._turn)
            self._turn = []

    # --- undo ---------------------------------------------------------------
    def undo_last(self) -> list[str]:
        """Roll back the most recent committed turn. Returns the restored paths."""
        if not self.history:
            return []
        return self._restore(self.history.pop())


def wrap_registry(registry: dict[str, tools_mod.Tool],
                  verifier: Verifier) -> dict[str, tools_mod.Tool]:
    """Return the tool registry with every MUTATING tool snapshotted before it runs.

    Done by wrapping rather than editing the tools so `tools.py` stays policy-free and
    usable with verification switched off.
    """
    wrapped: dict[str, tools_mod.Tool] = {}
    for name, tool in registry.items():
        if not tool.mutating:
            wrapped[name] = tool
            continue

        def make(t: tools_mod.Tool):
            def run(**kw):
                path = kw.get("path")
                if isinstance(path, str):
                    verifier.before_mutation(path)
                return t.run(**kw)
            return run

        wrapped[name] = tools_mod.Tool(
            tool.name, tool.description, tool.parameters, make(tool),
            mutating=True, needs_confirm=tool.needs_confirm)
    return wrapped
