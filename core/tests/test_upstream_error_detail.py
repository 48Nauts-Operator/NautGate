"""A failed call must say WHY.

A bare 502 "upstream_failed" is the first thing a new user meets when they
install without a provider key, and it gives them nothing to act on.
"""

import httpx

from app.routes.v1 import _upstream_detail, _upstream_reason


def test_reason_from_openai_shaped_error():
    r = httpx.Response(401, json={"error": {"message": "Missing Authentication header", "code": 401}})
    assert _upstream_reason(r) == "Missing Authentication header"


def test_reason_from_detail_key_and_plain_text():
    assert _upstream_reason(httpx.Response(400, json={"detail": "nope"})) == "nope"
    assert _upstream_reason(httpx.Response(500, text="boom")) == "boom"


def test_reason_is_capped_and_never_raises():
    assert len(_upstream_reason(httpx.Response(400, text="x" * 900))) == 200
    assert _upstream_reason(httpx.Response(204)) is None


def test_401_names_the_missing_credential_and_the_fix():
    d = _upstream_detail(401, "Missing Authentication header", "openrouter")
    assert "no working provider credential" in d
    assert "openrouter" in d
    assert "Settings" in d and "OPENROUTER_API_KEY" in d


def test_passthrough_lane_is_not_named_as_a_provider():
    # "passthrough" is a routing lane, not something you can put a key in.
    assert "for passthrough" not in _upstream_detail(403, None, "passthrough")


def test_other_statuses_keep_the_reason_but_stay_generic():
    assert _upstream_detail(500, "kaboom", "openai") == "upstream_failed (500): kaboom"
    assert _upstream_detail(502, None, None) == "upstream_failed"
