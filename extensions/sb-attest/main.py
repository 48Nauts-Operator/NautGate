"""sb-attest — hardware attestation for NautGate.

NautGate already logs every decision into a SHA-256 hash chain
(`nautgate.privacy_log`, see the sb-privacy extension). A chain proves nobody
EDITED a row: change one and every hash after it stops matching. It does not
prove nobody REWROTE the whole thing from genesis, which anyone with write
access to the database can do. That gap is the reason this extension exists.

A signature from a Securosys Primus HSM closes it. The key's private half
cannot leave the hardware, so a signature over a digest is evidence a third
party can check without trusting us or our database. Securosys returns it as
an attestation receipt; we store the receipt.

ONE key, many subjects. The same configured key signs whatever we hand it:
an agent's finished work in xNAUT, the route a NautGate request took, the
current head of the privacy chain. The subject is a label on the receipt, not
a reason for another key.

Sign the head, not each row. One signature over the current chain head covers
every row before it, because the chain already binds them. Signing per request
would put an HSM round trip on the hot path and bill for it, and it would
prove nothing more.

Config (env):
  SB_ATTEST_TSB_URL     required   Securosys TSB REST base, e.g. https://.../tsb-demo
  SB_ATTEST_KEY_NAME    required   the signing key's label in TSB
  SB_ATTEST_API_KEY     optional   sent as X-API-KEY
  SB_ATTEST_JWT         optional   sent as Authorization: Bearer
  SB_ATTEST_ALGORITHM   optional   default SHA256_WITH_RSA
  SB_ATTEST_DB_URL      required   postgres, same database as sb-privacy
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager

import asyncpg
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

import migrate
from evidence import CheckpointError, checkpoint_payload
from tsb import TsbConfig, TsbError, sign

logging.basicConfig(level=os.getenv("SB_ATTEST_LOG_LEVEL", "INFO"))
log = logging.getLogger("sb-attest")

# The label used for receipts over the privacy chain's head, so a verifier can
# find them without guessing.
CHAIN_SUBJECT = "nautgate.privacy_log"
CHECKPOINT_SUBJECT = "nautgate.audit_checkpoint"


def _truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def config_from_env() -> TsbConfig:
    url = os.getenv("SB_ATTEST_TSB_URL", "").strip()
    key = os.getenv("SB_ATTEST_KEY_NAME", "").strip()
    if not url or not key:
        raise RuntimeError("SB_ATTEST_TSB_URL and SB_ATTEST_KEY_NAME are required")
    api_key = os.getenv("SB_ATTEST_API_KEY", "").strip() or None
    jwt = os.getenv("SB_ATTEST_JWT", "").strip() or None
    if not api_key and not jwt:
        # Not fatal: some TSB deployments are reachable without one. Say it
        # loudly rather than letting an unauthenticated production run pass
        # unnoticed.
        log.warning("no SB_ATTEST_API_KEY and no SB_ATTEST_JWT: calling TSB unauthenticated")
    if _truthy("SB_ATTEST_PRODUCTION") and not (api_key or jwt):
        raise RuntimeError("production mode requires SB_ATTEST_API_KEY or SB_ATTEST_JWT")
    return TsbConfig(
        url=url,
        key_name=key,
        api_key=api_key,
        jwt=jwt,
        signature_algorithm=os.getenv("SB_ATTEST_ALGORITHM", "SHA256_WITH_RSA").strip(),
        key_password=os.getenv("SB_ATTEST_KEY_PASSWORD", "").strip() or None,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.tsb = config_from_env()
    dsn = os.getenv("SB_ATTEST_DB_URL", "").strip()
    if not dsn:
        raise RuntimeError("SB_ATTEST_DB_URL is required")
    app.state.internal_token = os.getenv("SB_ATTEST_INTERNAL_TOKEN", "").strip() or None
    if _truthy("SB_ATTEST_PRODUCTION") and not app.state.internal_token:
        raise RuntimeError("production mode requires SB_ATTEST_INTERNAL_TOKEN")
    app.state.public_key = load_public_key()
    if _truthy("SB_ATTEST_PRODUCTION") and app.state.public_key is None:
        raise RuntimeError("production mode requires SB_ATTEST_PUBLIC_KEY_PATH or PEM")
    app.state.pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    await migrate.apply(app.state.pool)
    log.info("sb-attest ready: key=%s tsb=%s", app.state.tsb.key_name, app.state.tsb.url)
    try:
        yield
    finally:
        await app.state.pool.close()


app = FastAPI(title="sb-attest", version="0.1.0", lifespan=lifespan)


class AttestRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    digest: str = Field(min_length=2, max_length=256)
    meta: dict = Field(default_factory=dict)


class CheckpointSignRequest(BaseModel):
    checkpoint: dict


def load_public_key():
    """Load the configured checkpoint verification key or certificate."""
    value = os.getenv("SB_ATTEST_PUBLIC_KEY_PEM", "").replace("\\n", "\n").strip()
    path = os.getenv("SB_ATTEST_PUBLIC_KEY_PATH", "").strip()
    if path:
        value = __import__("pathlib").Path(path).read_text(encoding="utf-8").strip()
    if not value:
        return None
    raw = value.encode()
    try:
        if "BEGIN CERTIFICATE" in value:
            return x509.load_pem_x509_certificate(raw).public_key()
        return serialization.load_pem_public_key(raw)
    except ValueError as exc:
        raise RuntimeError("invalid SB_ATTEST public key or certificate") from exc


def public_key_fingerprint(public_key) -> str:
    der = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(der).hexdigest()


def verify_signature(public_key, payload: bytes, signature_b64: str, algorithm: str) -> None:
    if algorithm != "SHA256_WITH_RSA" or not isinstance(public_key, rsa.RSAPublicKey):
        raise ValueError("checkpoint v1 requires an RSA SHA-256 verification key")
    try:
        signature = base64.b64decode(signature_b64, validate=True)
        public_key.verify(signature, payload, padding.PKCS1v15(), hashes.SHA256())
    except (ValueError, InvalidSignature) as exc:
        raise ValueError("TSB returned an invalid checkpoint signature") from exc


def _authorize_internal(request: Request) -> None:
    expected = getattr(request.app.state, "internal_token", None)
    supplied = request.headers.get("x-nautgate-attest-token", "")
    if expected and not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="invalid sidecar credential")


def _generic_allowed() -> bool:
    return _truthy("SB_ATTEST_ALLOW_GENERIC") or not _truthy("SB_ATTEST_PRODUCTION")


def digest_bytes(digest: str) -> bytes:
    """A digest arrives as hex. Reject anything else at the boundary.

    Signing the ASCII of a malformed digest would still return a signature,
    and the receipt would verify against nothing anyone can reproduce.
    """
    text = digest.strip().lower().removeprefix("0x")
    try:
        raw = binascii.unhexlify(text)
    except (binascii.Error, ValueError):
        raise ValueError("digest must be hex") from None
    if not raw:
        raise ValueError("digest is empty")
    return raw


async def attest(app: FastAPI, subject: str, digest: str, meta: dict) -> dict:
    cfg: TsbConfig = app.state.tsb
    try:
        payload = digest_bytes(digest)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    try:
        # urllib is blocking; keep the event loop free.
        signature = await asyncio.to_thread(sign, cfg, payload)
    except TsbError as exc:
        log.error("attestation failed for %s: %s", subject, exc)
        raise HTTPException(status_code=502, detail=f"TSB: {exc}") from None

    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO nautgate.attestation
                (subject, digest, key_name, algorithm, signature, tsb_url, meta)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
            RETURNING id, ts
            """,
            subject,
            digest.strip().lower(),
            cfg.key_name,
            cfg.signature_algorithm,
            signature,
            cfg.url,
            __import__("json").dumps(meta or {}),
        )
    return {
        "id": row["id"],
        "ts": row["ts"].isoformat(),
        "subject": subject,
        "digest": digest.strip().lower(),
        "key_name": cfg.key_name,
        "algorithm": cfg.signature_algorithm,
        "signature": signature,
    }


