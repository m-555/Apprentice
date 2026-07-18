"""§6.2 worker-output store + unified-diff reconstruction.

`delegate` stores every worker output under a short `output_id`. `log_correction`
can then reference that id and send only a unified-diff `correction_patch` — Claude
pays for the delta, never a re-transmitted copy of the worker output. The store
reconstructs `corrected_output = apply(patch, stored_output)` so Phase-5 retrieval
still gets full before/after pairs.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from . import paths
except ImportError:
    import paths

_STORE_PATH = paths.STORE_PATH


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def put(output_id: str, output: str, *, provider: str, role: str, task: str) -> None:
    """Persist a worker output so a later log_correction can reference it by id."""
    _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "output_id": output_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "role": role,
        "task": task,
        "output": output,
    }
    with _STORE_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def get(output_id: str) -> dict[str, Any] | None:
    """Return the stored record for output_id (last write wins), or None."""
    if not output_id or not _STORE_PATH.exists():
        return None
    found = None
    for line in _STORE_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("output_id") == output_id:
            found = rec  # keep scanning so the latest wins
    return found


def apply_patch(original: str, patch: str) -> tuple[str | None, str]:
    """Apply a unified-diff `patch` to `original`. Returns (corrected, "") on success
    or (None, error_text) on failure.

    Robust to whatever path Claude put in the diff header: we rewrite the first
    ---/+++ lines to a/unit b/unit and apply with `git apply -p1`.
    """
    if not patch.strip():
        return original, ""  # empty patch = accepted as-is
    norm, seen_minus, seen_plus = [], False, False
    for ln in patch.splitlines():
        if ln.startswith("--- ") and not seen_minus:
            norm.append("--- a/unit"); seen_minus = True
        elif ln.startswith("+++ ") and not seen_plus:
            norm.append("+++ b/unit"); seen_plus = True
        else:
            norm.append(ln)
    patch_text = "\n".join(norm) + "\n"

    with tempfile.TemporaryDirectory() as tmp:
        unit = os.path.join(tmp, "unit")
        with open(unit, "w", encoding="utf-8", newline="\n") as f:
            f.write(original if original.endswith("\n") else original + "\n")
        ppath = os.path.join(tmp, "fix.patch")
        with open(ppath, "w", encoding="utf-8", newline="\n") as f:
            f.write(patch_text)
        # -c core.autocrlf=false: don't let CRLF conversion break context matching.
        # git apply is atomic (no change on failure), so escalate leniency on retry:
        # plain first (exact), then --recount (tolerate off line counts), then also
        # --ignore-whitespace (last resort).
        base = ["git", "-c", "core.autocrlf=false", "apply", "-p1"]
        last_err = "git apply failed"
        for extra in ([], ["--recount"], ["--recount", "--ignore-whitespace"]):
            try:
                proc = subprocess.run(
                    base + extra + [ppath],
                    cwd=tmp, capture_output=True, text=True, timeout=30,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                return None, f"git apply unavailable: {exc}"
            if proc.returncode == 0:
                with open(unit, encoding="utf-8") as f:
                    return f.read(), ""
            last_err = (proc.stderr or "git apply failed").strip()
        return None, last_err
