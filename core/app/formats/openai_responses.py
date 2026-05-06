"""Week 1b — OpenAI Responses ↔ OpenAI Chat translation.

Responses is the newer shape: `input` + `instructions` instead of Chat's
`messages`, and the response is `output: [{type: "message", content: [{type: "output_text", text: ...}]}]`
plus a flattened `output_text` convenience field.

Reference: https://platform.openai.com/docs/api-reference/responses

Streaming translation lands later — for now /v1/responses with stream=true
returns 501 (Coming-In: week-1b-stream).
"""

from __future__ import annotations

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
