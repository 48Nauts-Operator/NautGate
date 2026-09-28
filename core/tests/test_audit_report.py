from app.audit_report import render_receipt_report

RECEIPT = {
    "schema": "dev.nautgate.decision-receipt/v1",
    "receipt_id": "88328fd3-dd5f-4e39-ad9d-c337ee421e29",
    "decision_id": "55b084cf-259f-4b35-8e01-173f5c5df614",
    "sequence": 204411,
    "started_at": "2026-09-28T13:30:58.000000Z",
    "completed_at": "2026-09-28T13:31:00.000000Z",
    "client": {"agent_id": "enga", "nautgate_key_id": "ng_ab12", "protocol": "openai_chat"},
    "request": {
        "requested_model": "auto",
        "stream": False,
        "sampling": {"temperature": "0", "top_p": None, "seed": 7},
        "body_sha256": "0" * 64,
        "prompt_sha256": "2" * 64,
        "upstream_body_sha256": None,
        "tools_sha256": None,
    },
    "classification": {"sensitivity": "none", "signals_sha256": None},
    "routing": {
        "selected_provider": "openrouter",
        "selected_model": "openrouter/deepseek/deepseek-v4-flash",
        "observed_model": "deepseek-v4-flash",
        "observed_provider": "openrouter",
        "substituted": False,
        "fallback_attempts": [],
        "reason_code": "auto",
        "selected_transport": "openrouter",
    },
    "result": {
        "status": "success",
        "upstream_status": 200,
        "response_sha256": "4" * 64,
        "finish_reason": "stop",
        "input_tokens": 12,
        "output_tokens": 2,
        "cost_microusd": 12,
        "error_code": None,
        "provider_fingerprint": None,
    },
    "environment": {"harness": "curl/8.4", "sandbox_id": None, "capture_path": "gateway"},
    "model_integrity": {
        "weights_digest": None,
        "digest_source": "unresolved",
        "resolved_at": None,
    },
    "tool_evidence": {"calls_observed": 0, "executions_observed": 0, "events_sha256": None},
    "runtime": {"nautgate_version": "0.5.1", "instance_id": "stargate", "config_sha256": None},
}

META = {
    "attested": True,
    "checkpoint_id": "fc286c38-a9b8-5135-8242-0dc1e3434344",
    "key_id": "NAUTGATE_AUDIT_KEY",
    "key_fingerprint": "54922529a30f6c2cab0d701dec8402280f1a55dc0f8da300de8a1c1c374bab4c",
}


def test_report_is_a_complete_html_document_with_the_material_facts():
    html = render_receipt_report(RECEIPT, META)
    for fact in (
        "88328fd3-dd5f-4e39-ad9d-c337ee421e29",
        "openrouter/deepseek/deepseek-v4-flash",
        "temperature",
        ">0<",  # the recorded temperature value
        "seed",
        "204411",
        "NAUTGATE_AUDIT_KEY",
        "54922529a30f",
        "nautgate receipt verify",
        "gateway",
    ):
        assert fact in html, fact
    assert html.lstrip().lower().startswith("<!doctype html")


def test_report_states_pending_when_not_attested():
    html = render_receipt_report(RECEIPT, {**META, "attested": False, "checkpoint_id": None})
    assert "NOT YET ATTESTED" in html


def test_report_escapes_client_controlled_strings():
    hostile = dict(RECEIPT)
    hostile["environment"] = {
        "harness": "<script>alert(1)</script>",
        "sandbox_id": None,
        "capture_path": "gateway",
    }
    html = render_receipt_report(hostile, META)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_verify_verdict_page_states_the_result_in_house_style():
    from app.audit_report import render_verify_verdict

    html = render_verify_verdict(
        ok=True,
        receipt_id="88328fd3-dd5f-4e39-ad9d-c337ee421e29",
        detail={"Sequence": 204411, "Signing key": "NAUTGATE_AUDIT_KEY"},
    )
    assert "VERIFIED" in html
    assert "88328fd3" in html
    assert "204411" in html
    assert "#808000" in html  # NG brand olive, public-report baseline

    failed = render_verify_verdict(ok=False, receipt_id="x", detail={})
    assert "NOT VERIFIED" in failed
