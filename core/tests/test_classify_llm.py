"""CLASSIFY slow-path — LLM-confirm tests."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.classify import Classification
from app.classify_llm import (
    is_ambiguous,
    llm_confirm,
    maybe_upgrade_classification,
)

# --- is_ambiguous heuristic -----------------------------------------------


@pytest.mark.parametrize("text", [None, "", "short text"])
def test_is_ambiguous_skips_short_or_empty(text):
    assert is_ambiguous(text) is False


def test_is_ambiguous_skips_long_clean_text():
    assert is_ambiguous("write me a haiku about clouds. " * 5) is False


@pytest.mark.parametrize(
    "phrase",
    [
        "this is private — do not share",
        "internal use only please",
        "the document is confidential and sensitive",
        "subject to NDA",
        "proprietary trade secret",
    ],
)
def test_is_ambiguous_catches_hint_phrases(phrase):
    text = phrase + (" " * 100)  # padded to clear length floor
    assert is_ambiguous(text) is True


# --- llm_confirm ----------------------------------------------------------


def _llm_response(text: str) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


@pytest.mark.asyncio
async def test_llm_confirm_returns_pii_on_pii_verdict():
    nr = AsyncMock()
    nr.chat_completions.return_value = _llm_response("PII\nLooks like personal info")

    s, reason = await llm_confirm("x" * 100, nr, model="claude-haiku-4-5")
    assert s == "pii"
    assert reason == "Looks like personal info"


@pytest.mark.asyncio
async def test_llm_confirm_returns_secret_on_secret_verdict():
    nr = AsyncMock()
    nr.chat_completions.return_value = _llm_response("SECRET\nlooks like a credential")
    s, _ = await llm_confirm("x" * 100, nr, model="x")
    assert s == "secret"


@pytest.mark.asyncio
async def test_llm_confirm_handles_unknown_verdict():
    nr = AsyncMock()
    nr.chat_completions.return_value = _llm_response("MAYBE\nunsure")
    s, _ = await llm_confirm("x" * 100, nr, model="x")
    assert s == "none"


@pytest.mark.asyncio
async def test_llm_confirm_short_input_skips_llm():
    nr = AsyncMock()
    s, _ = await llm_confirm("hi", nr, model="x")
    assert s == "none"
    nr.chat_completions.assert_not_called()


@pytest.mark.asyncio
async def test_llm_confirm_timeout_falls_through():
    async def slow(_payload):
        await asyncio.sleep(2)
        return _llm_response("PII\n")

    nr = AsyncMock()
    nr.chat_completions = slow
    s, _ = await llm_confirm("x" * 100, nr, model="x", timeout_s=0.05)
    assert s == "none"


@pytest.mark.asyncio
async def test_llm_confirm_swallows_exceptions():
    async def boom(_payload):
        raise RuntimeError("upstream went away")

    nr = AsyncMock()
    nr.chat_completions = boom
    s, _ = await llm_confirm("x" * 100, nr, model="x")
    assert s == "none"


@pytest.mark.asyncio
async def test_llm_confirm_handles_non_dict_response():
    nr = AsyncMock()
    nr.chat_completions.return_value = ["error", "list"]
    s, _ = await llm_confirm("x" * 100, nr, model="x")
    assert s == "none"


# --- maybe_upgrade_classification -----------------------------------------


@pytest.mark.asyncio
async def test_maybe_upgrade_disabled_short_circuits():
    base = Classification(sensitivity="none", reason=None, signals=[])
    nr = AsyncMock()
    out = await maybe_upgrade_classification(
        base, text="x" * 100 + " confidential", nautrouter=nr, enabled=False, model="x"
    )
    assert out is base
    nr.chat_completions.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_upgrade_skips_when_fast_path_already_flagged():
    """LLM never downgrades pii or secret."""
    base = Classification(
        sensitivity="pii",
        reason="email",
        signals=[{"rule_id": "email", "sensitivity": "pii", "count": 1}],
    )
    nr = AsyncMock()
    nr.chat_completions.return_value = _llm_response("NONE\nclean")
    out = await maybe_upgrade_classification(
        base, text="confidential " * 30, nautrouter=nr, enabled=True, model="x"
    )
    assert out.sensitivity == "pii"
    nr.chat_completions.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_upgrade_skips_unambiguous_text():
    base = Classification(sensitivity="none", reason=None, signals=[])
    nr = AsyncMock()
    out = await maybe_upgrade_classification(
        base, text="write me a haiku about clouds", nautrouter=nr, enabled=True, model="x"
    )
    assert out.sensitivity == "none"
    nr.chat_completions.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_upgrade_upgrades_to_pii():
    base = Classification(sensitivity="none", reason=None, signals=[])
    nr = AsyncMock()
    nr.chat_completions.return_value = _llm_response("PII\npersonal data found")
    text = "this document is confidential. " * 10  # ambiguous + ≥50 chars
    out = await maybe_upgrade_classification(
        base, text=text, nautrouter=nr, enabled=True, model="x"
    )
    assert out.sensitivity == "pii"
    assert out.signals[-1]["rule_id"] == "llm_confirm"
    assert out.signals[-1]["sensitivity"] == "pii"
    assert "llm_confirm:pii" in (out.reason or "")


@pytest.mark.asyncio
async def test_maybe_upgrade_returns_unchanged_on_none_verdict():
    base = Classification(sensitivity="none", reason=None, signals=[])
    nr = AsyncMock()
    nr.chat_completions.return_value = _llm_response("NONE\nclean text")
    text = "this document is confidential. " * 10
    out = await maybe_upgrade_classification(
        base, text=text, nautrouter=nr, enabled=True, model="x"
    )
    # No upgrade → caller's classification is returned as-is.
    assert out.sensitivity == "none"
    assert all(s["rule_id"] != "llm_confirm" for s in out.signals)
