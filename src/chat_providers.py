"""Multi-turn chat WITH TOOLS, normalized across providers — the agent's model layer.

`providers.py` is single-shot (system+user -> text), which is all `delegate` needs. An
agent needs conversation state and tool calls, and every provider spells those
differently. This module hides that behind one shape:

    chat(messages, tools, cfg, provider, model, usage) -> AssistantTurn

    AssistantTurn.content     assistant text (may be "")
    AssistantTurn.tool_calls  [ToolCall(id, name, args: dict), ...]

Message dicts are OpenAI-shaped and translated per provider:
    {"role": "system"|"user"|"assistant"|"tool", "content": str,
     "tool_calls": [...]?, "tool_call_id": str?, "name": str?}

Two tool protocols, chosen by `providers.<name>.tool_protocol`:
  • "native" (default): the provider's own function-calling API.
  • "text": for models with no tool support — the model is told to emit a single fenced
    ```action {json}``` block, which we parse into the same ToolCall shape. Slower and
    less reliable, but it makes ANY chat model usable as an agent.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

try:
    from . import providers
except ImportError:
    import providers


#: Private key on an OpenAI-shaped tool_call dict, carrying Gemini's opaque per-call
#: thought signature (base64 of the SDK's bytes, so transcripts stay JSON). Gemini 3
#: hard-rejects a follow-up request whose functionCall parts have lost it, so it must
#: survive the round-trip through history. Providers that are sent history verbatim
#: strip it — see `_without_private_keys`.
SIGNATURE_KEY = "_thought_signature"


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    #: Opaque provider state to replay on the next request (Gemini only; base64 str).
    thought_signature: str | None = None


@dataclass
class AssistantTurn:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


def _without_private_keys(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """History minus our private per-call bookkeeping (see SIGNATURE_KEY).

    Only for providers we hand the history to VERBATIM: OpenAI-compatible endpoints
    reject unknown fields inside `tool_calls[]` outright, so a Gemini-flavoured session
    that later switches provider — the escalation ladder does exactly that — would 400.
    Providers that translate history into their own wire shape never see these keys.
    Copies only the messages that carry one; the rest are passed through untouched.
    """
    out = []
    for m in messages:
        calls = m.get("tool_calls")
        if calls and any(SIGNATURE_KEY in tc for tc in calls):
            m = {**m, "tool_calls": [{k: v for k, v in tc.items() if k != SIGNATURE_KEY}
                                     for tc in calls]}
        out.append(m)
    return out


def _coerce_args(raw: Any) -> dict[str, Any]:
    """Providers disagree: Ollama returns a dict, OpenAI a JSON string. Accept both,
    and never raise on malformed args — the loop reports the error back to the model."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except json.JSONDecodeError:
            return {"__raw__": raw}
    return {}


def _new_id(prefix: str, i: int) -> str:
    return f"{prefix}_{i}_{int(time.time() * 1000) % 100000}"


# --- streaming --------------------------------------------------------------
# `on_delta(text)` is called with each token fragment as it arrives, so a UI can show
# the answer forming instead of a dead pause. Streaming is transparent to callers: the
# same AssistantTurn is returned either way.

def _stream_lines(url: str, body: dict[str, Any], headers: dict[str, str],
                  timeout_s: int):
    """POST and yield decoded response lines as they arrive."""
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                yield line


class ToolCallAccumulator:
    """Reassemble OpenAI-style streamed tool calls.

    Deltas arrive fragmented and keyed by `index`: the function name usually appears
    once, then `arguments` in pieces that must be concatenated in order before parsing.
    """

    def __init__(self) -> None:
        self._by_index: dict[int, dict[str, str]] = {}

    def add(self, delta_calls: list[dict[str, Any]] | None) -> None:
        for tc in delta_calls or []:
            idx = int(tc.get("index", 0) or 0)
            slot = self._by_index.setdefault(idx, {"id": "", "name": "", "args": ""})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] = fn["name"]
            if fn.get("arguments"):
                slot["args"] += fn["arguments"]

    def finish(self) -> list[ToolCall]:
        out: list[ToolCall] = []
        for i, (idx, slot) in enumerate(sorted(self._by_index.items())):
            if not slot["name"]:
                continue
            out.append(ToolCall(slot["id"] or _new_id("oai", idx or i),
                                slot["name"], _coerce_args(slot["args"])))
        return out


