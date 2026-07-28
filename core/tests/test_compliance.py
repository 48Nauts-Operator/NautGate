"""NAUTGATE-25 — compliance AUDIT layer. Labels and flags, never gates."""

from pathlib import Path

import pytest

from app.compliance import Policy, build_trace, load_policy, strictest

POLICY_PATH = Path(__file__).resolve().parents[2] / "config" / "compliance.yaml"


@pytest.fixture(scope="module")
def policy() -> Policy:
    return load_policy(POLICY_PATH)


# ---- scope ---------------------------------------------------------------


def test_evaluated_against_covers_establishment_and_markets(policy):
    # CH establishment + CH/EU markets → Swiss and EU regimes both in scope.
    assert policy.evaluated_against() == ["CH-FADP", "EU-GDPR", "EU-AI-Act"]


def test_markets_pull_in_regimes_beyond_establishment():
    # The extraterritorial case: a Swiss firm serving EU customers is in GDPR
    # scope regardless of where it sits. This is why markets is asked separately.
    p = Policy(
        {
            "scope": {"establishment": "CH", "markets": ["CH"]},
            "regimes": {"CH": ["CH-FADP"], "EU": ["EU-GDPR", "EU-AI-Act"]},
        }
    )
    assert p.evaluated_against() == ["CH-FADP"]
    p2 = Policy(
        {
            "scope": {"establishment": "CH", "markets": ["CH", "EU"]},
            "regimes": {"CH": ["CH-FADP"], "EU": ["EU-GDPR", "EU-AI-Act"]},
        }
    )
    assert "EU-GDPR" in p2.evaluated_against()


def test_sector_overlays_are_off_by_default(policy):
    assert policy.scope.get("sectors") == []
    assert not any("FINMA" in r for r in policy.evaluated_against())


# ---- labels --------------------------------------------------------------


def test_strictest_picks_the_higher_band():
    assert strictest("G", "R", "Y") == "R"
    assert strictest("G") == "G"
    assert strictest() == "G"


def test_unknown_activity_fails_closed(policy):
    # Under-flagging is the failure an audit layer cannot afford, so unknown
    # traffic reads as the stricter band, not the looser one.
    t = build_trace(policy, activity="something-we-have-never-seen", provider_name="lmstudio")
    assert t.confidence == "fallback"
    assert t.label == "O"


def test_personal_data_lifts_a_routine_activity(policy):
    # A "public" activity that turns out to carry personal data is not public.
    t = build_trace(policy, activity="web-research", sensitivity="pii", provider_name="lmstudio")
    assert t.label == "Y"
    assert t.data_class == "personal"


def test_declared_activity_is_marked_declared(policy):
    t = build_trace(policy, activity="cv-screening", provider_name="lmstudio")
    assert t.confidence == "declared"
    assert t.label == "R"


# ---- the three mock scenarios -------------------------------------------


def test_scenario_research_is_clean(policy):
    t = build_trace(policy, activity="web-research", sensitivity="none", provider_name="openrouter")
    assert t.label == "G"
    assert t.flags == []
    assert t.destination["third_country_transfer"] is True  # recorded, not flagged
    assert t.regimes_touched == []


def test_scenario_business_email_local_is_clean_but_recorded(policy):
    t = build_trace(policy, activity="business-email", sensitivity="pii", provider_name="lmstudio")
    assert t.label == "O"
    assert t.flags == []
    assert t.destination["third_country_transfer"] is False
    assert "EU-GDPR" in t.regimes_touched and "CH-FADP" in t.regimes_touched


def test_scenario_cv_screening_raises_three_flags(policy):
    # The hero row: it ran, nothing was blocked, and the record says why it matters.
    t = build_trace(policy, activity="cv-screening", sensitivity="pii", provider_name="openrouter")
    assert t.label == "R"
    ids = {f.id for f in t.flags}
    assert ids == {"third-country-transfer", "high-risk-no-assessment", "no-human-review"}
    assert {f.severity for f in t.flags if f.id == "third-country-transfer"} == {"critical"}
    assert "EU-AI-Act" in t.regimes_touched


# ---- individual flag rules ----------------------------------------------


def test_same_call_locally_raises_nothing(policy):
    # Identical activity and data, routed locally → no flags. The destination is
    # what makes the difference, and that is the point of recording it.
    t = build_trace(
        policy,
        activity="cv-screening",
        sensitivity="pii",
        provider_name="lmstudio",
        has_assessment=True,
        has_human_review=True,
    )
    assert t.flags == []


def test_dpa_provider_does_not_raise_transfer_flag(policy):
    t = build_trace(
        policy, activity="business-email", sensitivity="pii", provider_name="infomaniak"
    )
    assert [f.id for f in t.flags] == []


def test_secret_to_external_api_is_critical(policy):
    t = build_trace(
        policy, activity="proprietary-code", sensitivity="secret", provider_name="openrouter"
    )
    ids = {f.id for f in t.flags}
    assert "secret-to-external" in ids
    assert next(f for f in t.flags if f.id == "secret-to-external").severity == "critical"


def test_prohibited_practice_flags_even_when_local(policy):
    t = build_trace(
        policy,
        activity="biometric-inference",
        sensitivity="pii",
        provider_name="lmstudio",
        has_assessment=True,
        has_human_review=True,
    )
    assert t.label == "X"
    assert "prohibited-practice" in {f.id for f in t.flags}


def test_evidence_clears_the_oversight_flags(policy):
    t = build_trace(
        policy,
        activity="cv-screening",
        sensitivity="pii",
        provider_name="infomaniak",
        has_assessment=True,
        has_human_review=True,
    )
    assert t.flags == []


# ---- shape ---------------------------------------------------------------


def test_trace_serialises_for_the_audit_row(policy):
    t = build_trace(policy, activity="cv-screening", sensitivity="pii", provider_name="openrouter")
    d = t.to_dict()
    assert d["evaluated_against"] == ["CH-FADP", "EU-GDPR", "EU-AI-Act"]
    assert d["destination"]["provider"] == "openrouter"
    assert d["provider_terms"]["retention"] == "30d"
    assert all({"id", "severity", "regime", "title", "detail"} <= set(f) for f in d["flags"])
