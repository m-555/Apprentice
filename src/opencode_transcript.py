"""Metadata-first projection of OpenCode events into public Apprentice chat."""
from __future__ import annotations


def internal(info: dict) -> bool:
    return info.get("role") == "assistant" and (info.get("summary") or
        info.get("mode") in ("compaction", "summary", "title"))


class Transcript:
    def __init__(self, emit):
        self.emit = emit
        self.messages, self.parts = {}, {}

    def apply(self, event: dict):
        kind, props = event.get("type"), event.get("properties", {})
        if kind == "message.updated":
            info = props["info"]
            self.messages[info["id"]] = info
            if internal(info):
                self.emit({"type": "message_remove", "message_id": info["id"]})
            else:
                for part in list(self.parts.values()):
                    if part["messageID"] == info["id"]:
                        self.project(part)
        elif kind == "message.part.updated":
            part = props["part"]
            if part["type"] == "reasoning":
                return
            self.parts[part["id"]] = part
            self.project(part)
        elif kind == "message.part.delta":
            part = self.parts.get(props["partID"])
            if part and part["type"] == "text" and props.get("field") == "text":
                part["text"] = part.get("text", "") + props["delta"]
                self.project(part)
        elif kind == "message.part.removed":
            self.parts.pop(props["partID"], None)
            self.emit({"type": "part_remove", "id": props["partID"]})
        elif kind == "message.removed":
            self.messages.pop(props["messageID"], None)
            self.emit({"type": "message_remove", "message_id": props["messageID"]})

    def project(self, part: dict):
        info = self.messages.get(part["messageID"])
        if not info or internal(info) or info["role"] == "user":
            return  # The controller publishes the original, unwrapped user request.
        base = {"id": part["id"], "message_id": part["messageID"]}
        if part["type"] == "text" and not part.get("synthetic") and not part.get("ignored"):
            self.emit({"type": "message_part", "role": "assistant", "text": part.get("text", ""), **base})
        elif part["type"] == "tool":
            state = part["state"]
            self.emit({"type": "tool_part", "tool": part["tool"], "status": state["status"],
                "args": state.get("input", {}), "text": (state.get("output") or state.get("error") or "")[:10000], **base})

    def history(self, messages: list[dict]):
        for message in messages:
            self.apply({"type": "message.updated", "properties": {"info": message["info"]}})
            for part in message["parts"]:
                self.apply({"type": "message.part.updated", "properties": {"part": part}})


def final_answer(messages: list[dict]) -> str:
    for message in reversed(messages):
        info = message["info"]
        if info.get("role") != "assistant" or internal(info):
            continue
        if info.get("error"):
            raise RuntimeError(str(info["error"]))
        if info.get("finish") not in ("stop", "end_turn"):
            raise RuntimeError("The model stopped without a completed final answer.")
        text = "\n".join(p.get("text", "") for p in message["parts"]
            if p["type"] == "text" and not p.get("synthetic") and not p.get("ignored")).strip()
        if text:
            return text
        raise RuntimeError("The model ended the turn without a user-facing answer.")
    raise RuntimeError("OpenCode returned no assistant answer.")
