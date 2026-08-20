import base64
import copy
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from evidence import CHECKPOINT_DOMAIN, CheckpointError, checkpoint_payload
from main import public_key_fingerprint, verify_signature


def _checkpoint():
    return {
        "schema": "dev.nautgate.audit-checkpoint/v1",
        "checkpoint_id": "00000000-0000-7000-8000-000000000010",
        "instance_id": "nautgate-test",
        "first_sequence": 1,
        "last_sequence": 3,
        "receipt_count": 3,
        "merkle_algorithm": "sha256-binary-v1",
        "merkle_root": "ab" * 32,
        "opened_at": "2026-08-20T10:11:00.000000Z",
        "closed_at": "2026-08-20T10:12:00.000000Z",
        "previous_checkpoint_sha256": None,
        "signing_key_id": "nautgate-attestation-v1",
    }


def test_checkpoint_payload_is_domain_separated_and_deterministic():
    checkpoint = _checkpoint()
    payload = checkpoint_payload(checkpoint, expected_key="nautgate-attestation-v1")
    assert payload.startswith(CHECKPOINT_DOMAIN)
    reordered = dict(reversed(list(checkpoint.items())))
    assert checkpoint_payload(reordered, expected_key="nautgate-attestation-v1") == payload


def test_core_and_sidecar_share_the_published_checkpoint_vector():
    fixture = Path(__file__).parents[3] / "docs" / "fixtures" / "audit-v1" / "vectors.json"
    vector = json.loads(fixture.read_text(encoding="utf-8"))["checkpoint"]
    assert (
        checkpoint_payload(vector["value"], expected_key="nautgate-attestation-v1").hex()
        == vector["payload_hex"]
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"schema": "dev.nautgate.audit-checkpoint/v2"},
        {"signing_key_id": "wrong-key"},
        {"receipt_count": 2},
        {"merkle_root": "not-a-hash"},
        {"extra": True},
    ],
)
def test_checkpoint_contract_fails_closed(mutation):
    checkpoint = copy.deepcopy(_checkpoint())
    checkpoint.update(mutation)
    with pytest.raises(CheckpointError):
        checkpoint_payload(checkpoint, expected_key="nautgate-attestation-v1")


def test_rsa_signature_is_verified_before_receipt_is_accepted():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    payload = checkpoint_payload(_checkpoint(), expected_key="nautgate-attestation-v1")
    signature = private_key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
    encoded = base64.b64encode(signature).decode()
    verify_signature(public_key, payload, encoded, "SHA256_WITH_RSA")
    assert len(public_key_fingerprint(public_key)) == 64
    with pytest.raises(ValueError, match="invalid"):
        verify_signature(public_key, payload + b"tampered", encoded, "SHA256_WITH_RSA")
