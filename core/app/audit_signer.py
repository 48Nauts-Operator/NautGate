"""Submit staged checkpoints to the typed sb-attest signing endpoint."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from dataclasses import dataclass

import httpx
import structlog

from app.audit_evidence import checkpoint_payload

log = structlog.get_logger()


@dataclass(frozen=True)
class SignResult:
    status: str
    checkpoint_id: str | None = None
    error: str | None = None


async def sign_checkpoint_once(
    pool,
    *,
    sidecar_url: str,
    internal_token: str,
    expected_key_id: str,
    expected_fingerprint: str | None = None,
    timeout_seconds: float = 20.0,
    max_attempts: int = 10,
    client: httpx.AsyncClient | None = None,
) -> SignResult:
    """Claim, submit, and atomically finalize one staged checkpoint."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            locked = await conn.fetchval(
                "SELECT pg_try_advisory_xact_lock(hashtext('nautgate.audit_signer'))"
            )
            if not locked:
                return SignResult(status="busy")
            row = await conn.fetchrow(
                """
                SELECT checkpoint_id::text, canonical_checkpoint, checkpoint_hash,
                       key_id, attempt_count
                  FROM nautgate.audit_checkpoints
                 WHERE status = 'signing'
                 ORDER BY first_sequence
                 LIMIT 1
                 FOR UPDATE
                """
            )
            if row is None:
                return SignResult(status="empty")
            checkpoint_id = row["checkpoint_id"]
            checkpoint = row["canonical_checkpoint"]
            if isinstance(checkpoint, str):
                checkpoint = json.loads(checkpoint)
            expected_hash = hashlib.sha256(checkpoint_payload(checkpoint)).hexdigest()
            if bytes(row["checkpoint_hash"]).hex() != expected_hash:
                return await _fail(
                    conn,
                    checkpoint_id,
                    int(row["attempt_count"]),
                    max_attempts,
                    "stored checkpoint hash mismatch",
                )
            if row["key_id"] != expected_key_id:
                return await _fail(
                    conn,
                    checkpoint_id,
                    int(row["attempt_count"]),
                    max_attempts,
                    "checkpoint key does not match configured signing key",
                )

            owned_client = client is None
            http = client or httpx.AsyncClient(timeout=timeout_seconds)
            try:
                response = await http.post(
                    f"{sidecar_url.rstrip('/')}/v1/attest/checkpoint",
                    json={"checkpoint": checkpoint},
                    headers={"X-NautGate-Attest-Token": internal_token},
                )
                response.raise_for_status()
                result = response.json()
                _validate_response(
                    result,
                    checkpoint_id=checkpoint_id,
                    checkpoint_hash=expected_hash,
                    expected_key_id=expected_key_id,
                    expected_fingerprint=expected_fingerprint,
                )
            except Exception as exc:
                return await _fail(
                    conn,
                    checkpoint_id,
                    int(row["attempt_count"]),
                    max_attempts,
                    str(exc) or type(exc).__name__,
                )
            finally:
                if owned_client:
                    await http.aclose()

            await conn.execute(
                """
                UPDATE nautgate.audit_checkpoints
                   SET status = 'verified', signature = $2,
                       algorithm = $3, public_key_fingerprint = $4,
                       signed_at = NOW(), attempt_count = attempt_count + 1,
                       last_error = NULL
                 WHERE checkpoint_id = $1::uuid
                """,
                checkpoint_id,
                result["signature"],
                result["algorithm"],
                result["public_key_fingerprint"],
            )
            await conn.execute(
                """
                UPDATE nautgate.audit_receipts
                   SET status = 'verified'
                 WHERE checkpoint_id = $1::uuid
                """,
                checkpoint_id,
            )
            return SignResult(status="verified", checkpoint_id=checkpoint_id)


def _validate_response(
    result: dict,
    *,
    checkpoint_id: str,
    checkpoint_hash: str,
    expected_key_id: str,
    expected_fingerprint: str | None,
) -> None:
    if result.get("verified") is not True:
        raise ValueError("sidecar did not verify the TSB signature")
    if result.get("checkpoint_id") != checkpoint_id:
        raise ValueError("sidecar returned a different checkpoint")
    if result.get("checkpoint_hash") != checkpoint_hash:
        raise ValueError("sidecar signed different checkpoint bytes")
    if result.get("key_id") != expected_key_id:
        raise ValueError("sidecar used a different signing key")
    if result.get("algorithm") != "SHA256_WITH_RSA" or result.get("encoding") != "base64-der":
        raise ValueError("sidecar returned an unsupported signature format")
    if expected_fingerprint and result.get("public_key_fingerprint") != expected_fingerprint:
        raise ValueError("sidecar public key fingerprint changed")
    try:
        signature = base64.b64decode(result["signature"], validate=True)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("sidecar returned malformed signature bytes") from exc
    if not signature:
        raise ValueError("sidecar returned an empty signature")


async def _fail(conn, checkpoint_id: str, attempts: int, max_attempts: int, error: str):
    terminal = attempts + 1 >= max_attempts
    await conn.execute(
        """
        UPDATE nautgate.audit_checkpoints
           SET status = $2, attempt_count = attempt_count + 1,
               last_error = $3
         WHERE checkpoint_id = $1::uuid
        """,
        checkpoint_id,
        "failed" if terminal else "signing",
        error[:1000],
    )
    return SignResult(
        status="failed" if terminal else "retry", checkpoint_id=checkpoint_id, error=error
    )


async def run_scheduler(pool, **kwargs) -> None:
    tick_seconds = float(kwargs.pop("tick_seconds", 5.0))
    log.info("audit_signer_scheduler_started", sidecar_url=kwargs.get("sidecar_url"))
    while True:
        try:
            result = await sign_checkpoint_once(pool, **kwargs)
            if result.status not in ("empty", "busy"):
                log.info("audit_checkpoint_sign", **result.__dict__)
        except asyncio.CancelledError:
            log.info("audit_signer_scheduler_cancelled")
            raise
        except Exception as exc:
            log.error(
                "audit_signer_iteration_failed",
                error=str(exc) or repr(exc),
                error_type=type(exc).__name__,
            )
        await asyncio.sleep(tick_seconds)
