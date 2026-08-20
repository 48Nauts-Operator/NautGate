import base64
import copy
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app import cli
from app.audit_checkpoint import build_checkpoint
from app.audit_evidence import receipt_hash
from app.audit_verify import VerificationError, key_fingerprint, verify_bundle
from app.db import queries
from app.routes import v1


def _bundle():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    receipts = [
        {
            "schema": "dev.nautgate.decision-receipt/v1",
            "receipt_id": f"00000000-0000-7000-8000-{index:012d}",
            "decision_id": f"10000000-0000-7000-8000-{index:012d}",
            "sequence": index,
        }
        for index in range(10, 13)
    ]
    now = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
    rows = [
        {
            "evidence_sequence": receipt["sequence"],
            "receipt_hash": receipt_hash(receipt),
            "created_at": now + timedelta(seconds=index),
        }
        for index, receipt in enumerate(receipts)
    ]
    checkpoint, payload, _, proofs = build_checkpoint(
        rows, instance_id="test-instance", signing_key_id="test-key-v1"
    )
    signature = key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
    public_key = key.public_key()
    return (
        {
            "bundle_schema": "dev.nautgate.evidence-bundle/v1",
            "receipt": receipts[1],
            "receipt_hash": rows[1]["receipt_hash"].hex(),
            "leaf_index": 1,
            "merkle_proof": proofs[1],
            "checkpoint": checkpoint,
            "signature": {
                "algorithm": "SHA256_WITH_RSA",
                "encoding": "base64-der",
                "value": base64.b64encode(signature).decode(),
                "key_id": "test-key-v1",
                "public_key_fingerprint": key_fingerprint(public_key),
            },
        },
        public_key,
    )


def test_bundle_verifies_receipt_inclusion_and_checkpoint_signature():
    bundle, public_key = _bundle()
    report = verify_bundle(
        bundle,
        public_key,
        expected_key_id="test-key-v1",
        expected_fingerprint=key_fingerprint(public_key),
    )
    assert report.verified is True
    assert report.evidence_sequence == 11
    assert report.receipt_id == bundle["receipt"]["receipt_id"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda b: b["receipt"].update({"sequence": 12}), "content hash"),
        (lambda b: b["merkle_proof"][0].update({"hash": "00" * 32}), "inclusion proof"),
        (lambda b: b["checkpoint"].update({"merkle_root": "00" * 32}), "inclusion proof"),
        (lambda b: b["signature"].update({"value": base64.b64encode(b"bad").decode()}), "signature"),
        (lambda b: b["signature"].update({"key_id": "other"}), "checkpoint key"),
        (lambda b: b.update({"leaf_index": 3}), "leaf index"),
    ],
)
def test_every_bound_layer_fails_closed_when_mutated(mutation, message):
    bundle, public_key = _bundle()
    changed = copy.deepcopy(bundle)
    mutation(changed)
    with pytest.raises(VerificationError, match=message):
        verify_bundle(changed, public_key)


def test_untrusted_public_key_is_rejected():
    bundle, _ = _bundle()
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    with pytest.raises(VerificationError, match="fingerprint"):
        verify_bundle(bundle, other)


def test_receipt_verify_cli_supports_machine_readable_output(tmp_path, capsys):
    bundle, public_key = _bundle()
    bundle_path = tmp_path / "evidence.json"
    key_path = tmp_path / "public.pem"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    key_path.write_bytes(
        public_key.public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    assert cli.main(
        ["receipt", "verify", str(bundle_path), "--public-key", str(key_path), "--json"]
    ) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["verified"] is True
    assert result["evidence_sequence"] == 11


def test_receipt_verify_cli_returns_distinct_failure_status(tmp_path, capsys):
    bundle, public_key = _bundle()
    bundle["receipt"]["sequence"] = 999
    bundle_path = tmp_path / "evidence.json"
    key_path = tmp_path / "public.pem"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    key_path.write_bytes(
        public_key.public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    assert cli.main(
        ["receipt", "verify", str(bundle_path), "--public-key", str(key_path), "--json"]
    ) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["verified"] is False


class _Acquire:
    def __init__(self, row):
        self.row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def fetchrow(self, sql, receipt_id, agent_id):
        self.call = (sql, receipt_id, agent_id)
        return self.row


class _Pool:
    def __init__(self, row):
        self.connection = _Acquire(row)

    def acquire(self):
        return self.connection


@pytest.mark.asyncio
async def test_export_bundle_is_verified_only_and_agent_scoped():
    bundle, _ = _bundle()
    row = {
        "canonical_receipt": json.dumps(bundle["receipt"]),
        "receipt_hash": bytes.fromhex(bundle["receipt_hash"]),
        "merkle_leaf_index": bundle["leaf_index"],
        "merkle_proof": json.dumps(bundle["merkle_proof"]),
        "canonical_checkpoint": json.dumps(bundle["checkpoint"]),
        "algorithm": bundle["signature"]["algorithm"],
        "signature": bundle["signature"]["value"],
        "key_id": bundle["signature"]["key_id"],
        "public_key_fingerprint": bundle["signature"]["public_key_fingerprint"],
    }
    pool = _Pool(row)
    exported = await queries.export_evidence_bundle(
        pool, receipt_id=bundle["receipt"]["receipt_id"], agent_id="agent-a"
    )
    assert exported == bundle
    sql, receipt_id, agent_id = pool.connection.call
    assert "r.status = 'verified'" in sql and "c.status = 'verified'" in sql
    assert receipt_id == UUID(bundle["receipt"]["receipt_id"])
    assert agent_id == "agent-a"


@pytest.mark.asyncio
async def test_export_endpoint_authenticates_and_returns_bundle(monkeypatch):
    bundle, _ = _bundle()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=object())))
    seen = {}

    async def fake_authenticate(pool, received_request):
        assert pool is request.app.state.db
        assert received_request is request
        return "agent-a"

    async def fake_export(pool, *, receipt_id, agent_id):
        seen.update(receipt_id=receipt_id, agent_id=agent_id)
        return bundle

    monkeypatch.setattr(v1, "authenticate", fake_authenticate)
    monkeypatch.setattr(v1.queries, "export_evidence_bundle", fake_export)
    response = await v1.audit_evidence_bundle(bundle["receipt"]["receipt_id"], request)
    assert json.loads(response.body) == bundle
    assert seen == {
        "receipt_id": UUID(bundle["receipt"]["receipt_id"]),
        "agent_id": "agent-a",
    }
