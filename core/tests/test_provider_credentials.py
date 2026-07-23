"""Provider credentials encrypt at rest and round-trip (NAUTGATE-8)."""

import os
from unittest import mock

import pytest
from cryptography.exceptions import InvalidTag

os.environ.setdefault("NAUTGATE_MASTER_KEY", "unit-test-master-key")


def test_crypto_roundtrip_and_tamper():
    from app import crypto

    ct, nonce = crypto.encrypt("sk-or-v1-abc123SECRET")
    assert crypto.decrypt(ct, nonce) == "sk-or-v1-abc123SECRET"
    assert crypto.last4("sk-or-v1-abc123SECRET") == "CRET"
    # tampered ciphertext must fail (AEAD)
    with pytest.raises(InvalidTag):
        crypto.decrypt(ct[:-1] + bytes([ct[-1] ^ 1]), nonce)


def test_missing_master_key_raises():
    from app import crypto

    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("NAUTGATE_MASTER_KEY", None)
        assert not crypto.master_key_configured()
        with pytest.raises(crypto.MasterKeyMissing):
            crypto.encrypt("x")
    os.environ["NAUTGATE_MASTER_KEY"] = "unit-test-master-key"
