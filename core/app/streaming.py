"""Streaming SSE tee accumulator + post-stream parser.

Per Tech Paper §11. The gateway uses an in-process StreamCapture that tees the
upstream byte stream to (a) the client, (b) an accumulator. Truncation at the
configured cap (default 8 MB) occurs at SSE event boundaries (`\\n\\n`) so
downstream tools parsing the captured body always see well-formed SSE.

The tee continues passing full bytes to the client even after truncation —
truncation only affects what we capture, not what the user receives.
"""

import json
from dataclasses import dataclass, field
from typing import Any

ACCUMULATOR_CAP_BYTES_DEFAULT = 8 * 1024 * 1024  # 8 MB — Tech Paper §11.3


@dataclass
class StreamCapture:
    cap_bytes: int = ACCUMULATOR_CAP_BYTES_DEFAULT
    accumulator: bytearray = field(default_factory=bytearray)
    bytes_seen: int = 0
    was_truncated: bool = False
    truncated_at_byte: int | None = None

    def append(self, chunk: bytes) -> None:
        """Tee one upstream chunk into the accumulator. Always safe to call after truncation."""
        self.bytes_seen += len(chunk)
        if self.was_truncated:
            return

        if len(self.accumulator) + len(chunk) <= self.cap_bytes:
            self.accumulator.extend(chunk)
            return

        # We're about to overflow. Take what fits, then trim back to the last
        # complete SSE event boundary so the captured body is parseable.
        remaining = self.cap_bytes - len(self.accumulator)
        partial = chunk[:remaining]
        self.accumulator.extend(partial)
        last_boundary = self.accumulator.rfind(b"\n\n")
        if last_boundary >= 0:
            # Keep up to and including the boundary.
            del self.accumulator[last_boundary + 2 :]
        # else: no SSE boundary anywhere in the buffer (unlikely) — keep what we have.
        self.was_truncated = True
        self.truncated_at_byte = len(self.accumulator)


def _iter_sse_events(buf: bytes):
    """Yield (event_type, data_lines) tuples from a buffer of SSE bytes.

    Handles both event-stream styles:
      - OpenAI Chat: just `data: {...}\\n\\n` blocks (and a `data: [DONE]\\n\\n` terminator)
      - Anthropic Messages: `event: <name>\\ndata: {...}\\n\\n` blocks

    Yielded `event_type` is None for OpenAI (no event line); `data_lines` is the raw
    payload string after the leading `data: `. `[DONE]` is yielded as ("done", "[DONE]").
    """
    text = buf.decode("utf-8", errors="replace")
    for raw_block in text.split("\n\n"):
        block = raw_block.strip("\n")
        if not block:
            continue
        event_type: str | None = None
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event_type = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].lstrip())
        if not data_lines:
            continue
        data = "\n".join(data_lines)
        if data == "[DONE]":
            yield ("done", "[DONE]")
            continue
        yield (event_type, data)


