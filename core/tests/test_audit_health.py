from app.audit_meta import audit_health


def _status(**over):
    base = {
        "pending": 0, "failed": 0, "open_gaps": 0, "checkpoint_failures": 0,
        "signing_lag_seconds": 0, "decisions_last_hour": 10, "receipts_last_hour": 10,
    }
    base.update(over)
    return base


def test_healthy_when_enabled_and_current():
    health, alerts = audit_health(_status(), enabled=True, lag_warning_s=300, lag_critical_s=3600)
    assert health == "healthy" and alerts == []


def test_disabled_with_unsigned_traffic_is_critical_not_calm():
    health, alerts = audit_health(
        _status(pending=86000), enabled=False, lag_warning_s=300, lag_critical_s=3600
    )
    assert health == "critical"
    assert any(a["code"] == "attestation_disabled_with_traffic" for a in alerts)


def test_disabled_with_no_traffic_is_just_disabled():
    health, alerts = audit_health(
        _status(decisions_last_hour=0, receipts_last_hour=0),
        enabled=False, lag_warning_s=300, lag_critical_s=3600,
    )
    assert health == "disabled" and alerts == []


def test_receipt_shortfall_raises_coverage_alert():
    health, alerts = audit_health(
        _status(decisions_last_hour=100, receipts_last_hour=80),
        enabled=True, lag_warning_s=300, lag_critical_s=3600,
    )
    assert any(a["code"] == "receipt_coverage" for a in alerts)
    assert health in ("warning", "critical")


def test_signing_lag_thresholds_still_fire():
    health, alerts = audit_health(
        _status(signing_lag_seconds=4000, pending=5),
        enabled=True, lag_warning_s=300, lag_critical_s=3600,
    )
    assert health == "critical"
    assert any(a["code"] == "signing_lag" and a["severity"] == "critical" for a in alerts)
