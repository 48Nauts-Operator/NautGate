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

from app.harness import promote_text_tool_calls

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


def _tool_result_text(content: Any) -> str:
    """Flatten an Anthropic tool_result's content (str or list of blocks) to a
    plain string for an OpenAI {role:tool} message."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(parts)
    return "" if content is None else str(content)


def _translate_message(role: str, content: list) -> list[dict]:
    """Translate one Anthropic message (list-of-blocks form) into one or more
    OpenAI messages, preserving tool history.

      assistant text/tool_use → {role:assistant, content, tool_calls}
      user tool_result        → {role:tool, tool_call_id, content} (one each)
      user/assistant text|image → the normal normalized message
    """
    if not isinstance(content, list):
        return [{"role": role, "content": str(content)}]

    tool_uses = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
    tool_results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
    other = [b for b in content if isinstance(b, dict) and b.get("type") in ("text", "image")]

    out: list[dict] = []

    # Assistant turn that issued tool calls.
    if role == "assistant" and tool_uses:
        text = "\n".join(b.get("text", "") for b in other if b.get("type") == "text")
        msg: dict[str, Any] = {"role": "assistant"}
        msg["content"] = text or None  # OpenAI allows null content with tool_calls
        msg["tool_calls"] = [
            {
                "id": tu.get("id") or f"call_{i}",
                "type": "function",
                "function": {
                    "name": tu.get("name", ""),
                    "arguments": json.dumps(tu.get("input") or {}),
                },
            }
            for i, tu in enumerate(tool_uses)
        ]
        return [msg]

    # User turn carrying tool results → one OpenAI tool message per result.
    if role == "user" and tool_results:
        for tr in tool_results:
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": tr.get("tool_use_id") or "",
                    "content": _tool_result_text(tr.get("content")),
                }
            )
        # Any accompanying text/image blocks become a normal user message.
        if other:
            blocks = _normalize_content_blocks(other)
            if blocks:
                out.append(
                    {
                        "role": "user",
                        "content": blocks[0]["text"]
                        if len(blocks) == 1 and blocks[0].get("type") == "text"
                        else blocks,
                    }
                )
        return out

    # Plain text/image message (no tool blocks).
    blocks = _normalize_content_blocks(content)
    if len(blocks) == 1 and blocks[0].get("type") == "text":
        return [{"role": role, "content": blocks[0]["text"]}]
    return [{"role": role, "content": blocks}]


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
            # NAUTGATE-2: preserve tool_use/tool_result history so agentic
            # loops don't derail on routed models. One Anthropic message can
            # expand into several OpenAI messages (assistant.tool_calls, and a
            # separate {role:tool} per tool_result).
            msgs.extend(_translate_message(role, content))

    out["messages"] = msgs

    # Pass-through scalars.
    for k in ("temperature", "top_p", "max_tokens", "stream", "metadata", "tool_choice"):
        if k in payload:
            out[k] = payload[k]

    # Tools need a *shape* translation, not pass-through. Anthropic format:
    #   {"name", "description", "input_schema"}
    # OpenAI format:
    #   {"type": "function", "function": {"name", "description", "parameters"}}
    # Without this, upstream models receive malformed tool defs and silently
    # produce no tool_calls — which presents to Claude Code as an empty answer.
    raw_tools = payload.get("tools")
    if isinstance(raw_tools, list) and raw_tools:
        translated_tools: list[dict] = []
        for t in raw_tools:
            if not isinstance(t, dict):
                continue
            # Already OpenAI-shaped (passthrough/Codex clients): keep as-is.
            if "function" in t and isinstance(t["function"], dict):
                translated_tools.append(t)
                continue
            # Anthropic-shaped: rebuild as OpenAI function tool.
            if "name" in t:
                translated_tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": t.get("name"),
                            "description": t.get("description", ""),
                            # Anthropic uses input_schema; OpenAI uses parameters.
                            "parameters": t.get("input_schema")
                            or t.get("parameters")
                            or {"type": "object"},
                        },
                    }
                )
        if translated_tools:
            out["tools"] = translated_tools

    if "stop_sequences" in payload:
        out["stop"] = payload["stop_sequences"]

    return out


# --- Response: OpenAI Chat → Anthropic Messages ---------------------------


def response_to_anthropic(
    openai_resp: dict, model: str | None = None, normalize: bool = False
) -> dict:
    """Translate a non-streaming OpenAI Chat response into an Anthropic Messages response.

    ``normalize`` (opt-in harness module) promotes a local model's pseudo tool
    call from text into structured tool_calls before translation.
    """
    choices = openai_resp.get("choices") or []
    msg = (choices[0] if choices else {}).get("message") or {}
    finish = (choices[0] if choices else {}).get("finish_reason")
    if normalize:
        msg, promoted = promote_text_tool_calls(msg)
        if promoted:
            finish = "tool_calls"
    content = msg.get("content") or ""
    usage = openai_resp.get("usage") or {}

    # NAUTGATE-2 (issue #2): map OpenAI tool_calls → Anthropic tool_use blocks
    # so non-streamed tool responses aren't returned as empty content.
    blocks: list[dict] = []
    if content:
        blocks.append({"type": "text", "text": content})
    for tc in msg.get("tool_calls") or []:
        fn = (tc or {}).get("function") or {}
        args = fn.get("arguments")
        try:
            parsed = json.loads(args) if isinstance(args, str) else (args or {})
        except (ValueError, TypeError):
            parsed = {}
        blocks.append(
            {
                "type": "tool_use",
                "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:20]}",
                "name": fn.get("name", ""),
                "input": parsed,
            }
        )

    return {
        "id": openai_resp.get("id") or f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "content": blocks,
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

    def __init__(self, model: str, normalize: bool = False):
        self.model = model
        # Opt-in harness module: buffer text and promote a pseudo tool call at
        # the end (see app.harness). Off by default → streams text as it arrives.
        self.normalize = normalize
        self._accum_text = ""
        self._buf = bytearray()
        self._message_started = False
        # Text content block (index 0 if any text was streamed).
        self._text_block_started = False
        self._text_block_stopped = False
        # Tool-use content blocks. OpenAI streams tool_calls with their own
        # `index` (per call), but Anthropic content_block indices are global
        # within the message — we map openai_tool_index → anthropic_block_index.
        self._tool_blocks: dict[
            int, dict
        ] = {}  # openai_idx → {block_index, started, stopped, name, id}
        self._next_block_index = 0
        self._message_stopped = False
        self._input_tokens: int | None = None
        self._output_tokens: int | None = None
        self._finish_reason: str | None = None
        self._message_id = f"msg_{uuid.uuid4().hex[:24]}"

    # Backwards-compat shims so existing tests that touched these names still work.
    @property
    def _content_block_started(self) -> bool:
        return self._text_block_started

    @_content_block_started.setter
    def _content_block_started(self, v: bool) -> None:
        self._text_block_started = v

    @property
    def _content_block_stopped(self) -> bool:
        return self._text_block_stopped

    @_content_block_stopped.setter
    def _content_block_stopped(self, v: bool) -> None:
        self._text_block_stopped = v

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

    def _allocate_block_index(self) -> int:
        idx = self._next_block_index
        self._next_block_index += 1
        return idx

    def _start_content_block(self) -> bytes:
        """Open the text content block at index 0 (always reserved for text)."""
        self._text_block_started = True
        # Make sure index 0 stays for text — if no tools have been opened yet,
        # _next_block_index is 0; bump it to 1.
        if self._next_block_index == 0:
            self._next_block_index = 1
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
        self._text_block_stopped = True
        return self._emit("content_block_stop", {"type": "content_block_stop", "index": 0})

    # ---- tool_use translation ----

    def _start_tool_block(self, openai_idx: int, tool_id: str, name: str) -> bytes:
        block_index = self._allocate_block_index()
        self._tool_blocks[openai_idx] = {
            "block_index": block_index,
            "started": True,
            "stopped": False,
            "name": name,
            "id": tool_id,
        }
        return self._emit(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": block_index,
                "content_block": {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": name,
                    "input": {},
                },
            },
        )

    def _delta_tool_input(self, openai_idx: int, partial_json: str) -> bytes:
        info = self._tool_blocks.get(openai_idx)
        if info is None:
            return b""
        return self._emit(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": info["block_index"],
                "delta": {"type": "input_json_delta", "partial_json": partial_json},
            },
        )

    def _stop_tool_block(self, openai_idx: int) -> bytes:
        info = self._tool_blocks.get(openai_idx)
        if info is None or info["stopped"]:
            return b""
        info["stopped"] = True
        return self._emit(
            "content_block_stop",
            {"type": "content_block_stop", "index": info["block_index"]},
        )

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

    def _flush_normalized(self) -> list[bytes]:
        """Opt-in harness path: decide tool_use-vs-text for the buffered content
        once the whole message is known, and emit the right Anthropic blocks."""
        out: list[bytes] = []
        msg, promoted = promote_text_tool_calls({"content": self._accum_text})
        if not promoted:
            if not self._text_block_started:
                out.append(self._start_content_block())
            out.append(self._delta_text(self._accum_text))
            return out
        self._finish_reason = "tool_calls"
        leftover = msg.get("content")
        if isinstance(leftover, str) and leftover.strip():
            if not self._text_block_started:
                out.append(self._start_content_block())
            out.append(self._delta_text(leftover))
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            out.append(
                self._start_tool_block(
                    i, tool_id=tc.get("id") or f"call_{uuid.uuid4().hex[:24]}", name=fn.get("name", "")
                )
            )
            args = fn.get("arguments")
            args_str = args if isinstance(args, str) else json.dumps(args or {})
            if args_str:
                out.append(self._delta_tool_input(i, args_str))
        return out

    def _terminate(self):
        out: list[bytes] = []
        if not self._message_started:
            out.append(self._start_message())
        # Opt-in harness module: flush buffered text (promoted to tool_use if it
        # held a pseudo tool call). Only when nothing structured was streamed.
        if self.normalize and self._accum_text and not self._tool_blocks:
            out.extend(self._flush_normalized())
            self._accum_text = ""
        if self._text_block_started and not self._text_block_stopped:
            out.append(self._stop_content_block())
        # Close any still-open tool_use blocks so the message ends cleanly.
        for openai_idx, info in self._tool_blocks.items():
            if info["started"] and not info["stopped"]:
                out.append(self._stop_tool_block(openai_idx))
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

        # Text content delta — opens the text block on first non-empty text.
        text = delta.get("content")
        if isinstance(text, str) and text:
            if self.normalize:
                # Hold text back; _terminate decides tool_use-vs-text once the
                # full message is known.
                self._accum_text += text
            else:
                if not self._text_block_started:
                    out.append(self._start_content_block())
                out.append(self._delta_text(text))

        # Tool-call deltas. OpenAI streams these as a list, each entry tagged
        # with its own `index`. The first chunk for a given index carries
        # `id` and `function.name`; subsequent chunks carry only
        # `function.arguments` partials. Anthropic's wire format is:
        #   content_block_start (type=tool_use, id, name, input={})
        #   content_block_delta (type=input_json_delta, partial_json="...")
        #   content_block_stop
        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                openai_idx = tc.get("index", 0)
                fn = tc.get("function") or {}
                call_id = tc.get("id")
                name = fn.get("name")
                args_partial = fn.get("arguments")
                # First chunk for this call (has id and/or name) → open block.
                if openai_idx not in self._tool_blocks and (call_id or name):
                    out.append(
                        self._start_tool_block(
                            openai_idx,
                            tool_id=call_id or f"call_{uuid.uuid4().hex[:24]}",
                            name=name or "",
                        )
                    )
                # Argument deltas come as already-stringified JSON fragments.
                if isinstance(args_partial, str) and args_partial:
                    if openai_idx not in self._tool_blocks:
                        # Defensive: open a block even without id/name so we
                        # never drop a partial.
                        out.append(
                            self._start_tool_block(
                                openai_idx,
                                tool_id=f"call_{uuid.uuid4().hex[:24]}",
                                name="",
                            )
                        )
                    out.append(self._delta_tool_input(openai_idx, args_partial))

        if ch.get("finish_reason"):
            self._finish_reason = ch["finish_reason"]
            # If the model finished with tool_calls, close all tool blocks now
            # (rather than waiting for the [DONE] / stream-end terminator).
            if ch["finish_reason"] in ("tool_calls", "stop"):
                if self._text_block_started and not self._text_block_stopped:
                    out.append(self._stop_content_block())
                for openai_idx, info in list(self._tool_blocks.items()):
                    if info["started"] and not info["stopped"]:
                        out.append(self._stop_tool_block(openai_idx))

        return out
