#!/usr/bin/env python3
"""One typed checkpoint signature against a real TSB. Opt-in only.

    SB_ATTEST_LIVE=1 SB_ATTEST_TSB_URL=... SB_ATTEST_KEY_NAME=... python scripts/live_smoke.py

Deliberately not a pytest: the demo TSB is access someone lent us, and a check
that runs on every `pytest` invocation would hammer it. Signing creates no
state on their side, so this leaves nothing behind to clean up.
"""

import base64
import json
import os
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from evidence import checkpoint_payload  # noqa: E402
from tsb import TsbConfig, TsbError, sign  # noqa: E402

if os.getenv("SB_ATTEST_LIVE") != "1":
    print("set SB_ATTEST_LIVE=1 to call the HSM; skipping")
    raise SystemExit(0)

cfg = TsbConfig(
    url=os.environ["SB_ATTEST_TSB_URL"],
    key_name=os.environ["SB_ATTEST_KEY_NAME"],
    api_key=os.getenv("SB_ATTEST_API_KEY") or None,
    jwt=os.getenv("SB_ATTEST_JWT") or None,
)
checkpoint = {
    "schema": "dev.nautgate.audit-checkpoint/v1",
    "checkpoint_id": "00000000-0000-7000-8000-000000000075",
    "instance_id": "live-smoke",
    "first_sequence": 1,
    "last_sequence": 1,
    "receipt_count": 1,
    "merkle_algorithm": "sha256-binary-v1",
    "merkle_root": "75" * 32,
    "opened_at": "2026-08-20T00:00:00.000000Z",
    "closed_at": "2026-08-20T00:00:00.000000Z",
    "previous_checkpoint_sha256": None,
    "signing_key_id": cfg.key_name,
}
payload = checkpoint_payload(checkpoint, expected_key=cfg.key_name)
try:
    signature = sign(cfg, payload)
except TsbError as exc:
    print(f"FAILED ({exc.status} {exc.reason}): {exc}")
    raise SystemExit(1) from None

public_value = os.getenv("SB_ATTEST_PUBLIC_KEY_PEM", "").replace("\\n", "\n").strip()
public_path = os.getenv("SB_ATTEST_PUBLIC_KEY_PATH", "").strip()
if public_path:
    public_value = Path(public_path).read_text(encoding="utf-8").strip()
if not public_value:
    print("FAILED: SB_ATTEST_PUBLIC_KEY_PEM or SB_ATTEST_PUBLIC_KEY_PATH is required")
    raise SystemExit(1)
raw = public_value.encode()
public_key = (
    x509.load_pem_x509_certificate(raw).public_key()
    if "BEGIN CERTIFICATE" in public_value
    else serialization.load_pem_public_key(raw)
)
try:
    public_key.verify(
        base64.b64decode(signature, validate=True), payload, padding.PKCS1v15(), hashes.SHA256()
    )
except Exception as exc:
    print(f"FAILED: TSB signature did not verify locally: {exc}")
    raise SystemExit(1) from None
print(
    json.dumps(
        {"verified": True, "key_id": cfg.key_name, "checkpoint_id": checkpoint["checkpoint_id"]}
    )
)