def parse_sse_for_outcome(buf: bytes) -> dict[str, Any]:
    """Walk accumulated SSE bytes and extract metrics for `route_outcomes`.

    Returns a dict with (all keys optional):
        prompt_tokens:     int | None
        completion_tokens: int | None
        reasoning_tokens:  int | None
        finish_reason:     str | None
        assembled_content: str
        was_empty:         bool

    `was_empty` is True when usage indicates completion tokens were generated but the
    assembled assistant content is empty — the Tongyi failure mode (Tech Paper §8).
    """
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    finish_reason: str | None = None
    actual_model: str | None = None
    actual_provider: str | None = None
    provider_error: dict | None = None
    content_parts: list[str] = []
    # tool_calls accumulator — keyed by tool_call index for OpenAI Chat streaming;
    # for Anthropic, keyed by content_block index. Both stream incrementally.
    tool_calls_acc: dict[int, dict] = {}
    # Anthropic content-block index → tool name/id captured at block start.
    anthropic_blocks: dict[int, dict] = {}

    for _event_type, data in _iter_sse_events(buf):
        if data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue

        if isinstance(payload.get("error"), dict):
            provider_error = payload["error"]

        # OpenRouter / OpenAI: each chunk carries the actual model picked.
        if isinstance(payload.get("model"), str) and not actual_model:
            actual_model = payload["model"]
        if isinstance(payload.get("provider"), str) and not actual_provider:
            actual_provider = payload["provider"]

        # ---- OpenAI Chat shape ----
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            ch = choices[0]
            delta = ch.get("delta") or {}
            piece = delta.get("content")
            if isinstance(piece, str) and piece:
                content_parts.append(piece)
            if ch.get("finish_reason"):
                finish_reason = ch["finish_reason"]
            for tc in delta.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                idx = tc.get("index", 0)
                slot = tool_calls_acc.setdefault(idx, {"id": None, "name": None, "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if isinstance(fn.get("arguments"), str):
                    slot["arguments"] += fn["arguments"]

        usage = payload.get("usage")
        if isinstance(usage, dict):
            pt = usage.get("prompt_tokens")
            # OpenAI prompt_tokens is the TOTAL input. Cache reads arrive either
            # as prompt_tokens_details.cached_tokens (OpenAI / DeepSeek via OR) or
            # as Anthropic passthrough fields (cache_*_input_tokens) when OR fronts
            # an Anthropic model. Probe both; store fresh = total − read − write.
            details_in = usage.get("prompt_tokens_details")
            if isinstance(details_in, dict) and details_in.get("cached_tokens") is not None:
                cache_read_tokens = details_in["cached_tokens"]
            elif usage.get("cache_read_input_tokens") is not None:
                cache_read_tokens = usage["cache_read_input_tokens"]
            if usage.get("cache_creation_input_tokens") is not None:
                cache_write_tokens = usage["cache_creation_input_tokens"]
            if pt is not None:
                pt = pt - (cache_read_tokens or 0) - (cache_write_tokens or 0)
                if pt < 0:  # provider already excluded cache from prompt_tokens
                    pt = usage.get("prompt_tokens")
                prompt_tokens = pt
            completion_tokens = usage.get("completion_tokens", completion_tokens)
            details = (
                usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
            )
            if isinstance(details, dict):
                reasoning_tokens = details.get("reasoning_tokens", reasoning_tokens)

        # ---- Anthropic Messages shape ----
        ptype = payload.get("type")
        if ptype == "message_start":
            msg = payload.get("message") or {}
            u = msg.get("usage") or {}
            # Anthropic input_tokens is already FRESH; cache reads/writes are
            # separate additive fields carried on message_start.
            prompt_tokens = u.get("input_tokens", prompt_tokens)
            if u.get("cache_read_input_tokens") is not None:
                cache_read_tokens = u["cache_read_input_tokens"]
            if u.get("cache_creation_input_tokens") is not None:
                cache_write_tokens = u["cache_creation_input_tokens"]
        elif ptype == "content_block_start":
            cb = payload.get("content_block") or {}
            if cb.get("type") == "tool_use":
                idx = payload.get("index", 0)
                anthropic_blocks[idx] = {
                    "id": cb.get("id"),
                    "name": cb.get("name"),
                    "arguments": "",
                }
        elif ptype == "content_block_delta":
            d = payload.get("delta") or {}
            if d.get("type") == "text_delta" and isinstance(d.get("text"), str):
                content_parts.append(d["text"])
            elif d.get("type") == "input_json_delta":
                idx = payload.get("index", 0)
                slot = anthropic_blocks.get(idx)
                if slot is not None and isinstance(d.get("partial_json"), str):
                    slot["arguments"] += d["partial_json"]
        elif ptype == "message_delta":
            d = payload.get("delta") or {}
            if d.get("stop_reason"):
                finish_reason = d["stop_reason"]
            u = payload.get("usage") or {}
            if u.get("output_tokens") is not None:
                completion_tokens = u["output_tokens"]

    assembled = "".join(content_parts)

    # Combine OpenAI-streamed and Anthropic-streamed tool calls — only one of
    # the two will actually be populated per request.
    tool_calls: list[dict] = []
    for idx in sorted(tool_calls_acc.keys()):
        s = tool_calls_acc[idx]
        if s.get("name"):
            tool_calls.append(
                {"id": s.get("id"), "name": s["name"], "arguments": s.get("arguments") or ""}
            )
    for idx in sorted(anthropic_blocks.keys()):
        s = anthropic_blocks[idx]
        if s.get("name"):
            tool_calls.append(
                {"id": s.get("id"), "name": s["name"], "arguments": s.get("arguments") or ""}
            )

    # was_empty: the model billed for completion tokens but emitted NEITHER
    # text content NOR tool calls. A tool_use-only response is a real,
    # successful response — flagging it as empty caused the brain layer to
    # silently demote models that were actually doing their job correctly.
    was_empty = bool((completion_tokens or 0) > 0 and not assembled and not tool_calls)

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "finish_reason": finish_reason,
        "assembled_content": assembled,
        "was_empty": was_empty,
        "tool_calls": tool_calls,
        "actual_model": actual_model,
        "actual_provider": actual_provider,
        "provider_error": provider_error,
    }
