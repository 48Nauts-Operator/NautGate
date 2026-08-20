"""Securosys Transaction Security Broker (TSB) client — synchronous signing.

Only one operation is used: POST /v1/synchronousSign, which asks the HSM to
sign a payload with a named key whose private half never leaves the hardware.

  https://docs.securosys.com/tsb/overview
  POST /v1/synchronousSign  ->  {"signature": "<base64>"}

Auth is either an API key header or a bearer JWT; the OpenAPI document lists
both as alternatives (`XApiKeyAuth`, `bearerAuth`), so both are optional here
and either alone is enough. Some deployments (the engineering demo, for one)
accept unauthenticated calls; that is a property of the deployment, not
something to rely on, so a run with neither credential is logged loudly.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass

log = logging.getLogger("sb-attest.tsb")

# The HSM signs raw bytes we hand it. UNSPECIFIED means "no structured payload
# type" — the other values (ISO_20022, PDF, BTC, ETH, HEX) tell TSB how to
# decode a payload for display in an approval flow, which synchronous signing
# does not have.
PAYLOAD_TYPE = "UNSPECIFIED"


class TsbError(RuntimeError):
    """A TSB call that did not return a signature.

    `reason` is Securosys' machine-readable code when it sent one
    (`res.error.key.not.existent`, `res.error.in.hsm`, ...), which is far more
    useful to an operator than the HTTP status alone.
    """

    def __init__(self, message: str, *, status: int | None = None, reason: str | None = None):
        super().__init__(message)
        self.status = status
        self.reason = reason


@dataclass(frozen=True)
class TsbConfig:
    url: str
    key_name: str
    api_key: str | None = None
    jwt: str | None = None
    signature_algorithm: str = "SHA256_WITH_RSA"
    signature_type: str = "DER"
    key_password: str | None = None
    timeout: float = 15.0

    def endpoint(self) -> str:
        """`url` is a base, and the caller may or may not have included /v1."""
        base = self.url.rstrip("/")
        if base.endswith("/synchronousSign"):
            return base
        if base.endswith("/v1"):
            return f"{base}/synchronousSign"
        return f"{base}/v1/synchronousSign"

    def headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            h["X-API-KEY"] = self.api_key
        if self.jwt:
            h["Authorization"] = f"Bearer {self.jwt}"
        return h


def build_request(cfg: TsbConfig, payload: bytes) -> dict:
    """The signRequest body. Payload is base64 of the raw bytes to be signed."""
    req: dict = {
        "payload": base64.b64encode(payload).decode("ascii"),
        "payloadType": PAYLOAD_TYPE,
        "signKeyName": cfg.key_name,
        "signatureAlgorithm": cfg.signature_algorithm,
        "signatureType": cfg.signature_type,
    }
    if cfg.key_password:
        req["keyPassword"] = cfg.key_password
    return {"signRequest": req}


def _parse_error(status: int, body: bytes) -> TsbError:
    """TSB reports failures as JSON with errorCode/reason/message.

    Observed against the engineering demo:
      404 res.error.key.not.existent  — signKeyName is wrong
      500 res.error.in.hsm            — KEY_FUNCTION_NOT_PERMITTED (key cannot sign)
      500 res.error.in.hsm            — "The provided approvals are insufficient"
                                        (the key needs an approval policy, so it
                                        cannot be used for synchronous signing)
    """
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return TsbError(f"HTTP {status}: {body[:200]!r}", status=status)
    msg = data.get("message") or data.get("reason") or f"HTTP {status}"
    return TsbError(msg, status=status, reason=data.get("reason"))


def sign(cfg: TsbConfig, payload: bytes) -> str:
    """Sign `payload` on the HSM. Returns the base64 signature.

    Raises TsbError on any non-signature outcome, so a caller never mistakes a
    failure for an attestation.
    """
    body = json.dumps(build_request(cfg, payload)).encode("utf-8")
    req = urllib.request.Request(cfg.endpoint(), data=body, headers=cfg.headers(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise _parse_error(exc.code, exc.read()) from None
    except urllib.error.URLError as exc:
        raise TsbError(f"cannot reach TSB at {cfg.endpoint()}: {exc.reason}") from None

    signature = data.get("signature")
    if not signature:
        # A 200 with no signature is not success. Say so rather than storing
        # an empty attestation that verifies against nothing.
        raise TsbError(f"TSB returned no signature: {json.dumps(data)[:200]}", status=200)
    return signature
