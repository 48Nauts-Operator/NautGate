import pytest

from app.classify import Classification
from app.confidentiality import (
    ConfidentialityPolicyError,
    classify_confidentiality,
    confidential_route_model,
    redact_bowden,
)


def _classification(sensitivity: str = "none") -> Classification:
    return Classification(sensitivity=sensitivity, reason=None, signals=[])


def test_bowden_upgrades_swiss_identifier_without_exposing_value():
    raw = "756.9217.0769.85"
    result = classify_confidentiality(_classification(), f"AHV {raw}")

    assert result.classification.sensitivity == "pii"
    assert result.bowden_labels == {"AHV": 1}
    assert any(s["rule_id"] == "bowden:ch_ahv_ean13_v1" for s in result.classification.signals)
    assert raw not in repr(result.classification.signals)


def test_caller_declaration_can_upgrade_but_not_downgrade():
    upgraded = classify_confidentiality(_classification(), "hello", declaration="restricted")
    assert upgraded.classification.sensitivity == "secret"

    unchanged = classify_confidentiality(_classification("secret"), "hello", declaration="public")
    assert unchanged.classification.sensitivity == "secret"


def test_invalid_caller_declaration_is_rejected():
    with pytest.raises(ConfidentialityPolicyError, match="invalid X-NautGate"):
        classify_confidentiality(_classification(), "hello", declaration="maybe")


def test_confidential_route_is_fail_closed_and_lmstudio_only():
    assert confidential_route_model("pii", {"enabled": False}) is None
    assert (
        confidential_route_model("none", {"enabled": True, "local_model": "lmstudio/qwen"}) is None
    )
    assert (
        confidential_route_model("pii", {"enabled": True, "local_model": "lmstudio/qwen"})
        == "lmstudio/qwen"
    )
    with pytest.raises(ConfidentialityPolicyError, match="no local model"):
        confidential_route_model("secret", {"enabled": True})
    with pytest.raises(ConfidentialityPolicyError, match="lmstudio"):
        confidential_route_model(
            "secret", {"enabled": True, "local_model": "openrouter/cloud-model"}
        )


def test_bowden_redaction_does_not_return_raw_value():
    raw = "CH93 0076 2011 6238 5295 7"
    redacted = redact_bowden(f"Send funds to {raw} today")
    assert raw not in redacted
    assert "[bowden-iban-redacted]" in redacted
