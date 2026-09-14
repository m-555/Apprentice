"""Whether a local model reasons privately before it answers.

Models served by the local llama.cpp router launch with `--reasoning off`, which
closes the chat template's thinking channel. A reasoning-trained model then has
nowhere to put its deliberation except the visible answer, so an open-ended request
can come back as paragraphs of "wait, let me reconsider" instead of a reply.
llama.cpp accepts `chat_template_kwargs.enable_thinking` per request, so the choice
costs no model reload.

Two paths need it and they are NOT the same:

  * `delegate` builds its own request body, so it carries the preference itself and
    always wins over whatever the router defaults to.
  * chat / run / assign go through OpenCode, which builds the body for us. The only
    way to reach those is the router's own default — hence set_router().

UNSET (None) means "follow the router", which is what a fresh install does and what
keeps behaviour identical to before this module existed.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

_ON = {"1", "on", "true", "yes"}
_OFF = {"0", "off", "false", "no"}

_preference: bool | None = None


def coerce(value: Any, fallback: bool | None = None) -> bool | None:
    """Accept the spellings a UI, config file or JSON body might send."""
    if value is None or value == "":
        return fallback
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _ON:
        return True
    if text in _OFF:
        return False
    if text in ("auto", "router", "default"):
        return None
    return fallback


def preference() -> bool | None:
    """True/False when the user has chosen; None to follow the router's default."""
    return _preference


def set_preference(value: Any) -> bool | None:
    global _preference
    _preference = coerce(value, _preference)
    return _preference


def apply_to_body(body: dict[str, Any], enabled: bool | None = ...) -> dict[str, Any]:
    """Carry the preference on a request we build ourselves.

    Mutates and returns `body`. With no preference the body is untouched, so the
    router's default decides and nothing changes for an install that never set this.
    """
    choice = preference() if enabled is ... else enabled
    if choice is None:
        return body
    kwargs = body.get("chat_template_kwargs")
    if not isinstance(kwargs, dict):
        kwargs = {}
    kwargs["enable_thinking"] = bool(choice)
    body["chat_template_kwargs"] = kwargs
    return body


def router_base(cfg: dict[str, Any]) -> str:
    """The router's root URL, derived from the configured OpenAI-compatible base."""
    host = (cfg.get("runner", {}) or {}).get("host", "") or ""
    host = host.rstrip("/")
    if host.endswith("/v1"):
        host = host[: -len("/v1")]
    return host


def set_router(cfg: dict[str, Any], enabled: bool, timeout_s: float = 5.0) -> bool | None:
    """Set the router-wide default so OpenCode-backed paths follow it too.

    Best effort: the router may simply not be running, and a chat session must not
    fail because a preference could not be published. Returns the router's new value,
    or None when it could not be reached.
    """
    base = router_base(cfg)
    if not base:
        return None
    request = urllib.request.Request(
        f"{base}/thinking",
        data=json.dumps({"enabled": bool(enabled)}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return coerce(json.loads(response.read().decode("utf-8")).get("thinking"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None
