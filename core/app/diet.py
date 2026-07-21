"""Prompt-diet strategies — evidence-based context pruning.

Pure functions over OpenAI-shape message lists. Used two ways:
  1. Shadow trials (app/shadow.py): prune a mirrored copy and blind-compare
     the answers — original vs pruned, same model.
  2. In-flight (routes/v1.py): once a strategy is PROVEN non-inferior for an
     agent, apply it to that agent's eligible live calls before forwarding.

A strategy returns None when pruning would be a no-op — callers skip those
calls (no information in a trial that changes nothing).
"""

from __future__ import annotations

import json

# strategy name → messages to keep from the tail (system messages always kept)
STRATEGIES: dict[str, int] = {
    "history-6": 6,
    "history-2": 2,
}


def prune_messages(messages: list[dict], strategy: str) -> list[dict] | None:
    """Keep all system messages + the last N non-system messages.

    Returns the pruned list, or None when nothing would be removed (or the
    strategy is unknown). Never mutates the input.
    """
    keep = STRATEGIES.get(strategy)
    if keep is None or not isinstance(messages, list):
        return None
    system = [m for m in messages if isinstance(m, dict) and m.get("role") == "system"]
    rest = [m for m in messages if isinstance(m, dict) and m.get("role") != "system"]
    if len(rest) <= keep:
        return None
    return system + rest[-keep:]


def payload_bytes(messages: list[dict]) -> int:
    return len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def apply_diet_to_payload(payload: dict, strategy: str) -> dict | None:
    """In-flight variant: prune payload["messages"] in place-style (returns a
    stats dict, or None when not applied). Only tool-free payloads are eligible
    — the evidence from shadow trials covers exactly that scope.
    """
    if payload.get("tools"):
        return None
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return None
    pruned = prune_messages(messages, strategy)
    if pruned is None:
        return None
    before = payload_bytes(messages)
    after = payload_bytes(pruned)
    payload["messages"] = pruned
    return {
        "strategy": strategy,
        "dropped_messages": len(messages) - len(pruned),
        "original_bytes": before,
        "pruned_bytes": after,
        "saved_pct": round(1 - after / before, 3) if before else 0.0,
    }
