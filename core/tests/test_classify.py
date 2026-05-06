"""Day 4b — fast-path sensitivity classifier.

Pure-function tests. Each rule has a positive case + a negative case so we don't drift.
"""

import pytest

from app.classify import Classification, assemble_user_text, classify

# --- Trivial cases ----------------------------------------------------------


@pytest.mark.parametrize("text", ["", None, "   "])
def test_empty_input_is_none(text):
    out = classify(text)
    assert out.sensitivity == "none"
    assert out.signals == []


def test_clean_text_is_none():
    out = classify("write me a haiku about clouds")
    assert out.sensitivity == "none"
    assert out.signals == []


# --- PII rules --------------------------------------------------------------


@pytest.mark.parametrize(
    "text,rule_id",
    [
        ("contact me at hello@48nauts.com please", "email"),
        ("here is alice.doe+work@sub.example.co", "email"),
        ("call (415) 555-0142 between 9 and 5", "phone_us"),
        ("phone: 415-555-0142 -- mobile", "phone_us"),
        ("SSN 123-45-6789 on file", "ssn_us"),
        ("card 4111 1111 1111 1111 expires 12/26", "credit_card_like"),
    ],
)
def test_pii_matches(text, rule_id):
    out = classify(text)
    assert out.sensitivity == "pii"
    assert any(s["rule_id"] == rule_id for s in out.signals), out


# --- Secret rules (override PII) -------------------------------------------


@pytest.mark.parametrize(
    "text,rule_id",
    [
        ("export OPENAI_API_KEY=sk-abc123def456ghi789jkl0", "openai_api_key"),
        (
            "key: sk-ant-api03-abcDEFghiJKLmnoPQRstuVWXyz0123456789ABCdefGHI_jklMNO",
            "anthropic_api_key",
        ),
        ("commit token ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789ab signed off", "github_pat"),
        ("AKIAIOSFODNN7EXAMPLE is the access key", "aws_access_key_id"),
        ("AIzaSyD_abcdefghijklmnopqrstuvwxyz01234 is mine", "google_api_key"),
        ("xoxb-1234567890-abcdef-ghIJKL is from slack", "slack_token"),
        ("processed via sk_live_0123456789abcdefghijKLMN stripe key", "stripe_key"),
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJB...", "private_key_block"),
        (
            "auth eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.tWqrNL5jKqRzHN8mP5Lk",
            "jwt",
        ),
        (
            "internal token ng_e66b0ba05bb94d698b2b351f729bbeaa_QMKKarzhuQq1CuYpFarFMjLwuElOZSQNTufjRmC6Iqg",
            "nautgate_token",
        ),
    ],
)
def test_secret_matches(text, rule_id):
    out = classify(text)
    assert out.sensitivity == "secret", f"expected secret for {rule_id}, got {out}"
    assert any(s["rule_id"] == rule_id for s in out.signals), out


def test_secret_overrides_pii():
    """Both secret and PII present → result is secret, but signals carry the PII too."""
    text = "email me at hello@48nauts.com and use ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789ab"
    out = classify(text)
    assert out.sensitivity == "secret"
    rule_ids = {s["rule_id"] for s in out.signals}
    assert "github_pat" in rule_ids
    assert "email" in rule_ids


def test_reason_string_lists_all_signals():
    out = classify("email hello@48nauts.com card 4111111111111111")
    assert out.sensitivity == "pii"
    assert out.reason is not None
    assert "email" in out.reason
    assert "credit_card_like" in out.reason


# --- Negative regression cases ---------------------------------------------


def test_short_digit_run_is_not_credit_card():
    """credit_card_like rule needs ≥13 digits — phone numbers shouldn't match it."""
    out = classify("call 555-1234 to confirm")
    assert all(s["rule_id"] != "credit_card_like" for s in out.signals)


def test_to_db_shape():
    out = Classification(
        sensitivity="pii",
        reason="email",
        signals=[{"rule_id": "email", "sensitivity": "pii", "count": 1}],
    )
    db = out.to_db()
    assert db["sensitivity"] == "pii"
    assert db["signals"] == [{"rule_id": "email", "sensitivity": "pii", "count": 1}]


def test_to_db_none_when_signals_empty():
    out = Classification(sensitivity="none", reason=None, signals=[])
    db = out.to_db()
    assert db["sensitivity"] == "none"
    assert db["signals"] is None


# --- assemble_user_text helper ---------------------------------------------


def test_assemble_concatenates_user_messages_only():
    messages = [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "hello there"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "follow up"},
    ]
    assert assemble_user_text(messages) == "hello there\nfollow up"


def test_assemble_handles_block_content():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "image", "source": {"data": "..."}},
                {"type": "text", "text": "world"},
            ],
        },
    ]
    assert assemble_user_text(messages) == "hello\nworld"


def test_assemble_handles_empty():
    assert assemble_user_text(None) == ""
    assert assemble_user_text([]) == ""
    assert assemble_user_text([{"role": "user", "content": ""}]) == ""
