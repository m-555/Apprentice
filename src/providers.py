"""Provider handlers for delegation.

Each handler takes (system_prompt, user_prompt, cfg) and returns generated text.
`cfg` is the full parsed config/qwen.json dict.

Add a provider by writing a handler and registering it in PROVIDERS. Keep the set
small — Claude is the orchestrator/reviewer; these are just token generators.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable


# Providers optionally populate `usage` (a caller-supplied dict) with per-call
# metering (§6.5): tokens_in, tokens_out, duration_s. It's an out-param so the
# return type stays a plain str and old callers keep working (usage defaults None).

# --- qwen: local Ollama (live) ---------------------------------------------
def call_qwen(system: str, user: str, cfg: dict[str, Any],
              usage: dict[str, Any] | None = None, model: str = "") -> str:
    host = cfg.get("runner", {}).get("host", "http://127.0.0.1:11434")
    model = model or cfg.get("worker_model", {}).get("tag", "qwen3-coder-next")
    keep_alive = cfg.get("keep_alive", {}).get("value", "30m")
    timeout_s = cfg.get("providers", {}).get("qwen", {}).get("timeout_s", 600)

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "keep_alive": keep_alive,
    }
    req = urllib.request.Request(
        f"{host}/api/chat",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.load(resp)
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Could not reach Ollama at {host} ({exc}). Is the server running?"
        ) from exc
    if usage is not None:
        # Ollama returns real token counts + nanosecond durations per call.
        usage["tokens_in"] = int(data.get("prompt_eval_count", 0) or 0)
        usage["tokens_out"] = int(data.get("eval_count", 0) or 0)
        usage["duration_s"] = round(int(data.get("total_duration", 0) or 0) / 1e9, 3)
    return (data.get("message") or {}).get("content", "")


def _resolve_gemini_model(g: dict[str, Any], model: str) -> str:
    """Map a caller's model hint to a concrete Vertex model id.

    `model` may be a tier alias ("flash"/"pro") that indexes providers.gemini.models,
    or a raw model id passed through verbatim, or empty (→ the configured default tier).
    """
    models = g.get("models", {}) or {}
    if model in models:                       # "flash" / "pro" alias
        return models[model]
    if model:                                 # caller passed a raw model id
        return model
    default_tier = g.get("default_model", "flash")
    return models.get(default_tier) or g.get("model", "gemini-2.5-flash")


# --- gemini: Vertex AI (wired, gated on creds) -----------------------------
def call_gemini(system: str, user: str, cfg: dict[str, Any],
                usage: dict[str, Any] | None = None, model: str = "") -> str:
    g = cfg.get("providers", {}).get("gemini", {})
    if not g.get("enabled", False):
        raise RuntimeError(
            "Gemini provider is not enabled yet. Configure it in config/qwen.local.json: "
            "set providers.gemini.project, providers.gemini.credentials_file (your Vertex "
            "service-account JSON) or GOOGLE_APPLICATION_CREDENTIALS, providers.gemini.models "
            "(flash/pro ids), `pip install google-genai`, and providers.gemini.enabled=true."
        )
    try:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "Gemini provider needs the SDK: `pip install google-genai` (into the venv)."
        ) from exc

    # Point Application Default Credentials at the service-account JSON if configured and
    # not already set in the environment (keeps the secret path out of committed config).
    creds_file = g.get("credentials_file", "")
    if creds_file and not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds_file

    project = g.get("project") or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    if not project:
        raise RuntimeError(
            "Set providers.gemini.project (or GOOGLE_CLOUD_PROJECT) for Vertex AI."
        )
    client = genai.Client(
        vertexai=g.get("vertexai", True),
        project=project,
        location=g.get("location", "us-central1"),
    )
    resp = client.models.generate_content(
        model=_resolve_gemini_model(g, model),
        contents=user,
        config=types.GenerateContentConfig(system_instruction=system),
    )
    if usage is not None:
        um = getattr(resp, "usage_metadata", None)
        if um is not None:
            usage["tokens_in"] = int(getattr(um, "prompt_token_count", 0) or 0)
            usage["tokens_out"] = int(getattr(um, "candidates_token_count", 0) or 0)
    return resp.text or ""


# --- openai/codex: optional future slot ------------------------------------
def call_openai(system: str, user: str, cfg: dict[str, Any],
                usage: dict[str, Any] | None = None, model: str = "") -> str:
    raise RuntimeError(
        "OpenAI/Codex provider is not enabled (no API key configured). "
        "It is an optional future slot — see providers.openai in config/qwen.json."
    )


PROVIDERS: dict[str, Callable[..., str]] = {
    "qwen": call_qwen,
    "gemini": call_gemini,
    "openai": call_openai,
}


def provider_names() -> list[str]:
    return sorted(PROVIDERS.keys())
