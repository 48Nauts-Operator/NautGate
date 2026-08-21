"""Day 4c — body-capture policy gate."""

import json

from app.capture import (
    BODY_CAPTURE_CAP_BYTES_DEFAULT,
    capture_prompt,
    capture_response,
    redact,
)


def _msg(text: str) -> list[dict]:
    return [{"role": "user", "content": text}]


# --- redact() ---------------------------------------------------------------


def test_redact_replaces_email():
    out = redact("hello@48nauts.com is mine")
    assert "[email-redacted]" in out
    assert "@48nauts.com" not in out


def test_redact_replaces_secret():
    out = redact("token ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789ab now")
    assert "[github_pat-redacted]" in out
    assert "ghp_" not in out


def test_redact_no_op_on_clean_text():
    assert redact("write me a haiku") == "write me a haiku"


# --- capture_prompt: policy gate -------------------------------------------


def test_capture_prompt_none_keeps_full_body():
    out = capture_prompt(_msg("hello clouds"), "none")
    assert out.body is not None
    assert "hello clouds" in out.body
    assert out.truncated_at_byte is None


def test_capture_prompt_pii_redacts_but_keeps_body():
    out = capture_prompt(_msg("email me at hello@48nauts.com"), "pii")
    assert out.body is not None
    assert "[email-redacted]" in out.body
    assert "@48nauts.com" not in out.body


def test_capture_prompt_pii_redacts_bowden_swiss_identifier():
    raw = "756.9217.0769.85"
    out = capture_prompt(_msg(f"My AHV is {raw}"), "pii")
    assert out.body is not None
    assert raw not in out.body
    assert "[bowden-ahv-redacted]" in out.body


def test_capture_prompt_secret_returns_none():
    out = capture_prompt(_msg("ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789ab"), "secret")
    assert out.body is None
    assert out.truncated_at_byte is None


def test_capture_prompt_empty_messages_returns_none():
    assert capture_prompt(None, "none").body is None
    assert capture_prompt([], "none").body is None


def test_capture_prompt_truncates_oversized():
    big = "x" * (BODY_CAPTURE_CAP_BYTES_DEFAULT + 1024)
    out = capture_prompt(_msg(big), "none")
    assert out.body is not None
    assert out.truncated_at_byte is not None
    assert out.truncated_at_byte <= BODY_CAPTURE_CAP_BYTES_DEFAULT
    assert "[truncated:" in out.body


def test_capture_prompt_serializes_full_messages_array():
    msgs = [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "hi"},
    ]
    out = capture_prompt(msgs, "none")
    parsed = json.loads(out.body)
    assert parsed == msgs


# --- capture_response: policy gate -----------------------------------------


def test_capture_response_dict_serialized():
    resp = {"choices": [{"message": {"content": "ok"}}]}
    out = capture_response(resp, "none")
    assert json.loads(out.body) == resp


def test_capture_response_string_passthrough():
    out = capture_response("the answer is 42", "none")
    assert out.body == "the answer is 42"


def test_capture_response_pii_redacts():
    out = capture_response("write to hello@48nauts.com", "pii")
    assert "[email-redacted]" in out.body


def test_capture_response_secret_suppressed():
    out = capture_response({"choices": [{"message": {"content": "secret-stuff"}}]}, "secret")
    assert out.body is None


def test_capture_response_none_input_returns_none():
    assert capture_response(None, "none").body is None
    assert capture_response("", "none").body is None
