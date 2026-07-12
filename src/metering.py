"""§6.5 metering — record per-delegation cost/outcome so routing is data-driven.

The point (from the plan): "you believe the loop costs more than it saves — instrument
it so you know." Every delegation appends one event to metrics/metrics.jsonl (local,
git-ignored — task text may be proprietary, so only a hash + short prefix is stored).

What we CAN measure in-pipeline:
  - tier (qwen | gemini) real token counts + duration (Ollama/Vertex report them),
  - the mechanical-gate verdict, retry count, and whether the unit was machine-verified
    with ZERO Claude involvement (the actual win),
  - and, via log_correction, when Claude DID have to step in (tier="claude") + category.
What we CANNOT: Claude's own token counts (Claude runs in Claude Code, not through this
server). So the Claude-side signal is *involvement rate* (accepted-with-no-Claude vs
needed-Claude), which is the decision-relevant proxy for "Claude tokens per accepted unit."
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
_METRICS_PATH = _ROOT / "metrics" / "metrics.jsonl"


def task_ref(task: str) -> dict[str, str]:
    """A non-proprietary reference to a task: sha1 + a short prefix for eyeballing."""
    h = hashlib.sha1((task or "").encode("utf-8")).hexdigest()[:12]
    prefix = " ".join((task or "").split())[:60]
    return {"task_sha": h, "task_prefix": prefix}


def record(event: dict[str, Any], cfg: dict[str, Any] | None = None) -> bool:
    """Append one metering event. Fail-safe: never let metering break a delegation."""
    if cfg is not None and not cfg.get("metering", {}).get("enabled", True):
        return False
    try:
        event = {"timestamp": datetime.now(timezone.utc).isoformat(), **event}
        _METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _METRICS_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def _load(limit: int | None = None) -> list[dict[str, Any]]:
    if not _METRICS_PATH.exists():
        return []
    lines = _METRICS_PATH.read_text(encoding="utf-8").splitlines()
    if limit:
        lines = lines[-limit:]
    out = []
    for ln in lines:
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
    return out


def tier_token_total(tier: str, since_iso: str | None = None) -> int:
    """Sum tokens_out for a tier (optionally since an ISO timestamp) — for budget caps."""
    total = 0
    for e in _load():
        if e.get("tier") != tier:
            continue
        if since_iso and e.get("timestamp", "") < since_iso:
            continue
        total += int(e.get("tokens_out", 0) or 0)
    return total


def report(last_n: int = 50) -> str:
    """Human-readable rollup of the last N events, broken down by tier."""
    events = _load()
    if not events:
        return "No metering events recorded yet."
    events = events[-last_n:]

    by_tier: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"n": 0, "tok_in": 0, "tok_out": 0, "dur": 0.0,
                 "worker_calls": 0, "gate_pass": 0, "gate_fail": 0,
                 "gate_skip": 0, "machine_verified": 0, "retried": 0}
    )
    delegations = 0
    machine_verified_no_claude = 0
    claude_events = 0

    for e in events:
        tier = e.get("tier", "?")
        t = by_tier[tier]
        t["n"] += 1
        t["tok_in"] += int(e.get("tokens_in", 0) or 0)
        t["tok_out"] += int(e.get("tokens_out", 0) or 0)
        t["dur"] += float(e.get("duration_s", 0.0) or 0.0)
        t["worker_calls"] += int(e.get("worker_calls", 0) or 0)
        gs = e.get("gate_status")
        if gs == "pass":
            t["gate_pass"] += 1
        elif gs == "fail":
            t["gate_fail"] += 1
        elif gs == "skipped":
            t["gate_skip"] += 1
        if e.get("machine_verified"):
            t["machine_verified"] += 1
        if int(e.get("worker_calls", 1) or 1) > 1:
            t["retried"] += 1

        if tier == "claude":
            claude_events += 1
        else:
            delegations += 1
            if e.get("machine_verified"):
                machine_verified_no_claude += 1

    lines = [f"Metering report — last {len(events)} event(s):", ""]
    for tier, t in sorted(by_tier.items()):
        lines.append(
            f"  [{tier}] events={t['n']} worker_calls={t['worker_calls']} "
            f"tokens(in/out)={t['tok_in']}/{t['tok_out']} dur={t['dur']:.1f}s"
        )
        if tier != "claude":
            lines.append(
                f"        gate pass/fail/skip={t['gate_pass']}/{t['gate_fail']}/{t['gate_skip']} "
                f"machine_verified={t['machine_verified']} retried={t['retried']}"
            )
    lines.append("")
    if delegations:
        pct = 100.0 * machine_verified_no_claude / delegations
        lines.append(
            f"  Accepted with ZERO Claude review: {machine_verified_no_claude}/{delegations} "
            f"({pct:.0f}%) delegations machine-verified."
        )
    lines.append(
        f"  Claude had to step in: {claude_events} time(s) "
        f"(logged via log_correction, corrected_by=claude)."
    )
    lines.append(
        "  NOTE: Claude token counts aren't visible to the pipeline; the win is the "
        "machine-verified %% climbing and Claude step-ins falling over time."
    )
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    print(report(n))
