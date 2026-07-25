"""Append-and-index the corrections store — the Phase-5 learning signal.

One writer shared by every producer of corrections:
  • `log_correction` (orchestrator-authored fixes),
  • the §6.1 worker→worker gate retry,
  • the delegate apply/test loop, and
  • the interactive agent's verify→revert→fix cycle.

Every record is appended to corrections.jsonl and embedded into index.jsonl so future
delegations retrieve it as few-shot. Indexing is fail-safe: if the embedder is down the
record is still saved (rebuild later with `apprentice reindex`).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

try:
    from . import paths, retrieval
except ImportError:
    import paths
    import retrieval


def write(record: dict[str, Any], cfg: dict[str, Any],
          corrections_path=None) -> bool:
    """Append `record` and index it. Returns whether it was indexed (never raises for
    embedding problems — the record itself must not be lost)."""
    path = corrections_path or paths.CORRECTIONS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    try:
        return retrieval.index_record(record, cfg)
    except Exception:
        return False


def machine_verified(provider: str, role: str, task: str, before: str, after: str,
                     explanation: str, error_category: str = "logic") -> dict[str, Any]:
    """Build a record for a fix a WORKER made and a machine verified (gate/test green).
    These cost the orchestrator nothing and still grow the retrieval store."""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "role": role,
        "task": task,
        "context": "",
        "qwen_output": before,
        "corrected_output": after,
        "error_category": error_category,
        "explanation": explanation,
        "machine_verified": True,
        "corrected_by": "worker_retry",
    }
