"""Offline verification for portable NautGate Evidence Bundle v1 files."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.audit_evidence import (
    BUNDLE_SCHEMA,
    CHECKPOINT_SCHEMA,
    RECEIPT_SCHEMA,
    checkpoint_payload,
    receipt_hash,
    verify_merkle_proof,
)


class VerificationError(ValueError):
    pass


@dataclass(frozen=True)
class VerificationReport:
    verified: bool
    receipt_id: str
    decision_id: str
    checkpoint_id: str
    key_id: str
    public_key_fingerprint: str
    evidence_sequence: int
    claim: str

    def to_dict(self) -> dict:
        return asdict(self)


def load_verification_key(path_or_pem: str):
    value = path_or_pem
    path = Path(path_or_pem)
    if "BEGIN " not in value:
        try:
            value = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise VerificationError(f"cannot read verification key: {exc}") from exc
    raw = value.encode()
    try:
        if "BEGIN CERTIFICATE" in value:
            return x509.load_pem_x509_certificate(raw).public_key()
        return serialization.load_pem_public_key(raw)
    except ValueError as exc:
        raise VerificationError("invalid PEM public key or certificate") from exc


def key_fingerprint(public_key) -> str:
    der = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(der).hexdigest()


def verify_bundle(
    bundle: dict,
    public_key,
    *,
    expected_key_id: str | None = None,
    expected_fingerprint: str | None = None,
) -> VerificationReport:
    if bundle.get("bundle_schema") != BUNDLE_SCHEMA:
        raise VerificationError("unsupported evidence bundle schema")
    receipt = bundle.get("receipt")
    checkpoint = bundle.get("checkpoint")
    signature = bundle.get("signature")
    if not all(isinstance(value, dict) for value in (receipt, checkpoint, signature)):
        raise VerificationError("bundle is missing receipt, checkpoint, or signature")
    if receipt.get("schema") != RECEIPT_SCHEMA:
        raise VerificationError("unsupported receipt schema")
    if checkpoint.get("schema") != CHECKPOINT_SCHEMA:
        raise VerificationError("unsupported checkpoint schema")

    calculated_receipt_hash = receipt_hash(receipt)
    if bundle.get("receipt_hash") != calculated_receipt_hash.hex():
        raise VerificationError("receipt content hash mismatch")
    proof = bundle.get("merkle_proof")
    if not isinstance(proof, list):
        raise VerificationError("Merkle proof must be an array")
    calculated_root = verify_merkle_proof(calculated_receipt_hash, proof).hex()
    if calculated_root != checkpoint.get("merkle_root"):
        raise VerificationError("Merkle inclusion proof does not reach checkpoint root")

    leaf_index = bundle.get("leaf_index")
    sequence = receipt.get("sequence")
    if not isinstance(leaf_index, int) or leaf_index < 0:
        raise VerificationError("invalid Merkle leaf index")
    if sequence != checkpoint.get("first_sequence", 0) + leaf_index:
        raise VerificationError("receipt sequence and Merkle leaf index disagree")
    if leaf_index >= checkpoint.get("receipt_count", 0):
        raise VerificationError("Merkle leaf index is outside the checkpoint")
    if checkpoint.get("receipt_count") != (
        checkpoint.get("last_sequence", 0) - checkpoint.get("first_sequence", 0) + 1
    ):
        raise VerificationError("checkpoint range and receipt count disagree")

    key_id = signature.get("key_id")
    if key_id != checkpoint.get("signing_key_id"):
        raise VerificationError("signature key does not match checkpoint key")
    if expected_key_id and key_id != expected_key_id:
        raise VerificationError("bundle was signed by an unexpected key")
    if signature.get("algorithm") != "SHA256_WITH_RSA" or signature.get("encoding") != "base64-der":
        raise VerificationError("unsupported signature algorithm or encoding")
    if not isinstance(public_key, rsa.RSAPublicKey):
        raise VerificationError("checkpoint v1 requires an RSA public key")
    fingerprint = key_fingerprint(public_key)
    if signature.get("public_key_fingerprint") != fingerprint:
        raise VerificationError("bundle fingerprint does not match verification key")
    if expected_fingerprint and fingerprint != expected_fingerprint:
        raise VerificationError("verification key fingerprint is not trusted")
    try:
        signature_bytes = base64.b64decode(signature["value"], validate=True)
        public_key.verify(
            signature_bytes,
            checkpoint_payload(checkpoint),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except (KeyError, TypeError, ValueError, InvalidSignature) as exc:
        raise VerificationError("checkpoint signature is invalid") from exc

    return VerificationReport(
        verified=True,
        receipt_id=str(receipt.get("receipt_id")),
        decision_id=str(receipt.get("decision_id")),
        checkpoint_id=str(checkpoint.get("checkpoint_id")),
        key_id=str(key_id),
        public_key_fingerprint=fingerprint,
        evidence_sequence=int(sequence),
        claim=(
            "The disclosed NautGate decision receipt is included in the "
            "hardware-signed checkpoint and has not been modified."
        ),
    )


def verify_bundle_file(
    bundle_path: str | Path,
    public_key_path_or_pem: str,
    **kwargs,
) -> VerificationReport:
    try:
        bundle = json.loads(Path(bundle_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError(f"cannot read evidence bundle: {exc}") from exc
    return verify_bundle(bundle, load_verification_key(public_key_path_or_pem), **kwargs)
