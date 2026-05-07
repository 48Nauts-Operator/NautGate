"""Week 1b — OpenAI Responses ↔ OpenAI Chat translation.

Responses is the newer shape: `input` + `instructions` instead of Chat's
`messages`, and the response is `output: [{type: "message", content: [{type: "output_text", text: ...}]}]`
plus a flattened `output_text` convenience field.

Reference: https://platform.openai.com/docs/api-reference/responses

Streaming translation maps OpenAI Chat SSE → Responses SSE event set:
  response.created → response.output_item.added → response.content_part.added
  → response.output_text.delta (per chunk) → response.output_text.done
  → response.content_part.done → response.output_item.done → response.completed
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

# OpenAI finish_reason → Responses status
_FINISH_TO_STATUS: dict[str, str] = {
    "stop": "completed",
    "length": "incomplete",
    "tool_calls": "completed",
    "content_filter": "completed",
}


def _normalize_input_block(blk: dict) -> dict | None:
    """Responses uses `input_text` blocks; OpenAI Chat uses `text`."""
    bt = blk.get("type")
    if bt in ("input_text", "text"):
        return {"type": "text", "text": blk.get("text", "")}
    if bt in ("input_image", "image_url"):
        # Responses.input_image carries `image_url` directly.
        url = blk.get("image_url") or blk.get("url")
        if isinstance(url, dict):  # already shaped
            return {"type": "image_url", "image_url": url}
        if isinstance(url, str):
            return {"type": "image_url", "image_url": {"url": url}}
    return None


def request_to_openai_chat(payload: dict) -> dict:
    """Translate an OpenAI Responses request into an OpenAI Chat request.

    Shape changes:
      - `instructions` → first message with role=system
      - `input` (str | list) → `messages` (single user-message in the str case;
        otherwise normalized blocks for each turn)
    Pass-through: model, temperature, top_p, max_output_tokens → max_tokens, stream, tools, etc.
    """
    out: dict[str, Any] = {"model": payload.get("model")}
    msgs: list[dict] = []

    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions:
        msgs.append({"role": "system", "content": instructions})

    inp = payload.get("input")
    if isinstance(inp, str):
        if inp:
            msgs.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict):
                continue
            role = item.get("role", "user")
            if role not in ("user", "assistant", "system", "developer"):
                role = "user"
            if role == "developer":  # Responses API "developer" ≈ system
                role = "system"
            content = item.get("content")
            if isinstance(content, str):
                msgs.append({"role": role, "content": content})
            elif isinstance(content, list):
                blocks = [_normalize_input_block(b) for b in content if isinstance(b, dict)]
                blocks = [b for b in blocks if b is not None]
                if len(blocks) == 1 and blocks[0].get("type") == "text":
                    msgs.append({"role": role, "content": blocks[0]["text"]})
                elif blocks:
                    msgs.append({"role": role, "content": blocks})
    out["messages"] = msgs

    # Pass-through scalars + renames.
    for k in ("temperature", "top_p", "stream", "tools", "tool_choice", "metadata"):
        if k in payload:
            out[k] = payload[k]
    if "max_output_tokens" in payload:
        out["max_tokens"] = payload["max_output_tokens"]
    elif "max_tokens" in payload:
        out["max_tokens"] = payload["max_tokens"]
    return out


def response_to_openai_responses(openai_resp: dict, model: str | None = None) -> dict:
    """Translate a non-streaming OpenAI Chat response into an OpenAI Responses response."""
    choices = openai_resp.get("choices") or []
    msg = (choices[0] if choices else {}).get("message") or {}
    text = msg.get("content") or ""
    finish = (choices[0] if choices else {}).get("finish_reason") or "stop"
    usage = openai_resp.get("usage") or {}

    response_id = openai_resp.get("id") or f"resp_{uuid.uuid4().hex[:24]}"
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": _FINISH_TO_STATUS.get(finish, "completed"),
        "model": openai_resp.get("model") or model,
        "output": [
            {
                "id": f"msg_{uuid.uuid4().hex[:24]}",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}] if text else [],
            }
        ],
        "output_text": text,
        "usage": {
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
            "total_tokens": (
                (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)
            )
            or None,
        },
    }


# --- Streaming: OpenAI Chat SSE → OpenAI Responses SSE --------------------


class ResponsesStreamTranslator:
    """OpenAI Chat SSE → OpenAI Responses SSE.

    Emits the eight Responses events in order, lazily — `response.created` etc.
    only fire once we see real content, so a stream that errors before any
    content still produces clean terminator events on finish().
    """

    def __init__(self, model: str):
        self.model = model
        self._buf = bytearray()
        self._response_id = f"resp_{uuid.uuid4().hex[:24]}"
        self._item_id = f"msg_{uuid.uuid4().hex[:24]}"
        self._created_emitted = False
        self._item_added = False
        self._part_added = False
        self._part_done = False
        self._item_done = False
        self._completed = False
        self._text_buf: list[str] = []
        self._input_tokens: int | None = None
        self._output_tokens: int | None = None
        self._finish_reason: str | None = None

    @staticmethod
    def _emit(event: str, data: dict) -> bytes:
        return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()

    def _response_obj(self, *, status: str) -> dict:
        return {
            "id": self._response_id,
            "object": "response",
            "created_at": int(time.time()),
            "status": status,
            "model": self.model,
            "output": (
                [
                    {
                        "id": self._item_id,
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "".join(self._text_buf)}],
                    }
                ]
                if self._text_buf
                else []
            ),
            "output_text": "".join(self._text_buf),
            "usage": {
                "input_tokens": self._input_tokens,
                "output_tokens": self._output_tokens,
                "total_tokens": ((self._input_tokens or 0) + (self._output_tokens or 0)) or None,
            }
            if status == "completed"
            else None,
        }

    def _ensure_created(self) -> list[bytes]:
        if self._created_emitted:
            return []
        self._created_emitted = True
        return [
            self._emit(
                "response.created",
                {"type": "response.created", "response": self._response_obj(status="in_progress")},
            )
        ]

    def _ensure_item_added(self) -> list[bytes]:
        if self._item_added:
            return []
        self._item_added = True
        return [
            self._emit(
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {
                        "id": self._item_id,
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                    },
                },
            )
        ]

    def _ensure_part_added(self) -> list[bytes]:
        if self._part_added:
            return []
        self._part_added = True
        return [
            self._emit(
                "response.content_part.added",
                {
                    "type": "response.content_part.added",
                    "item_id": self._item_id,
                    "output_index": 0,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": ""},
                },
            )
        ]

    def _emit_delta(self, text: str) -> bytes:
        return self._emit(
            "response.output_text.delta",
            {
                "type": "response.output_text.delta",
                "item_id": self._item_id,
                "output_index": 0,
                "content_index": 0,
                "delta": text,
            },
        )

    def _close(self) -> list[bytes]:
        out: list[bytes] = []
        if not self._part_done and self._part_added:
            self._part_done = True
            out.append(
                self._emit(
                    "response.output_text.done",
                    {
                        "type": "response.output_text.done",
                        "item_id": self._item_id,
                        "output_index": 0,
                        "content_index": 0,
                        "text": "".join(self._text_buf),
                    },
                )
            )
            out.append(
                self._emit(
                    "response.content_part.done",
                    {
                        "type": "response.content_part.done",
                        "item_id": self._item_id,
                        "output_index": 0,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": "".join(self._text_buf)},
                    },
                )
            )
        if not self._item_done and self._item_added:
            self._item_done = True
            out.append(
                self._emit(
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": {
                            "id": self._item_id,
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "".join(self._text_buf)}],
                        },
                    },
                )
            )
        if not self._completed:
            self._completed = True
            # Make sure response.created fired even on an empty stream — clients
            # parsing the event list expect to see it before completed.
            if not self._created_emitted:
                out = self._ensure_created() + out
            out.append(
                self._emit(
                    "response.completed",
                    {
                        "type": "response.completed",
                        "response": self._response_obj(status="completed"),
                    },
                )
            )
        return out

    # ---- public ----

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buf.extend(chunk)
        events_out: list[bytes] = []
        while b"\n\n" in self._buf:
            blob, _, rest = bytes(self._buf).partition(b"\n\n")
            self._buf = bytearray(rest)
            for line in blob.split(b"\n"):
                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str.startswith("data:"):
                    continue
                payload_str = line_str[5:].strip()
                if payload_str == "[DONE]":
                    events_out.extend(self._close())
                    continue
                try:
                    obj = json.loads(payload_str)
                except json.JSONDecodeError:
                    continue
                events_out.extend(self._handle_chunk(obj))
        return events_out

    def finish(self) -> list[bytes]:
        return self._close()

    # ---- internals ----

    def _handle_chunk(self, obj: dict) -> list[bytes]:
        out: list[bytes] = []

        usage = obj.get("usage")
        if isinstance(usage, dict):
            self._input_tokens = usage.get("prompt_tokens", self._input_tokens)
            self._output_tokens = usage.get("completion_tokens", self._output_tokens)

        choices = obj.get("choices") or []
        if not choices:
            return out
        ch = choices[0]
        delta = ch.get("delta") or {}

        text = delta.get("content")
        if isinstance(text, str) and text:
            out.extend(self._ensure_created())
            out.extend(self._ensure_item_added())
            out.extend(self._ensure_part_added())
            self._text_buf.append(text)
            out.append(self._emit_delta(text))

        if ch.get("finish_reason"):
            self._finish_reason = ch["finish_reason"]
            # Don't emit terminators yet — the [DONE] sentinel triggers _close().
            # If upstream sends finish_reason but no [DONE], finish() handles it.

        return out
