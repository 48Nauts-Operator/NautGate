"""Crash-safe staging worker for Verified Audit Trail Merkle checkpoints."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog

from app.audit_checkpoint import EvidenceGapError, build_checkpoint
from app.audit_evidence import canonical_json

log = structlog.get_logger()


@dataclass(frozen=True)
class StageResult:
    status: str
    checkpoint_id: str | None = None
    receipt_count: int = 0
    expected_sequence: int | None = None
    observed_sequence: int | None = None


async def stage_checkpoint_once(
    pool,
    *,
    instance_id: str,
    signing_key_id: str,
    max_receipts: int = 1000,
    max_age_seconds: float = 60.0,
    force: bool = False,
) -> StageResult:
    """Atomically consume one contiguous outbox range into a checkpoint."""
    max_receipts = max(1, min(int(max_receipts), 10_000))
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Only one local/replicated worker may choose the next range.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext('nautgate.audit_checkpoint'))"
            )
            pending = await conn.fetchrow(
                """
                SELECT COUNT(*)::int AS count, MIN(r.created_at) AS oldest
                  FROM nautgate.audit_outbox o
                  JOIN nautgate.audit_receipts r USING (receipt_id)
                """
            )
            if not pending or not pending["count"]:
                return StageResult(status="empty")
            if not force and pending["count"] < max_receipts:
                age = datetime.now(UTC) - pending["oldest"]
                if age < timedelta(seconds=max_age_seconds):
                    return StageResult(status="waiting")

            previous = await conn.fetchrow(
                """
                SELECT last_sequence, checkpoint_hash
                  FROM nautgate.audit_checkpoints
                 WHERE instance_id = $1 AND status <> 'failed'
                 ORDER BY last_sequence DESC
                 LIMIT 1
                """,
                instance_id,
            )
            rows = await conn.fetch(
                """
                SELECT r.receipt_id, r.evidence_sequence, r.receipt_hash, r.created_at
                  FROM nautgate.audit_outbox o
                  JOIN nautgate.audit_receipts r USING (receipt_id)
                 ORDER BY r.evidence_sequence
                 LIMIT $1
                 FOR UPDATE OF o, r
                """,
                max_receipts,
            )
            if not rows:
                return StageResult(status="empty")
            material = [dict(row) for row in rows]
            expected_first = int(previous["last_sequence"]) + 1 if previous else 1
            observed_first = int(material[0]["evidence_sequence"])
            if observed_first != expected_first:
                await _record_gap(conn, expected_first, observed_first)
                return StageResult(
                    status="gap",
                    expected_sequence=expected_first,
                    observed_sequence=observed_first,
                )
            try:
                checkpoint, payload, checkpoint_hash, proofs = build_checkpoint(
                    material,
                    instance_id=instance_id,
                    signing_key_id=signing_key_id,
                    previous_checkpoint_hash=(
                        bytes(previous["checkpoint_hash"]) if previous else None
                    ),
                )
            except EvidenceGapError as exc:
                await _record_gap(conn, exc.expected, exc.observed)
                return StageResult(
                    status="gap",
                    expected_sequence=exc.expected,
                    observed_sequence=exc.observed,
                )

            checkpoint_id = checkpoint["checkpoint_id"]
            root = bytes.fromhex(checkpoint["merkle_root"])
            previous_hash = (
                bytes.fromhex(checkpoint["previous_checkpoint_sha256"])
                if checkpoint["previous_checkpoint_sha256"]
                else None
            )
            await conn.execute(
                """
                INSERT INTO nautgate.audit_checkpoints
                    (checkpoint_id, schema_version, instance_id,
                     first_sequence, last_sequence, receipt_count, merkle_root,
                     canonical_checkpoint, canonical_bytes, checkpoint_hash,
                     previous_checkpoint_hash, key_id, opened_at, closed_at)
                VALUES ($1::uuid, $2, $3, $4, $5, $6, $7,
                        $8::jsonb, $9, $10, $11, $12, $13, $14)
                ON CONFLICT (checkpoint_id) DO NOTHING
                """,
                checkpoint_id,
                checkpoint["schema"],
                instance_id,
                checkpoint["first_sequence"],
                checkpoint["last_sequence"],
                checkpoint["receipt_count"],
                root,
                json.dumps(checkpoint, ensure_ascii=False, separators=(",", ":")),
                canonical_json(checkpoint),
                checkpoint_hash,
                previous_hash,
                signing_key_id,
                checkpoint["opened_at"],
                checkpoint["closed_at"],
            )
            for index, (row, proof) in enumerate(zip(material, proofs, strict=True)):
                await conn.execute(
                    """
                    UPDATE nautgate.audit_receipts
                       SET status = 'batched', checkpoint_id = $2::uuid,
                           merkle_leaf_index = $3, merkle_proof = $4::jsonb
                     WHERE receipt_id = $1
                    """,
                    row["receipt_id"],
                    checkpoint_id,
                    index,
                    json.dumps(proof, separators=(",", ":")),
                )
            await conn.execute(
                "DELETE FROM nautgate.audit_outbox WHERE receipt_id = ANY($1::uuid[])",
                [row["receipt_id"] for row in material],
            )
            return StageResult(
                status="staged",
                checkpoint_id=checkpoint_id,
                receipt_count=len(material),
            )


async def _record_gap(conn, expected: int, observed: int) -> None:
    await conn.execute(
        """
        INSERT INTO nautgate.audit_gaps (expected_sequence, observed_sequence)
        VALUES ($1, $2)
        ON CONFLICT DO NOTHING
        """,
        expected,
        observed,
    )
    await conn.execute(
        """
        UPDATE nautgate.audit_receipts
           SET status = 'gap'
         WHERE evidence_sequence >= $1 AND evidence_sequence < $2
        """,
        expected,
        observed,
    )


async def run_scheduler(
    pool,
    *,
    instance_id: str,
    signing_key_id: str,
    max_receipts: int = 1000,
    max_age_seconds: float = 60.0,
    tick_seconds: float = 5.0,
) -> None:
    log.info("audit_checkpoint_scheduler_started", instance_id=instance_id)
    while True:
        try:
            result = await stage_checkpoint_once(
                pool,
                instance_id=instance_id,
                signing_key_id=signing_key_id,
                max_receipts=max_receipts,
                max_age_seconds=max_age_seconds,
            )
            if result.status not in ("empty", "waiting"):
                log.info("audit_checkpoint_stage", **result.__dict__)
        except asyncio.CancelledError:
            log.info("audit_checkpoint_scheduler_cancelled")
            raise
        except Exception as exc:
            log.error(
                "audit_checkpoint_iteration_failed",
                error=str(exc) or repr(exc),
                error_type=type(exc).__name__,
            )
        await asyncio.sleep(tick_seconds)
