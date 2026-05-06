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
    finish_reason: str | None = None
    content_parts: list[str] = []

    for _event_type, data in _iter_sse_events(buf):
        if data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue

        # ---- OpenAI Chat shape ----
        # {"choices":[{"delta":{"content":"..."},"finish_reason":...}],"usage":{...}}
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            ch = choices[0]
            delta = ch.get("delta") or {}
            piece = delta.get("content")
            if isinstance(piece, str) and piece:
                content_parts.append(piece)
            if ch.get("finish_reason"):
                finish_reason = ch["finish_reason"]

        usage = payload.get("usage")
        if isinstance(usage, dict):
            prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
            completion_tokens = usage.get("completion_tokens", completion_tokens)
            details = usage.get("output_tokens_details") or {}
            if isinstance(details, dict):
                reasoning_tokens = details.get("reasoning_tokens", reasoning_tokens)

        # ---- Anthropic Messages shape ----
        # message_start carries usage.input_tokens; content_block_delta(text_delta) carries text;
        # message_delta carries stop_reason and usage.output_tokens.
        ptype = payload.get("type")
        if ptype == "message_start":
            msg = payload.get("message") or {}
            u = msg.get("usage") or {}
            prompt_tokens = u.get("input_tokens", prompt_tokens)
        elif ptype == "content_block_delta":
            d = payload.get("delta") or {}
            if d.get("type") == "text_delta" and isinstance(d.get("text"), str):
                content_parts.append(d["text"])
        elif ptype == "message_delta":
            d = payload.get("delta") or {}
            if d.get("stop_reason"):
                finish_reason = d["stop_reason"]
            u = payload.get("usage") or {}
            if u.get("output_tokens") is not None:
                completion_tokens = u["output_tokens"]

    assembled = "".join(content_parts)
    was_empty = bool((completion_tokens or 0) > 0 and not assembled)

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "finish_reason": finish_reason,
        "assembled_content": assembled,
        "was_empty": was_empty,
    }
