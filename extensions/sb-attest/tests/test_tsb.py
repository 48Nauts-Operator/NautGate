"""Offline checks for the TSB request the HSM will see.

Nothing here talks to Securosys. The demo TSB is borrowed access, and a test
suite that calls it on every run would abuse that; the one live check is
`scripts/live_smoke.py`, which does nothing unless SB_ATTEST_LIVE=1.

What these lock down is the part a live call cannot tell you apart: whether
the body and headers are the ones the API documents. A wrong header comes back
as a generic 500, which is indistinguishable from a hardware problem.
"""

import base64
import json

import pytest

from main import digest_bytes
from tsb import TsbConfig, TsbError, _parse_error, build_request

CFG = TsbConfig(url="https://example.test/tsb", key_name="K", api_key=None, jwt=None)


def test_the_payload_is_base64_of_the_raw_digest():
    body = build_request(CFG, b"\x01\x02\x03")
    assert base64.b64decode(body["signRequest"]["payload"]) == b"\x01\x02\x03"
    assert body["signRequest"]["signKeyName"] == "K"
    assert body["signRequest"]["signatureAlgorithm"] == "SHA256_WITH_RSA"
    assert body["signRequest"]["signatureType"] == "DER"


def test_the_body_is_wrapped_in_signRequest():
    # TSB rejects a bare body with a generic error, so the wrapper is the
    # difference between working and an unexplained 500.
    assert list(build_request(CFG, b"x")) == ["signRequest"]


def test_a_key_password_is_only_sent_when_set():
    assert "keyPassword" not in build_request(CFG, b"x")["signRequest"]
    with_pw = TsbConfig(url=CFG.url, key_name="K", key_password="s3cret")
    assert build_request(with_pw, b"x")["signRequest"]["keyPassword"] == "s3cret"


@pytest.mark.parametrize(
    "api_key,jwt,expected",
    [
        ("abc", None, {"X-API-KEY": "abc"}),
        (None, "jjj", {"Authorization": "Bearer jjj"}),
        ("abc", "jjj", {"X-API-KEY": "abc", "Authorization": "Bearer jjj"}),
        (None, None, {}),
    ],
)
def test_both_credentials_are_optional_and_either_alone_is_enough(api_key, jwt, expected):
    h = TsbConfig(url=CFG.url, key_name="K", api_key=api_key, jwt=jwt).headers()
    for k, v in expected.items():
        assert h[k] == v
    assert ("X-API-KEY" in h) == bool(api_key)
    assert ("Authorization" in h) == bool(jwt)


@pytest.mark.parametrize(
    "url",
    ["https://x.test/tsb", "https://x.test/tsb/", "https://x.test/tsb/v1", "https://x.test/tsb/v1/synchronousSign"],
)
def test_the_endpoint_is_the_same_however_the_url_was_written(url):
    # A route is not a base: appending /v1/synchronousSign to a URL that
    # already ends in /v1 gives a 404 that reads like a broken deployment.
    assert TsbConfig(url=url, key_name="K").endpoint() == "https://x.test/tsb/v1/synchronousSign"


def test_real_tsb_errors_keep_their_reason_code():
    # Captured from the engineering demo, so the mapping is not invented.
    not_found = _parse_error(404, json.dumps(
        {"errorCode": 650, "reason": "res.error.key.not.existent",
         "message": "Key with name NOPE not found"}).encode())
    assert not_found.reason == "res.error.key.not.existent"
    assert "NOPE" in str(not_found)

    hsm = _parse_error(500, json.dumps(
        {"errorCode": 701, "reason": "res.error.in.hsm",
         "message": "KEY_FUNCTION_NOT_PERMITTED"}).encode())
    assert hsm.status == 500
    assert "KEY_FUNCTION_NOT_PERMITTED" in str(hsm)

    # Not every failure is JSON.
    assert isinstance(_parse_error(502, b"<html>gateway</html>"), TsbError)


def test_a_digest_must_be_hex():
    # The digest is the thing being attested. Signing the ASCII of a typo
    # yields a receipt that verifies against nothing.
    assert digest_bytes("a9ccbdea") == b"\xa9\xcc\xbd\xea"
    assert digest_bytes("0xA9CCBDEA") == b"\xa9\xcc\xbd\xea"
    for bad in ["", "zz", "abc", "hello world"]:
        with pytest.raises(ValueError):
            digest_bytes(bad)
