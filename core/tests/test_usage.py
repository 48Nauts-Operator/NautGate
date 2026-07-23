"""Tests for provider-aware usage normalization + cache-prefix hashing."""

from app.usage import cache_prefix_hash, normalize_usage


def test_anthropic_fresh_input_and_separate_cache():
    # Anthropic: input_tokens is already fresh; cache fields are additive.
    u = {
        "input_tokens": 245,
        "output_tokens": 512,
        "cache_creation_input_tokens": 18420,
        "cache_read_input_tokens": 0,
    }
    n = normalize_usage(u, provider_hint="anthropic")
    assert n.prompt_tokens == 245
    assert n.completion_tokens == 512
    assert n.cache_write_tokens == 18420
    assert n.cache_read_tokens == 0
    assert n.reasoning_tokens is None


def test_anthropic_cache_read_hit():
    u = {"input_tokens": 12, "output_tokens": 30, "cache_read_input_tokens": 18420}
    n = normalize_usage(u, provider_hint="anthropic")
    assert n.prompt_tokens == 12
    assert n.cache_read_tokens == 18420
    # total input reconstructs cleanly
    assert (n.prompt_tokens or 0) + (n.cache_read_tokens or 0) == 18432


def test_openai_subtracts_cached_from_total():
    # OpenAI prompt_tokens is the TOTAL; cached_tokens is a subset.
    u = {
        "prompt_tokens": 18665,
        "completion_tokens": 512,
        "prompt_tokens_details": {"cached_tokens": 18420},
    }
    n = normalize_usage(u, provider_hint="openai")
    assert n.prompt_tokens == 245  # 18665 - 18420
    assert n.cache_read_tokens == 18420
    assert n.cache_write_tokens is None
    assert n.completion_tokens == 512


def test_openai_reasoning_tokens():
    u = {
        "prompt_tokens": 100,
        "completion_tokens": 400,
        "completion_tokens_details": {"reasoning_tokens": 250},
    }
    n = normalize_usage(u, provider_hint="openai")
    assert n.reasoning_tokens == 250


def test_deepseek_hit_miss_split():
    u = {
        "prompt_tokens": 18665,
        "prompt_cache_hit_tokens": 18420,
        "prompt_cache_miss_tokens": 245,
        "completion_tokens": 512,
    }
    n = normalize_usage(u, provider_hint="deepseek")
    assert n.prompt_tokens == 245  # miss = fresh
    assert n.cache_read_tokens == 18420


def test_gemini_cached_content():
    u = {
        "usage_metadata": {
            "prompt_token_count": 1000,
            "cached_content_token_count": 800,
            "candidates_token_count": 120,
        }
    }
    n = normalize_usage(u, provider_hint="gemini")
    assert n.prompt_tokens == 200
    assert n.cache_read_tokens == 800
    assert n.completion_tokens == 120


def test_anthropic_via_openrouter_passthrough():
    # OpenRouter fronts an Anthropic model: OpenAI-shaped prompt_tokens (TOTAL)
    # plus Anthropic cache passthrough fields, NO input_tokens. Must not drop
    # fresh; fresh = total − read − write.
    u = {
        "prompt_tokens": 19800,
        "completion_tokens": 400,
        "cache_creation_input_tokens": 1800,
        "cache_read_input_tokens": 18000,
    }
    n = normalize_usage(u, provider_hint="openrouter")
    assert n.prompt_tokens == 0  # 19800 - 18000 - 1800
    assert n.cache_read_tokens == 18000
    assert n.cache_write_tokens == 1800


def test_openrouter_no_cache_model():
    # Open models (Llama/Kimi/Qwen) on OR don't bill caching → no cache fields.
    n = normalize_usage(
        {"prompt_tokens": 3000, "completion_tokens": 200}, provider_hint="openrouter"
    )
    assert n.prompt_tokens == 3000
    assert n.cache_read_tokens is None
    assert n.cache_write_tokens is None


def test_empty_and_garbage_usage():
    assert normalize_usage(None).prompt_tokens is None
    assert normalize_usage({}).prompt_tokens is None
    assert normalize_usage("nope").completion_tokens is None  # type: ignore[arg-type]


def test_bool_is_not_counted_as_int():
    # Guard against bool sneaking through (bool subclasses int).
    n = normalize_usage({"input_tokens": True, "output_tokens": 5}, provider_hint="anthropic")
    assert n.prompt_tokens is None
    assert n.completion_tokens == 5


def test_prefix_hash_stable_for_identical_prefix():
    a = {
        "system": "You are a helpful analyst.",
        "tools": [{"name": "read"}],
        "messages": [{"role": "user", "content": "hi"}],
    }
    b = {
        "system": "You are a helpful analyst.",
        "tools": [{"name": "read"}],
        "messages": [{"role": "user", "content": "totally different question"}],
    }
    # Same system+tools, different user turn → same prefix hash.
    assert cache_prefix_hash(a) == cache_prefix_hash(b)
    assert cache_prefix_hash(a) is not None


def test_prefix_hash_changes_when_timestamp_injected():
    base = {"system": "You are a helpful analyst.", "tools": [{"name": "read"}]}
    leaky = {
        "system": "You are a helpful analyst. Current time: 2026-06-12T14:23:11Z",
        "tools": [{"name": "read"}],
    }
    assert cache_prefix_hash(base) != cache_prefix_hash(leaky)


def test_prefix_hash_openai_leading_system_messages():
    a = {
        "messages": [
            {"role": "system", "content": "stable prompt"},
            {"role": "user", "content": "q1"},
        ]
    }
    b = {
        "messages": [
            {"role": "system", "content": "stable prompt"},
            {"role": "user", "content": "q2"},
        ]
    }
    assert cache_prefix_hash(a) == cache_prefix_hash(b)


def test_prefix_hash_none_when_nothing_cacheable():
    assert cache_prefix_hash({"messages": [{"role": "user", "content": "hi"}]}) is None
    assert cache_prefix_hash({}) is None
    assert cache_prefix_hash(None) is None
