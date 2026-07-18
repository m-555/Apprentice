"""Phase 5 — in-context retrieval of past corrections (NO weight training).

On each correction we embed the task with the local embedding model (nomic-embed-text via
Ollama) and store the vector in corrections/index.jsonl. At delegation time we embed the
incoming task, find the top-k most similar past corrections for the SAME provider+role
(favoring real mistakes), and inject them as few-shot examples before calling the worker.

A flat on-disk vector list is plenty at this scale (thousands of records). All retrieval
tunables live in config/qwen.json -> "retrieval".
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
_INDEX_PATH = _ROOT / "corrections" / "index.jsonl"
_CORRECTIONS_PATH = _ROOT / "corrections" / "corrections.jsonl"


def _embedding_model(cfg: dict[str, Any]) -> str:
    tag = cfg.get("embedding_model", {}).get("tag", "nomic-embed-text")
    return tag.split(":")[0] if tag.endswith(":latest") else tag


def _embed(text: str, cfg: dict[str, Any]) -> list[float]:
    host = cfg.get("runner", {}).get("host", "http://127.0.0.1:11434")
    body = {"model": _embedding_model(cfg), "prompt": text}
    req = urllib.request.Request(
        f"{host}/api/embeddings",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp).get("embedding", [])


def index_record(record: dict[str, Any], cfg: dict[str, Any]) -> bool:
    """Embed a correction's task and append a compact entry to index.jsonl.

    Self-contained (carries the few-shot fields) so retrieval needs no join.
    """
    vec = _embed(record["task"], cfg)
    if not vec:
        return False
    entry = {
        "timestamp": record.get("timestamp", ""),
        "provider": record.get("provider", "qwen"),
        "role": record.get("role", ""),
        "error_category": record.get("error_category", "none"),
        "task": record.get("task", ""),
        "corrected_output": record.get("corrected_output", ""),
        "explanation": record.get("explanation", ""),
        "vector": vec,
    }
    _INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _INDEX_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return True


def _load_index() -> list[dict[str, Any]]:
    if not _INDEX_PATH.exists():
        return []
    out = []
    for line in _INDEX_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _cosine(query: np.ndarray, mat: np.ndarray) -> np.ndarray:
    qn = query / (np.linalg.norm(query) + 1e-9)
    mn = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
    return mn @ qn


def retrieve(task: str, provider: str, role: str, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Return up to top_k similar past corrections for (provider, role)."""
    rcfg = cfg.get("retrieval", {})
    if not rcfg.get("enabled", True):
        return []
    k = int(rcfg.get("top_k", 5))
    role_filter = bool(rcfg.get("role_filter", True))
    prefer = bool(rcfg.get("prefer_error_categories", True))
    mix = float(rcfg.get("mistake_vs_correct_mix", 0.7))

    entries = _load_index()
    cand = [
        e for e in entries
        if e.get("provider") == provider and (not role_filter or e.get("role") == role)
    ]
    if not cand:
        return []

    qv = np.asarray(_embed(task, cfg), dtype=np.float32)
    if qv.size == 0:
        return []
    mat = np.asarray([e["vector"] for e in cand], dtype=np.float32)
    sims = _cosine(qv, mat)
    for e, s in zip(cand, sims):
        e["_sim"] = float(s)
    cand.sort(key=lambda e: e["_sim"], reverse=True)

    if not prefer:
        return cand[:k]

    mistakes = [e for e in cand if e.get("error_category") != "none"]
    corrects = [e for e in cand if e.get("error_category") == "none"]
    n_mis = round(k * mix)
    chosen = mistakes[:n_mis] + corrects[: k - n_mis]
    if len(chosen) < k:  # backfill if a bucket was short
        # Compare by identity, not dict equality: two distinct records with identical
        # content must not dedupe each other (and `in` on dicts is O(n^2) as it grows).
        chosen_ids = {id(e) for e in chosen}
        rest = [e for e in cand if id(e) not in chosen_ids]
        chosen += rest[: k - len(chosen)]
    chosen.sort(key=lambda e: e["_sim"], reverse=True)
    return chosen[:k]


def format_fewshot(records: list[dict[str, Any]], max_solution_chars: int = 1200) -> str:
    """Render retrieved corrections as a few-shot block for the system prompt."""
    if not records:
        return ""
    parts = [
        "Here are past, SIMILAR tasks and their CORRECT solutions from this codebase. "
        "Learn from them and avoid the noted mistakes:\n"
    ]
    for i, e in enumerate(records, 1):
        sol = e.get("corrected_output", "")
        if len(sol) > max_solution_chars:
            sol = sol[:max_solution_chars] + "\n# … (truncated)"
        parts.append(f"\n[Example {i}] (past issue category: {e.get('error_category','')})\n")
        parts.append(f"Task: {e.get('task','')}\n")
        lesson = (e.get("explanation") or "").strip()
        if lesson:
            parts.append(f"Lesson: {lesson}\n")
        parts.append(f"Correct solution:\n{sol}\n")
    return "".join(parts)


def reindex(cfg: dict[str, Any]) -> int:
    """Rebuild index.jsonl from corrections.jsonl. Returns count indexed."""
    if _INDEX_PATH.exists():
        _INDEX_PATH.unlink()
    if not _CORRECTIONS_PATH.exists():
        return 0
    n = 0
    for line in _CORRECTIONS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        if index_record(json.loads(line), cfg):
            n += 1
    return n


if __name__ == "__main__":
    import sys

    cfg = json.loads((_ROOT / "config" / "qwen.json").read_text(encoding="utf-8"))
    if len(sys.argv) > 1 and sys.argv[1] == "reindex":
        print(f"reindexed {reindex(cfg)} record(s) -> {_INDEX_PATH}")
    else:
        print("usage: python retrieval.py reindex")
