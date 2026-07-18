"""Provider handlers for delegation.

Every handler has the standard signature (system, user, cfg, usage=None, model="") and
returns generated text. `cfg` is the full merged config dict. Providers optionally
populate `usage` (a caller-supplied dict) with per-call metering (§6.5): tokens_in,
tokens_out, duration_s.

PROVIDERS holds the three built-in names (qwen, gemini, openai). Beyond those, providers
are CONFIG-DRIVEN: add any entry under config `providers.<name>` with a known `kind` and
`resolve()` builds the handler — no code changes. Kinds:

  - "ollama-local"       : a local Ollama model (host/model/keep_alive per provider,
                           falling back to the top-level runner/worker_model config).
  - "openai-compatible"  : ANY OpenAI-style /chat/completions endpoint — OpenAI/Codex,
                           Groq, OpenRouter, Mistral, LM Studio, vLLM, llama.cpp server…
                           Configure base_url + model (+ api_key_env for the key; keys
                           NEVER live in config files).
  - "openai-api"         : alias of openai-compatible.
  - "vertex-ai"          : Google Vertex AI via google-genai (service-account/ADC).

Keep the set small — the orchestrator is the reviewer; these are just token generators.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable


def _sampling_options(provider_cfg: dict[str, Any]) -> dict[str, Any]:
    """Read providers.<name>.options, dropping `_`-prefixed doc keys. For implement-to-
    spec codegen a LOW temperature (~0.1-0.2) is the point: deterministic output means
    fewer gate failures and retry churn (default sampling temps are creativity settings)."""
    opts = provider_cfg.get("options", {}) or {}
    return {k: v for k, v in opts.items() if not k.startswith("_")}


def _resolve_model(p: dict[str, Any], model: str, fallback: str = "") -> str:
    """Map a caller's `model` hint to a concrete model id for a provider entry `p`.

    `model` may be a tier alias (e.g. "flash"/"pro"/"mini") indexing p["models"], a raw
    model id passed through verbatim, or empty (→ p["default_model"] tier, else
    p["model"], else `fallback`).
    """
    models = p.get("models", {}) or {}
    if model in models:                        # tier alias
        return models[model]
    if model:                                  # raw model id
        return model
    default_tier = p.get("default_model", "")
    return models.get(default_tier) or p.get("model") or fallback


def _post_json(url: str, body: dict[str, Any], headers: dict[str, str],
               timeout_s: int) -> dict[str, Any]:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.load(resp)


# --- kind: ollama-local ------------------------------------------------------
def call_ollama(name: str, system: str, user: str, cfg: dict[str, Any],
                usage: dict[str, Any] | None = None, model: str = "") -> str:
    p = cfg.get("providers", {}).get(name, {})
    host = p.get("host") or cfg.get("runner", {}).get("host", "http://127.0.0.1:11434")
    model_id = _resolve_model(
        p, model, cfg.get("worker_model", {}).get("tag", "qwen3-coder-next"))
    keep_alive = p.get("keep_alive") or cfg.get("keep_alive", {}).get("value", "30m")
    timeout_s = int(p.get("timeout_s", 600))

    body: dict[str, Any] = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "keep_alive": keep_alive,
    }
    opts = _sampling_options(p)
    if opts:
        body["options"] = opts  # e.g. temperature/top_p; passed straight to Ollama
    try:
        data = _post_json(f"{host}/api/chat", body, {}, timeout_s)
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


# --- kind: openai-compatible (OpenAI, Groq, OpenRouter, LM Studio, vLLM, …) --
def call_openai_compatible(name: str, system: str, user: str, cfg: dict[str, Any],
                           usage: dict[str, Any] | None = None, model: str = "") -> str:
    p = cfg.get("providers", {}).get(name, {})
    if not p.get("enabled", False):
        raise RuntimeError(
            f"Provider '{name}' is not enabled. In config/qwen.local.json set "
            f"providers.{name}.enabled=true, base_url (default https://api.openai.com/v1), "
            f"model (or models tier map), and export the API key in the env var named by "
            f"providers.{name}.api_key_env (keys never go in config files)."
        )
    base_url = (p.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    model_id = _resolve_model(p, model)
    if not model_id:
        raise RuntimeError(f"Set providers.{name}.model (or models/default_model).")
    timeout_s = int(p.get("timeout_s", 300))

    headers: dict[str, str] = {}
    api_key = os.environ.get(p.get("api_key_env", "OPENAI_API_KEY"), "")
    if api_key:  # local endpoints (LM Studio/vLLM) often need no key
        headers["Authorization"] = f"Bearer {api_key}"

    body: dict[str, Any] = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    opts = _sampling_options(p)
    for key in ("temperature", "top_p", "max_tokens"):
        if key in opts:
            body[key] = opts[key]

    t0 = time.monotonic()
    try:
        data = _post_json(f"{base_url}/chat/completions", body, headers, timeout_s)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        raise RuntimeError(
            f"Provider '{name}' returned HTTP {exc.code} from {base_url}: {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Could not reach provider '{name}' at {base_url} ({exc})."
        ) from exc
    if usage is not None:
        usage["duration_s"] = round(time.monotonic() - t0, 3)
        u = data.get("usage") or {}
        usage["tokens_in"] = int(u.get("prompt_tokens", 0) or 0)
        usage["tokens_out"] = int(u.get("completion_tokens", 0) or 0)
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"Provider '{name}' returned no choices: {str(data)[:300]}")
    return ((choices[0].get("message") or {}).get("content")) or ""


# --- kind: vertex-ai ---------------------------------------------------------
def call_vertex(name: str, system: str, user: str, cfg: dict[str, Any],
                usage: dict[str, Any] | None = None, model: str = "") -> str:
    g = cfg.get("providers", {}).get(name, {})
    if not g.get("enabled", False):
        raise RuntimeError(
            f"Provider '{name}' is not enabled yet. Configure it in config/qwen.local.json: "
            f"set providers.{name}.project, providers.{name}.credentials_file (your Vertex "
            f"service-account JSON) or GOOGLE_APPLICATION_CREDENTIALS, providers.{name}.models "
            f"(tier ids), `pip install google-genai`, and providers.{name}.enabled=true."
        )
    try:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "Vertex provider needs the SDK: `pip install google-genai` (into the venv)."
        ) from exc

    # Point Application Default Credentials at the service-account JSON if configured and
    # not already set in the environment (keeps the secret path out of committed config).
    creds_file = g.get("credentials_file", "")
    if creds_file and not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds_file

    project = g.get("project") or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    if not project:
        raise RuntimeError(
            f"Set providers.{name}.project (or GOOGLE_CLOUD_PROJECT) for Vertex AI."
        )
    client = genai.Client(
        vertexai=g.get("vertexai", True),
        project=project,
        location=g.get("location", "us-central1"),
    )
    gen_kwargs: dict[str, Any] = {"system_instruction": system}
    opts = _sampling_options(g)
    for key in ("temperature", "top_p", "max_output_tokens"):
        if key in opts:
            gen_kwargs[key] = opts[key]
    t0 = time.monotonic()
    resp = client.models.generate_content(
        model=_resolve_model(g, model, "gemini-2.5-flash"),
        contents=user,
        config=types.GenerateContentConfig(**gen_kwargs),
    )
    if usage is not None:
        usage["duration_s"] = round(time.monotonic() - t0, 3)
        um = getattr(resp, "usage_metadata", None)
        if um is not None:
            usage["tokens_in"] = int(getattr(um, "prompt_token_count", 0) or 0)
            usage["tokens_out"] = int(getattr(um, "candidates_token_count", 0) or 0)
    return resp.text or ""


# --- built-in names (standard signature; tests may stub these directly) -----
def call_qwen(system: str, user: str, cfg: dict[str, Any],
              usage: dict[str, Any] | None = None, model: str = "") -> str:
    return call_ollama("qwen", system, user, cfg, usage, model)


def call_gemini(system: str, user: str, cfg: dict[str, Any],
                usage: dict[str, Any] | None = None, model: str = "") -> str:
    return call_vertex("gemini", system, user, cfg, usage, model)


def call_openai(system: str, user: str, cfg: dict[str, Any],
                usage: dict[str, Any] | None = None, model: str = "") -> str:
    return call_openai_compatible("openai", system, user, cfg, usage, model)


PROVIDERS: dict[str, Callable[..., str]] = {
    "qwen": call_qwen,
    "gemini": call_gemini,
    "openai": call_openai,
}

_KIND_HANDLERS: dict[str, Callable[..., str]] = {
    "ollama-local": call_ollama,
    "openai-compatible": call_openai_compatible,
    "openai-api": call_openai_compatible,   # alias (the built-in `openai` entry uses it)
    "vertex-ai": call_vertex,
}


def resolve(name: str, cfg: dict[str, Any]) -> Callable[..., str] | None:
    """Return a standard-signature handler for `name`, or None if unknown.

    Built-in names win (so tests can stub PROVIDERS). Any other name is looked up in
    config `providers.<name>.kind` — this is what lets users add e.g. "groq" or
    "lmstudio" without touching code.
    """
    if name in PROVIDERS:
        return PROVIDERS[name]
    kind = cfg.get("providers", {}).get(name, {}).get("kind", "")
    handler = _KIND_HANDLERS.get(kind)
    if handler is None:
        return None

    def _bound(system: str, user: str, c: dict[str, Any],
               usage: dict[str, Any] | None = None, model: str = "",
               _n: str = name) -> str:
        return handler(_n, system, user, c, usage, model)

    return _bound


def provider_names(cfg: dict[str, Any] | None = None) -> list[str]:
    """Built-in provider names plus any config-defined provider with a known kind."""
    names = set(PROVIDERS.keys())
    for name, entry in (cfg or {}).get("providers", {}).items():
        if isinstance(entry, dict) and entry.get("kind") in _KIND_HANDLERS:
            names.add(name)
    return sorted(names)