def _streaming_enabled(p: dict[str, Any], on_delta) -> bool:
    """Stream only when someone is listening AND the provider allows it."""
    return bool(on_delta) and bool(p.get("stream", True))


# --- text protocol (models without native tool calling) ---------------------
_ACTION_RE = re.compile(r"```(?:action|json)?\s*\n(\{.*?\})\s*\n```", re.DOTALL)

TEXT_PROTOCOL_INSTRUCTIONS = (
    "\n\n--- HOW TO ACT ---\n"
    "You cannot call functions directly. To use a tool, reply with EXACTLY ONE fenced "
    "block and nothing else:\n"
    "```action\n"
    '{"tool": "<tool_name>", "args": {<arguments>}}\n'
    "```\n"
    "Wait for the result before the next action. When the work is done, reply with a "
    "plain-text summary and NO action block."
)


def parse_text_action(text: str) -> AssistantTurn:
    """Parse a text-protocol reply into the normalized shape."""
    m = _ACTION_RE.search(text or "")
    if not m:
        return AssistantTurn(content=(text or "").strip())
    try:
        obj = json.loads(m.group(1))
    except json.JSONDecodeError:
        return AssistantTurn(content=(text or "").strip())
    name = obj.get("tool") or obj.get("name") or ""
    if not name:
        return AssistantTurn(content=(text or "").strip())
    args = obj.get("args") if isinstance(obj.get("args"), dict) else {}
    before = (text[:m.start()] or "").strip()
    return AssistantTurn(content=before,
                         tool_calls=[ToolCall(_new_id("txt", 0), name, args)])


def _flatten_for_text_protocol(messages: list[dict[str, Any]],
                               tools: list[dict[str, Any]]) -> tuple[str, str]:
    """Render the conversation as (system, user) for a provider with no tool API."""
    sys_parts = [m["content"] for m in messages if m.get("role") == "system"]
    catalog = ["\n\n--- AVAILABLE TOOLS ---"]
    for t in tools:
        fn = t.get("function", t)
        params = ", ".join((fn.get("parameters", {}).get("properties") or {}).keys())
        catalog.append(f"- {fn.get('name')}({params}): {fn.get('description','')}")
    system = "\n".join(sys_parts) + "\n".join(catalog) + TEXT_PROTOCOL_INSTRUCTIONS

    convo = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        if role == "tool":
            convo.append(f"[TOOL RESULT]\n{m.get('content','')}")
        elif role == "assistant":
            calls = m.get("tool_calls") or []
            if calls:
                c = calls[0]
                fn = c.get("function", c)
                convo.append(f"[YOUR ACTION] {fn.get('name')} "
                             f"{json.dumps(_coerce_args(fn.get('arguments')))}")
            if m.get("content"):
                convo.append(f"[YOU] {m['content']}")
        else:
            convo.append(f"[USER] {m.get('content','')}")
    return system, "\n\n".join(convo)


