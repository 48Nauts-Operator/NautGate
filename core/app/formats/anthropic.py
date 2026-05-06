"""Week 1b — Anthropic Messages ↔ OpenAI Chat translation.

NautRouter sidecar speaks OpenAI Chat. We translate inbound Anthropic Messages
requests into OpenAI Chat shape, forward them, and translate the response (and
SSE stream) back to Anthropic Messages shape so the caller sees their native
format end-to-end.

Reference:
  - Anthropic Messages: https://docs.anthropic.com/en/api/messages
  - OpenAI Chat:        https://platform.openai.com/docs/api-reference/chat

Translation is best-effort; unknown / unmapped fields fall through unchanged.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

# --- Stop reason mapping ---------------------------------------------------

# OpenAI finish_reason → Anthropic stop_reason
_FINISH_TO_STOP: dict[str, str] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "stop_sequence",  # rough mapping — Anthropic has no exact equivalent
}


def _normalize_content_blocks(content: object) -> list[dict]:
    """Anthropic accepts content as either a string or a list of typed blocks.
    OpenAI Chat accepts content as either a string or a list of typed blocks (image, text).
    We pass strings through and turn block lists into OpenAI-style multimodal blocks where possible.
    """
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content)}]
    out: list[dict] = []
    for blk in content:
        if not isinstance(blk, dict):
            continue
        bt = blk.get("type")
        if bt == "text":
            out.append({"type": "text", "text": blk.get("text", "")})
        elif bt == "image":
            # Anthropic: {"type":"image","source":{"type":"base64","media_type":"image/png","data":"..."}}
            # OpenAI:    {"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}
            src = blk.get("source") or {}
            if src.get("type") == "base64":
                url = f"data:{src.get('media_type', 'application/octet-stream')};base64,{src.get('data', '')}"
                out.append({"type": "image_url", "image_url": {"url": url}})
            elif src.get("type") == "url":
                out.append({"type": "image_url", "image_url": {"url": src.get("url", "")}})
        else:
            # Unknown block type — drop rather than 400.
            continue
    return out


# --- Request: Anthropic → OpenAI Chat -------------------------------------


def request_to_openai_chat(payload: dict) -> dict:
    """Translate an Anthropic Messages request into an OpenAI Chat request.

    Important shape changes:
      - `system` (top-level string) → first message with role=system
      - `messages` content blocks normalized for OpenAI multimodal

    Pass-through fields: model, temperature, top_p, top_k → top_p (top_k drops),
      max_tokens, stop_sequences → stop, stream, tools, tool_choice, metadata.
    """
    out: dict[str, Any] = {}
    out["model"] = payload.get("model")

    msgs: list[dict] = []
    system = payload.get("system")
    if isinstance(system, str) and system:
        msgs.append({"role": "system", "content": system})
    elif isinstance(system, list):
        # Anthropic's system can also be a list of text blocks.
        text = "\n".join(
            b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text"
        )
        if text:
            msgs.append({"role": "system", "content": text})

    for m in payload.get("messages") or []:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        content = m.get("content")
        if isinstance(content, str):
            msgs.append({"role": role, "content": content})
        else:
            blocks = _normalize_content_blocks(content)
            # Collapse single-text-block to a string for cleanliness.
            if len(blocks) == 1 and blocks[0].get("type") == "text":
                msgs.append({"role": role, "content": blocks[0]["text"]})
            else:
                msgs.append({"role": role, "content": blocks})

    out["messages"] = msgs

    # Pass-through scalars.
    for k in ("temperature", "top_p", "max_tokens", "stream", "metadata", "tools", "tool_choice"):
        if k in payload:
            out[k] = payload[k]

    if "stop_sequences" in payload:
        out["stop"] = payload["stop_sequences"]

    return out


# --- Response: OpenAI Chat → Anthropic Messages ---------------------------


def response_to_anthropic(openai_resp: dict, model: str | None = None) -> dict:
    """Translate a non-streaming OpenAI Chat response into an Anthropic Messages response."""
    choices = openai_resp.get("choices") or []
    msg = (choices[0] if choices else {}).get("message") or {}
    content = msg.get("content") or ""
    finish = (choices[0] if choices else {}).get("finish_reason")
    usage = openai_resp.get("usage") or {}

    return {
        "id": openai_resp.get("id") or f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": content}] if content else [],
        "model": openai_resp.get("model") or model,
        "stop_reason": _FINISH_TO_STOP.get(finish, finish),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
        },
    }


# --- Streaming: OpenAI Chat SSE → Anthropic Messages SSE ------------------


class AnthropicStreamTranslator:
    """Stateful chunk-by-chunk translator from OpenAI Chat SSE to Anthropic Messages SSE.

    Buffers incoming bytes until newline-terminated SSE events arrive, parses each
    `data:` line as JSON (per OpenAI), and emits the corresponding Anthropic event(s).
    On `data: [DONE]` the translator emits the Anthropic terminator events.

    Wire usage:
        translator = AnthropicStreamTranslator(model_label)
        async for upstream_chunk in nautrouter.chat_completions_stream(payload):
            for out_chunk in translator.feed(upstream_chunk):
                yield out_chunk
        for out_chunk in translator.finish():
            yield out_chunk
    """

    def __init__(self, model: str):
        self.model = model
        self._buf = bytearray()
        self._message_started = False
        self._content_block_started = False
        self._content_block_stopped = False
        self._message_stopped = False
        self._input_tokens: int | None = None
        self._output_tokens: int | None = None
        self._finish_reason: str | None = None
        self._message_id = f"msg_{uuid.uuid4().hex[:24]}"

    # ---- helpers ----

    @staticmethod
    def _emit(event: str, data: dict) -> bytes:
        return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()

    def _start_message(self) -> bytes:
        self._message_started = True
        return self._emit(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": self._message_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": self.model,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": self._input_tokens or 0,
                        "output_tokens": 0,
                    },
                },
            },
        )

    def _start_content_block(self) -> bytes:
        self._content_block_started = True
        return self._emit(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        )

    def _delta_text(self, text: str) -> bytes:
        return self._emit(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        )

    def _stop_content_block(self) -> bytes:
        self._content_block_stopped = True
        return self._emit("content_block_stop", {"type": "content_block_stop", "index": 0})

    def _message_delta_stop(self) -> bytes:
        return self._emit(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": _FINISH_TO_STOP.get(
                        self._finish_reason or "stop", self._finish_reason
                    )
                    or "end_turn",
                    "stop_sequence": None,
                },
                "usage": {"output_tokens": self._output_tokens or 0},
            },
        )

    def _stop_message(self) -> bytes:
        self._message_stopped = True
        return self._emit("message_stop", {"type": "message_stop"})

    # ---- public ----

    def feed(self, chunk: bytes):
        """Yield translated bytes for any complete events present in the buffer."""
        self._buf.extend(chunk)
        events_out: list[bytes] = []
        # Split on \n\n boundary; keep any trailing partial in buf.
        while b"\n\n" in self._buf:
            blob, _, rest = bytes(self._buf).partition(b"\n\n")
            self._buf = bytearray(rest)
            for line in blob.split(b"\n"):
                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str.startswith("data:"):
                    continue
                payload_str = line_str[5:].strip()
                if payload_str == "[DONE]":
                    # Stream is done at the OpenAI level; emit terminators.
                    events_out.extend(self._terminate())
                    continue
                try:
                    obj = json.loads(payload_str)
                except json.JSONDecodeError:
                    continue
                events_out.extend(self._handle_chunk(obj))
        return events_out

    def finish(self):
        """Flush any pending events when upstream closes without sending [DONE]."""
        out: list[bytes] = []
        out.extend(self._terminate())
        return out

    def _terminate(self):
        out: list[bytes] = []
        if not self._message_started:
            out.append(self._start_message())
        if self._content_block_started and not self._content_block_stopped:
            out.append(self._stop_content_block())
        if not self._message_stopped:
            out.append(self._message_delta_stop())
            out.append(self._stop_message())
        return out

    def _handle_chunk(self, obj: dict):
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

        # First chunk usually carries role=assistant — emit message_start once.
        if not self._message_started:
            out.append(self._start_message())

        text = delta.get("content")
        if isinstance(text, str) and text:
            if not self._content_block_started:
                out.append(self._start_content_block())
            out.append(self._delta_text(text))

        if ch.get("finish_reason"):
            self._finish_reason = ch["finish_reason"]

        return out
