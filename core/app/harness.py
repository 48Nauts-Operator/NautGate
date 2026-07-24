"""Optional harness-compat normalization for local models.

OFF BY DEFAULT (settings.nautgate_harness_normalize). This is an extra module —
the default Messages bridge is untouched when it's disabled.

Local models (Qwen, other Hermes-format tool callers via LM Studio/Ollama) often
emit a tool call as TEXT — ``<tool_call>{...}</tool_call>`` in the content or a
reasoning block — while returning an empty structured ``tool_calls`` field. To an
agentic harness (Claude Code driving the model through NautGate) that reads as an
empty answer, and the tool loop stalls. When enabled, this promotes that pseudo
tool call into a proper OpenAI ``tool_calls`` array before the OpenAI→Anthropic
translation, so the harness sees a real tool call. NautGate is the right place to
fix it once for every harness on that route (see NAUTGATE-24 + the Buzz doc).
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

# Hermes/Qwen wrap: <tool_call>{ "name": ..., "arguments": {...} }</tool_call>
_TOOLCALL_TAG = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL | re.IGNORECASE)
# Fenced JSON fallback: ```json\n{...}\n```
_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _to_tool_call(obj: Any) -> dict | None:
    """Shape a parsed pseudo-call dict into an OpenAI tool_call, or None."""
    if not isinstance(obj, dict):
        return None
    fn = obj.get("function") if isinstance(obj.get("function"), dict) else {}
    name = obj.get("name") or obj.get("tool") or obj.get("tool_name") or fn.get("name")
    if not name:
        return None
    args = obj.get("arguments")
    if args is None:
        args = obj.get("parameters") or obj.get("input") or fn.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, TypeError):
            pass  # keep the raw string if it isn't JSON
    return {
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {
            "name": str(name),
            "arguments": args if isinstance(args, str) else json.dumps(args or {}),
        },
    }


def _extract(text: str) -> list[dict]:
    """Pull pseudo tool calls out of model text, most-specific pattern first."""
    calls: list[dict] = []
    for m in _TOOLCALL_TAG.finditer(text):
        try:
            tc = _to_tool_call(json.loads(m.group(1)))
        except (ValueError, TypeError):
            tc = None
        if tc:
            calls.append(tc)
    if calls:
        return calls
    for m in _FENCED_JSON.finditer(text):
        try:
            tc = _to_tool_call(json.loads(m.group(1)))
        except (ValueError, TypeError):
            tc = None
        if tc:
            calls.append(tc)
    if calls:
        return calls
    # Whole-content bare JSON object, e.g. {"name": "...", "arguments": {...}}
    s = text.strip()
    if s.startswith("{") and s.endswith("}"):
        try:
            tc = _to_tool_call(json.loads(s))
        except (ValueError, TypeError):
            tc = None
        if tc:
            calls.append(tc)
    return calls


def promote_text_tool_calls(msg: dict) -> tuple[dict, bool]:
    """If ``msg`` has no structured tool_calls but its text/reasoning contains a
    pseudo tool call, promote it. Returns ``(msg, promoted)``; ``msg`` is a copy
    when promoted, the original otherwise.
    """
    if not isinstance(msg, dict) or msg.get("tool_calls"):
        return msg, False

    content = msg.get("content")
    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
    haystack = ""
    if isinstance(content, str):
        haystack += content
    if isinstance(reasoning, str) and reasoning:
        haystack += "\n" + reasoning
    if not haystack.strip():
        return msg, False

    calls = _extract(haystack)
    if not calls:
        return msg, False

    out = dict(msg)
    out["tool_calls"] = calls
    # Strip the <tool_call> spans from visible content so the raw call isn't
    # also echoed as assistant text; drop content entirely if nothing's left.
    if isinstance(content, str):
        stripped = _TOOLCALL_TAG.sub("", content).strip()
        out["content"] = stripped or None
    return out, True