# --- kind: ollama-local -----------------------------------------------------
def _chat_ollama(name: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                 cfg: dict[str, Any], usage: dict[str, Any] | None,
                 model: str, on_delta=None) -> AssistantTurn:
    p = cfg.get("providers", {}).get(name, {})
    host = p.get("host") or cfg.get("runner", {}).get("host", "http://127.0.0.1:11434")
    model_id = providers._resolve_model(
        p, model, cfg.get("worker_model", {}).get("tag", "qwen3-coder-next"))
    stream = _streaming_enabled(p, on_delta)
    body: dict[str, Any] = {
        "model": model_id,
        "messages": _ollama_messages(messages),
        "stream": stream,
        "keep_alive": p.get("keep_alive") or cfg.get("keep_alive", {}).get("value", "30m"),
    }
    if tools:
        body["tools"] = tools
    opts = providers._sampling_options(p)
    if opts:
        body["options"] = opts

    t0 = time.monotonic()
    try:
        if stream:
            # Ollama streams NDJSON: each line is a partial message; the last has done=true
            # and carries the token counts. tool_calls appear whole (not fragmented).
            content_parts: list[str] = []
            raw_calls: list[dict[str, Any]] = []
            final: dict[str, Any] = {}
            for line in _stream_lines(f"{host}/api/chat", body, {},
                                      int(p.get("timeout_s", 600))):
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg_part = chunk.get("message") or {}
                piece = msg_part.get("content") or ""
                if piece:
                    content_parts.append(piece)
                    on_delta(piece)
                if msg_part.get("tool_calls"):
                    raw_calls.extend(msg_part["tool_calls"])
                if chunk.get("done"):
                    final = chunk
            data = {**final, "message": {"content": "".join(content_parts),
                                         "tool_calls": raw_calls}}
        else:
            data = providers._post_json(f"{host}/api/chat", body, {},
                                        int(p.get("timeout_s", 600)))
    except urllib.error.HTTPError as exc:
        # A 400 here is almost always a malformed message list, NOT an unreachable
        # server — surface the body so the cause is visible instead of guessed.
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        raise RuntimeError(f"Ollama at {host} rejected the request "
                           f"(HTTP {exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach Ollama at {host} ({exc}). "
                           f"Is the server running?") from exc
    if usage is not None:
        usage["tokens_in"] = int(data.get("prompt_eval_count", 0) or 0)
        usage["tokens_out"] = int(data.get("eval_count", 0) or 0)
        usage["duration_s"] = round(time.monotonic() - t0, 3)
    msg = data.get("message") or {}
    calls = []
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function", {})
        calls.append(ToolCall(tc.get("id") or _new_id("oll", i),
                              fn.get("name", ""), _coerce_args(fn.get("arguments"))))
    return AssistantTurn(msg.get("content") or "", calls)


