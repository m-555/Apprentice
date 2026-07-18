"""Where Apprentice keeps its runtime data (config, corrections, outputs, metrics) —
and the shared config loader every module uses.

Two run modes, detected automatically:

  • REPO CHECKOUT (git clone, or `pip install -e .`): everything lives in the repo
    root next to `src/`, exactly as before — config/, corrections/, outputs/, metrics/.
  • INSTALLED PACKAGE (pip/pipx): the package sits read-only in site-packages, so data
    lives in a user data home instead: $APPRENTICE_HOME if set, else ~/.apprentice.
    `apprentice init` creates it and seeds the default config (a copy of the repo's
    config/qwen.json ships inside the wheel under default_config/).

An explicitly set APPRENTICE_HOME always wins, so tests/CI can redirect everything.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_PKG_DIR = Path(__file__).resolve().parent          # src/ (checkout) or site-packages/apprentice/
DEFAULT_CONFIG_DIR = _PKG_DIR / "default_config"    # exists only in the installed wheel


def _detect_root() -> Path:
    env = os.environ.get("APPRENTICE_HOME", "")
    if env:
        return Path(env)
    repo = _PKG_DIR.parent
    if (repo / "config" / "qwen.json").exists():    # running from a checkout
        return repo
    return Path.home() / ".apprentice"


ROOT = _detect_root()
CONFIG_PATH = ROOT / "config" / "qwen.json"
LOCAL_CONFIG_PATH = ROOT / "config" / "qwen.local.json"
CORRECTIONS_PATH = ROOT / "corrections" / "corrections.jsonl"
INDEX_PATH = ROOT / "corrections" / "index.jsonl"
STORE_PATH = ROOT / "outputs" / "store.jsonl"
OUTPUTS_DIR = ROOT / "outputs"
METRICS_PATH = ROOT / "metrics" / "metrics.jsonl"


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge `overlay` into `base` (overlay wins on scalars; dicts merge)."""
    out = dict(base)
    for key, val in overlay.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def load_config() -> dict[str, Any]:
    """qwen.json + the gitignored qwen.local.json overlay (secrets/machine-local values),
    deep-merged. Missing/broken files degrade to {} — code paths all carry defaults."""
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        cfg = {}
    if LOCAL_CONFIG_PATH.exists():
        try:
            cfg = deep_merge(cfg, json.loads(LOCAL_CONFIG_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return cfg
