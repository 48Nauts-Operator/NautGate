from app.safeguard import extract_safeguard_evidence, sanitize_iteration


def test_extracts_provider_confirmed_refusal_without_content():
    payload = {
        "type": "message",
        "model": "claude-fable-5-1",
        "content": [{"type": "text", "text": "must not be copied"}],
        "stop_reason": "refusal",
        "stop_details": {
            "type": "safety",
            "category": "cybersecurity",
            "recommended_model": "claude-opus-4-8",
            "internal_note": "must not be retained",
        },
        "usage": {"input_tokens": 12, "output_tokens": 0},
    }

    evidence = extract_safeguard_evidence([payload])

    assert evidence == {
        "extractor_version": "safeguard-v1",
        "evidence_level": "provider_confirmed",
        "stop_reason": "refusal",
        "stop_details": {
            "type": "safety",
            "category": "cybersecurity",
            "recommended_model": "claude-opus-4-8",
        },
        "served_model": "claude-fable-5-1",
        "fallback_blocks": [],
        "usage_iterations": [],
    }
    assert "must not be copied" not in str(evidence)
    assert "internal_note" not in str(evidence)


def test_extracts_fallback_boundary_and_sanitized_iterations():
    payload = {
        "model": "claude-opus-4-8",
        "content": [
            {
                "type": "fallback",
                "from_model": "claude-fable-5-1",
                "to": {"model": "claude-opus-4-8"},
            },
            {"type": "text", "text": "private generated response"},
        ],
        "usage": {
            "iterations": [
                {
                    "type": "fallback_message",
                    "model": "claude-fable-5-1",
                    "stop_reason": "refusal",
                    "stop_details": {"category": "biology"},
                    "usage": {"input_tokens": 25, "output_tokens": 0},
                    "content": "must not survive",
                },
                {
                    "type": "message",
                    "model": "claude-opus-4-8",
                    "usage": {"input_tokens": 25, "output_tokens": 9},
                },
            ]
        },
    }

    evidence = extract_safeguard_evidence([payload])

    assert evidence["served_model"] == "claude-opus-4-8"
    assert evidence["fallback_blocks"] == [
        {
            "from_model": "claude-fable-5-1",
            "to_model": "claude-opus-4-8",
        }
    ]
    assert evidence["usage_iterations"][0]["stop_details"] == {"category": "biology"}
    assert "content" not in evidence["usage_iterations"][0]
    assert "private generated response" not in str(evidence)


def test_infrastructure_error_is_not_a_safeguard_event():
    assert (
        extract_safeguard_evidence([{"type": "error", "error": {"type": "overloaded_error"}}])
        is None
    )


def test_iteration_whitelist_rejects_credentials_and_content():
    cleaned = sanitize_iteration(
        {
            "type": "message",
            "model": "claude-opus-4-8",
            "usage": {"input_tokens": 1, "output_tokens": 2, "price": 99},
            "authorization": "Bearer secret",
            "content": "sensitive",
        }
    )
    assert cleaned == {
        "type": "message",
        "model": "claude-opus-4-8",
        "usage": {"input_tokens": 1, "output_tokens": 2},
    }