def _ollama_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate the canonical (OpenAI-shaped) history into what Ollama accepts.

    Two differences that cause a hard HTTP 400 if ignored:
      • `function.arguments` must be an OBJECT here (OpenAI uses a JSON string),
      • there is no `tool_call_id` field — tool results are matched positionally.
    """
    out = []
    for m in messages:
        msg: dict[str, Any] = {"role": m["role"], "content": m.get("content", "") or ""}
        if m.get("tool_calls"):
            msg["tool_calls"] = [
                {"type": "function",
                 "function": {"name": tc.get("function", {}).get("name", ""),
                              "arguments": _coerce_args(
                                  tc.get("function", {}).get("arguments"))}}
                for tc in m["tool_calls"]]
        if m["role"] == "tool" and m.get("name"):
            msg["name"] = m["name"]
        out.append(msg)
    return out


# --- kind: openai-compatible ------------------------------------------------
def _openai_stream(url: str, body: dict[str, Any], headers: dict[str, str],
                   timeout_s: int, on_delta, usage: dict[str, Any] | None
                   ) -> AssistantTurn:
    """Consume an OpenAI-style SSE stream into a complete AssistantTurn.

    Lines look like `data: {json}`, terminated by `data: [DONE]`. Text arrives in
    `delta.content`; tool calls arrive fragmented in `delta.tool_calls` and are
    reassembled by index.
    """
    content_parts: list[str] = []
    acc = ToolCallAccumulator()
    body = {**body, "stream": True, "stream_options": {"include_usage": True}}
    for line in _stream_lines(url, body, headers, timeout_s):
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if chunk.get("usage") and usage is not None:
            u = chunk["usage"]
            usage["tokens_in"] = int(u.get("prompt_tokens", 0) or 0)
            usage["tokens_out"] = int(u.get("completion_tokens", 0) or 0)
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            piece = delta.get("content")
            if piece:
                content_parts.append(piece)
                on_delta(piece)
            acc.add(delta.get("tool_calls"))
    return AssistantTurn("".join(content_parts), acc.finish())


def _chat_openai(name: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                 cfg: dict[str, Any], usage: dict[str, Any] | None,
                 model: str, on_delta=None) -> AssistantTurn:
    p = cfg.get("providers", {}).get(name, {})
    if not p.get("enabled", False):
        raise RuntimeError(f"Provider '{name}' is not enabled (config providers.{name}).")
    base_url = (p.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    model_id = providers._resolve_model(p, model)
    if not model_id:
        raise RuntimeError(f"Set providers.{name}.model (or models/default_model).")
    headers = {}
    api_key = os.environ.get(p.get("api_key_env", "OPENAI_API_KEY"), "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    body: dict[str, Any] = {"model": model_id,
                            "messages": _without_private_keys(messages)}
    if tools:
        body["tools"] = tools
    opts = providers._sampling_options(p)
    for key in ("temperature", "top_p", "max_tokens"):
        if key in opts:
            body[key] = opts[key]
    t0 = time.monotonic()
    try:
        if _streaming_enabled(p, on_delta):
            turn = _openai_stream(f"{base_url}/chat/completions", body, headers,
                                  int(p.get("timeout_s", 300)), on_delta, usage)
            if usage is not None:
                usage["duration_s"] = round(time.monotonic() - t0, 3)
            return turn
        data = providers._post_json(f"{base_url}/chat/completions", body, headers,
                                    int(p.get("timeout_s", 300)))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        raise RuntimeError(f"Provider '{name}' HTTP {exc.code} from {base_url}: "
                           f"{detail}") from exc
    if usage is not None:
        usage["duration_s"] = round(time.monotonic() - t0, 3)
        u = data.get("usage") or {}
        usage["tokens_in"] = int(u.get("prompt_tokens", 0) or 0)
        usage["tokens_out"] = int(u.get("completion_tokens", 0) or 0)
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"Provider '{name}' returned no choices.")
    msg = choices[0].get("message") or {}
    calls = []
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function", {})
        calls.append(ToolCall(tc.get("id") or _new_id("oai", i),
                              fn.get("name", ""), _coerce_args(fn.get("arguments"))))
    return AssistantTurn(msg.get("content") or "", calls)


# --- kind: vertex-ai --------------------------------------------------------
def _chat_vertex(name: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                 cfg: dict[str, Any], usage: dict[str, Any] | None,
                 model: str, on_delta=None) -> AssistantTurn:
    # NOTE: Vertex replies are not streamed yet — the SDK's streaming shape differs
    # enough to deserve its own change. The signature matches so callers stay uniform;
    # text simply arrives in one piece for this provider.
    del on_delta
    g = cfg.get("providers", {}).get(name, {})
    if not g.get("enabled", False):
        raise RuntimeError(f"Provider '{name}' is not enabled (config providers.{name}).")
    try:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Vertex provider needs: pip install google-genai") from exc

    creds = g.get("credentials_file", "")
    if creds and not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds
    project = g.get("project") or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    if not project:
        raise RuntimeError(f"Set providers.{name}.project for Vertex AI.")
    client = genai.Client(vertexai=g.get("vertexai", True), project=project,
                          location=g.get("location", "us-central1"))

    system_txt = "\n".join(m["content"] for m in messages if m.get("role") == "system")
    contents: list[Any] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        if role == "tool":
            contents.append(types.Content(role="user", parts=[types.Part.from_function_response(
                name=m.get("name") or "tool", response={"result": m.get("content", "")})]))
        elif role == "assistant":
            parts = []
            if m.get("content"):
                parts.append(types.Part.from_text(text=m["content"]))
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function", tc)
                part = types.Part.from_function_call(
                    name=fn.get("name", ""), args=_coerce_args(fn.get("arguments")))
                # Replay the signature the model gave us for THIS call, or Gemini 3
                # rejects the whole request rather than just ignoring the omission.
                sig = tc.get(SIGNATURE_KEY)
                if sig:
                    part.thought_signature = base64.b64decode(sig)
                parts.append(part)
            if parts:
                contents.append(types.Content(role="model", parts=parts))
        else:
            contents.append(types.Content(
                role="user", parts=[types.Part.from_text(text=m.get("content", ""))]))

    gen_kwargs: dict[str, Any] = {"system_instruction": system_txt}
    opts = providers._sampling_options(g)
    for key in ("temperature", "top_p", "max_output_tokens"):
        if key in opts:
            gen_kwargs[key] = opts[key]
    if tools:
        gen_kwargs["tools"] = [types.Tool(function_declarations=[
            types.FunctionDeclaration(
                name=t["function"]["name"],
                description=t["function"].get("description", ""),
                parameters=t["function"].get("parameters", {"type": "object"}),
            ) for t in tools])]

    t0 = time.monotonic()
    resp = client.models.generate_content(
        model=providers._resolve_model(g, model, "gemini-2.5-flash"),
        contents=contents, config=types.GenerateContentConfig(**gen_kwargs))
    if usage is not None:
        usage["duration_s"] = round(time.monotonic() - t0, 3)
        um = getattr(resp, "usage_metadata", None)
        if um is not None:
            usage["tokens_in"] = int(getattr(um, "prompt_token_count", 0) or 0)
            usage["tokens_out"] = int(getattr(um, "candidates_token_count", 0) or 0)

    text_parts, calls = [], []
    for cand in (getattr(resp, "candidates", None) or []):
        for i, part in enumerate(getattr(cand.content, "parts", None) or []):
            fc = getattr(part, "function_call", None)
            if fc is not None:
                # Keep the signature: Gemini 3 refuses the follow-up request (400
                # INVALID_ARGUMENT) if the functionCall it gets back has lost it.
                sig = getattr(part, "thought_signature", None)
                calls.append(ToolCall(_new_id("vtx", i), fc.name, dict(fc.args or {}),
                                      base64.b64encode(sig).decode() if sig else None))
            elif getattr(part, "text", None):
                text_parts.append(part.text)
    return AssistantTurn("".join(text_parts), calls)


_KIND_CHAT = {
    "ollama-local": _chat_ollama,
    "openai-compatible": _chat_openai,
    "openai-api": _chat_openai,
    "vertex-ai": _chat_vertex,
}

# Built-in provider names map to their kind (config may omit `kind` for these).
_BUILTIN_KIND = {"qwen": "openai-compatible", "gemini": "vertex-ai",
                 "openai": "openai-compatible"}


def provider_kind(cfg: dict[str, Any], provider: str) -> str:
    return cfg.get("providers", {}).get(provider, {}).get("kind") \
        or _BUILTIN_KIND.get(provider, "")


def supports_chat(cfg: dict[str, Any], provider: str) -> bool:
    return provider_kind(cfg, provider) in _KIND_CHAT


def chat(messages: list[dict[str, Any]], tools: list[dict[str, Any]],
         cfg: dict[str, Any], provider: str, model: str = "",
         usage: dict[str, Any] | None = None, on_delta=None) -> AssistantTurn:
    """One assistant turn. Uses the provider's native tool API, or the text protocol
    when `providers.<name>.tool_protocol == "text"` (or no tools were supplied).

    `on_delta(text)` — optional. Called with each token fragment as it arrives so a UI
    can render the reply as it forms. The return value is identical either way, so
    callers that don't pass it are unaffected.
    """
    kind = provider_kind(cfg, provider)
    handler = _KIND_CHAT.get(kind)
    if handler is None:
        raise ValueError(
            f"Provider '{provider}' has no chat handler (kind={kind or 'unset'}). "
            f"Supported kinds: {', '.join(sorted(_KIND_CHAT))}.")

    p = cfg.get("providers", {}).get(provider, {})
    if tools and p.get("tool_protocol", "native") == "text":
        # The text protocol needs the WHOLE reply before it can be parsed into an
        # action, so streaming its fragments to the UI would leak raw protocol JSON.
        system, user = _flatten_for_text_protocol(messages, tools)
        turn = handler(provider, [{"role": "system", "content": system},
                                  {"role": "user", "content": user}], [], cfg,
                       usage, model, None)
        return parse_text_action(turn.content)
    return handler(provider, messages, tools, cfg, usage, model, on_delta)
