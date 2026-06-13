"""Provider-aware usage normalization + cache-prefix hashing.

Every provider reports token usage in a different shape, and they disagree on
whether cache-hit tokens are *included* in the headline prompt count or reported
*separately*:

    Anthropic   input_tokens is already FRESH (non-cached); cache reads/writes
                are separate fields (cache_read_input_tokens,
                cache_creation_input_tokens).
    OpenAI      prompt_tokens is the TOTAL; prompt_tokens_details.cached_tokens
                is a SUBSET of it → fresh = prompt_tokens - cached_tokens.
    DeepSeek    prompt_cache_hit_tokens + prompt_cache_miss_tokens = total →
                fresh = miss, cache_read = hit.
    Gemini      usage_metadata.cached_content_token_count is a SUBSET of
                prompt_token_count → fresh = prompt - cached.

`normalize_usage` collapses all of these to a single shape where ``prompt_tokens``
always means FRESH (non-cached) input, so the three token columns sum cleanly:

    total_input = prompt_tokens + cache_read_tokens + cache_write_tokens

cache_write_tokens is Anthropic's premium cache-creation tier; it's ~always 0 or
absent for other providers (they don't charge a write premium).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

__all__ = ["NormalizedUsage", "normalize_usage", "cache_prefix_hash"]


@dataclass
class NormalizedUsage:
    prompt_tokens: int | None = None  # fresh (non-cached) input
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None  # Anthropic premium tier; ~0 elsewhere


def _as_int(v: object) -> int | None:
    if isinstance(v, bool):  # bool is an int subclass — exclude it
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    return None


def normalize_usage(usage: dict | None, *, provider_hint: str | None = None) -> NormalizedUsage:
    """Collapse any provider's usage object to fresh-input + cache split.

    Tolerant of partial/unknown shapes: probes every known field name and only
    fills what's present. ``provider_hint`` ("anthropic"/"openai"/"deepseek"/
    "gemini"/"openrouter"/None) decides the include-vs-separate handling; when
    None we infer from which fields are present.
    """
    if not isinstance(usage, dict):
        return NormalizedUsage()

    hint = (provider_hint or "").lower()

    # --- Native Anthropic Messages shape ----------------------------------
    # input_tokens is fresh; cache reads/writes are separate, additive fields.
    # NOTE: the discriminator is ``input_tokens`` specifically. When Anthropic
    # cache fields arrive WITHOUT input_tokens (e.g. OpenRouter passing them
    # through next to OpenAI-shaped prompt_tokens), it's NOT native Anthropic —
    # that's handled in the default branch below.
    if "input_tokens" in usage and "deepseek" not in hint:
        cache_read = _as_int(usage.get("cache_read_input_tokens"))
        cache_write = _as_int(usage.get("cache_creation_input_tokens"))
        return NormalizedUsage(
            prompt_tokens=_as_int(usage.get("input_tokens")),
            completion_tokens=_as_int(usage.get("output_tokens")),
            reasoning_tokens=None,  # Anthropic doesn't break out reasoning here
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )

    # --- DeepSeek shape ----------------------------------------------------
    # hit + miss = total; miss is the fresh input.
    if "prompt_cache_hit_tokens" in usage or "prompt_cache_miss_tokens" in usage:
        hit = _as_int(usage.get("prompt_cache_hit_tokens"))
        miss = _as_int(usage.get("prompt_cache_miss_tokens"))
        # Prefer miss as fresh; fall back to prompt_tokens - hit if miss absent.
        fresh = miss
        if fresh is None:
            total = _as_int(usage.get("prompt_tokens"))
            fresh = (total - (hit or 0)) if total is not None else None
        return NormalizedUsage(
            prompt_tokens=fresh,
            completion_tokens=_as_int(usage.get("completion_tokens")),
            reasoning_tokens=_reasoning_from_openai(usage),
            cache_read_tokens=hit,
            cache_write_tokens=None,
        )

    # --- Gemini shape ------------------------------------------------------
    if "usage_metadata" in usage or "cached_content_token_count" in usage:
        meta = usage.get("usage_metadata") if isinstance(usage.get("usage_metadata"), dict) else usage
        total = _as_int(meta.get("prompt_token_count"))
        cached = _as_int(meta.get("cached_content_token_count"))
        fresh = total
        if total is not None and cached:
            fresh = total - cached
        return NormalizedUsage(
            prompt_tokens=fresh,
            completion_tokens=_as_int(meta.get("candidates_token_count")),
            reasoning_tokens=_as_int(meta.get("thoughts_token_count")),
            cache_read_tokens=cached,
            cache_write_tokens=None,
        )

    # --- OpenAI / OpenRouter shape (default) -------------------------------
    # prompt_tokens is the TOTAL input. Cache reads arrive either as OpenAI's
    # prompt_tokens_details.cached_tokens (OpenAI / DeepSeek normalized by OR)
    # OR as Anthropic passthrough fields (cache_read_input_tokens /
    # cache_creation_input_tokens) when OpenRouter fronts an Anthropic model.
    # Probe both; fresh = total − read − write so the columns sum to the bill.
    total = _as_int(usage.get("prompt_tokens"))
    details_in = usage.get("prompt_tokens_details")
    cached = _as_int(details_in.get("cached_tokens")) if isinstance(details_in, dict) else None
    if cached is None:
        cached = _as_int(usage.get("cache_read_input_tokens"))
    cache_write = _as_int(usage.get("cache_creation_input_tokens"))
    fresh = total
    if total is not None:
        fresh = total - (cached or 0) - (cache_write or 0)
        if fresh < 0:  # provider already excluded cache from prompt_tokens
            fresh = total
    return NormalizedUsage(
        prompt_tokens=fresh,
        completion_tokens=_as_int(usage.get("completion_tokens")),
        reasoning_tokens=_reasoning_from_openai(usage),
        cache_read_tokens=cached,
        cache_write_tokens=cache_write,
    )


def _reasoning_from_openai(usage: dict) -> int | None:
    details = usage.get("completion_tokens_details") or usage.get("output_tokens_details")
    if isinstance(details, dict):
        return _as_int(details.get("reasoning_tokens"))
    return None


def cache_prefix_hash(payload: dict | None) -> str | None:
    """Stable hash of the cacheable request prefix (system prompt + tool defs).

    The leak detector groups outcomes by this hash: two calls that *should* share
    a cached prefix but show different hashes mean something tiny (a timestamp, an
    ID, non-deterministic tool output) is mutating the prefix and silently busting
    the provider's cache.

    Hashes the parts a provider actually caches:
        Anthropic  payload["system"] + payload["tools"]
        OpenAI     leading system message(s) + payload["tools"]
    Returns None when there's nothing cacheable to key on.
    """
    if not isinstance(payload, dict):
        return None

    parts: list[object] = []

    system = payload.get("system")
    if system is not None:
        parts.append(system)

    # OpenAI-style: leading system/developer messages form the stable prefix.
    msgs = payload.get("messages")
    if isinstance(msgs, list):
        for m in msgs:
            if not isinstance(m, dict):
                break
            if m.get("role") in ("system", "developer"):
                parts.append(m.get("content"))
            else:
                break  # prefix ends at the first non-system turn

    tools = payload.get("tools")
    if tools is not None:
        parts.append(tools)

    if not parts:
        return None

    try:
        canon = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None
    return hashlib.sha1(canon.encode("utf-8")).hexdigest()  # noqa: S324 (non-crypto use)