@app.get("/health")
async def health():
    return {"status": "ok", "key_name": app.state.tsb.key_name, "tsb": app.state.tsb.url}


@app.post("/v1/attest")
async def attest_digest(req: AttestRequest):
    """Sign any digest. The subject says what it is; the key is always the same."""
    if not _generic_allowed():
        raise HTTPException(status_code=404, detail="generic signing disabled")
    return await attest(app, req.subject, req.digest, req.meta)


@app.post("/v1/attest/chain-head")
async def attest_chain_head():
    """Seal the privacy log by signing its current head.

    The head is read here rather than passed in, so a receipt cannot be made
    for a head that was never in the table.
    """
    if not _generic_allowed():
        raise HTTPException(status_code=404, detail="generic signing disabled")
    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, this_hash FROM nautgate.privacy_log ORDER BY id DESC LIMIT 1"
        )
    if not row:
        raise HTTPException(status_code=404, detail="privacy_log is empty: nothing to attest")
    return await attest(app, CHAIN_SUBJECT, row["this_hash"], {"head_id": row["id"]})


@app.post("/v1/attest/checkpoint")
async def attest_checkpoint(req: CheckpointSignRequest, request: Request):
    """Validate, sign, and locally verify one canonical NautGate checkpoint."""
    _authorize_internal(request)
    cfg: TsbConfig = request.app.state.tsb
    try:
        payload = checkpoint_payload(req.checkpoint, expected_key=cfg.key_name)
    except CheckpointError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    public_key = request.app.state.public_key
    if public_key is None:
        raise HTTPException(status_code=503, detail="checkpoint verification key unavailable")
    try:
        signature = await asyncio.to_thread(sign, cfg, payload)
        verify_signature(public_key, payload, signature, cfg.signature_algorithm)
    except TsbError as exc:
        raise HTTPException(status_code=502, detail=f"TSB: {exc}") from None
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None

    digest = hashlib.sha256(payload).hexdigest()
    fingerprint = public_key_fingerprint(public_key)
    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO nautgate.attestation
                (subject, digest, key_name, algorithm, signature, tsb_url, meta)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
            RETURNING id, ts
            """,
            CHECKPOINT_SUBJECT,
            digest,
            cfg.key_name,
            cfg.signature_algorithm,
            signature,
            cfg.url,
            json.dumps(
                {
                    "checkpoint_id": req.checkpoint["checkpoint_id"],
                    "public_key_fingerprint": fingerprint,
                }
            ),
        )
    return {
        "id": row["id"],
        "ts": row["ts"].isoformat(),
        "checkpoint_id": req.checkpoint["checkpoint_id"],
        "checkpoint_hash": digest,
        "key_id": cfg.key_name,
        "algorithm": cfg.signature_algorithm,
        "encoding": "base64-der",
        "signature": signature,
        "public_key_fingerprint": fingerprint,
        "verified": True,
    }


@app.get("/v1/receipts")
async def receipts(subject: str | None = None, limit: int = 20):
    limit = max(1, min(limit, 200))
    sql = """
        SELECT id, ts, subject, digest, key_name, algorithm, signature
        FROM nautgate.attestation
    """
    args: list = []
    if subject:
        sql += " WHERE subject = $1"
        args.append(subject)
    sql += f" ORDER BY id DESC LIMIT {limit}"
    async with app.state.pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return {"receipts": [{**dict(r), "ts": r["ts"].isoformat()} for r in rows]}
