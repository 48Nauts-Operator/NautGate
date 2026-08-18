#!/usr/bin/env python3
"""One live signature against a real TSB. Opt-in only.

    SB_ATTEST_LIVE=1 SB_ATTEST_TSB_URL=... SB_ATTEST_KEY_NAME=... python scripts/live_smoke.py

Deliberately not a pytest: the demo TSB is access someone lent us, and a check
that runs on every `pytest` invocation would hammer it. Signing creates no
state on their side, so this leaves nothing behind to clean up.
"""

import hashlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
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
digest = hashlib.sha256(b"nautgate-chain-head-test").digest()
try:
    signature = sign(cfg, digest)
except TsbError as exc:
    print(f"FAILED ({exc.status} {exc.reason}): {exc}")
    raise SystemExit(1) from None
print(f"key={cfg.key_name} digest={digest.hex()}")
print(f"signature[{len(signature)}]={signature[:60]}...")
