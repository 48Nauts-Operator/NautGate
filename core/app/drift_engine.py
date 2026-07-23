"""DB integration for drift detection — wraps app/drift.py with persistence.

Called from the route handlers' fire-and-forget post-outcome step (alongside
``scorecard.process_brain``). Reads the just-completed decision, computes
drift metrics, updates baselines, writes anomalies, and raises/resolves
cluster alerts.
"""

from __future__ import annotations

import asyncpg
import structlog

from app.drift import (
    ALERT_RESOLUTION_SAMPLES,
    ANOMALY_CLUSTER_THRESHOLD,
    alert_direction,
    detect_compaction,
    extract_metrics,
    update_ewma,
)

log = structlog.get_logger()


async def process_drift(
    pool: asyncpg.Pool,
    *,
    decision_id,
) -> None:
    """Update baselines + anomalies + alerts for one completed decision.

    Picks the routed (decision_provider, decision_model) as the key — same
    convention as the scorecard, so the two systems align.
    """
    row = await pool.fetchrow(
        """
        SELECT d.id::text                     AS decision_id,
               d.agent_id                     AS agent_id,
               d.session_id                   AS session_id,
               d.decision_provider            AS decision_provider,
               d.decision_model               AS decision_model,
               d.messages_count               AS messages_count,
               d.request_size_bytes           AS request_size_bytes,
               o.prompt_tokens                AS prompt_tokens,
               o.response_size_bytes          AS response_size_bytes,
               o.first_byte_ms                AS first_byte_ms,
               o.duration_ms                  AS duration_ms
          FROM nautgate.route_decisions d
          LEFT JOIN nautgate.route_outcomes o ON d.id = o.decision_id
         WHERE d.id::text = $1
        """,
        str(decision_id),
    )
    if row is None:
        return

    provider = row["decision_provider"]
    model = row["decision_model"]
    if not provider or not model:
        return

    # Compute messages_count_delta against the previous turn of this session.
    messages_count_delta: int | None = None
    if row["session_id"] and row["messages_count"] is not None:
        prev = await pool.fetchrow(
            """
            SELECT messages_count
              FROM nautgate.route_decisions
             WHERE session_id = $1
               AND id::text != $2
               AND ts < (SELECT ts FROM nautgate.route_decisions WHERE id::text = $2)
             ORDER BY ts DESC
             LIMIT 1
            """,
            row["session_id"],
            str(decision_id),
        )
        if prev is not None and prev["messages_count"] is not None:
            messages_count_delta = row["messages_count"] - prev["messages_count"]
            if detect_compaction(prev["messages_count"], row["messages_count"]):
                # Force-write a compaction anomaly even before EWMA warm-up —
                # the event is the signal, not the z-score.
                await _write_anomaly(
                    pool,
                    provider=provider,
                    model=model,
                    metric_name="messages_count_delta",
                    z_score=-99.0,  # sentinel: forced flag
                    observed=float(messages_count_delta),
                    baseline_mean=0.0,
                    baseline_stddev=0.0,
                    decision_id=decision_id,
                )
                await _maybe_raise_alert(
                    pool,
                    provider=provider,
                    model=model,
                    metric_name="messages_count_delta",
                    observed=float(messages_count_delta),
                    baseline_mean=0.0,
                    z_score=-99.0,
                )

    metrics = extract_metrics(
        prompt_tokens=row["prompt_tokens"],
        request_size_bytes=row["request_size_bytes"],
        response_size_bytes=row["response_size_bytes"],
        first_byte_ms=row["first_byte_ms"],
        duration_ms=row["duration_ms"],
        messages_count_delta=messages_count_delta,
    )

    for metric_name, observed in metrics.items():
        if metric_name == "messages_count_delta":
            # Compaction handled above; skip generic z-score path so we don't
            # double-write on the same event.
            continue
        await _update_baseline_and_check(
            pool,
            provider=provider,
            model=model,
            metric_name=metric_name,
            observed=observed,
            decision_id=decision_id,
        )


async def _update_baseline_and_check(
    pool: asyncpg.Pool,
    *,
    provider: str,
    model: str,
    metric_name: str,
    observed: float,
    decision_id,
) -> None:
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """
            SELECT ewma_mean, ewma_variance, sample_count, consecutive_anomalies
              FROM nautgate.model_baselines
             WHERE provider = $1 AND model = $2 AND metric_name = $3
             FOR UPDATE
            """,
            provider,
            model,
            metric_name,
        )

        if row is None:
            update = update_ewma(
                prev_mean=0.0,
                prev_variance=0.0,
                prev_sample_count=0,
                observation=observed,
            )
            await conn.execute(
                """
                INSERT INTO nautgate.model_baselines
                    (provider, model, metric_name,
                     ewma_mean, ewma_variance, sample_count,
                     consecutive_anomalies, last_observed, last_z_score, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, 0, $7, $8, now())
                """,
                provider,
                model,
                metric_name,
                update.new_mean,
                update.new_variance,
                update.new_sample_count,
                observed,
                update.z_score,
            )
            return

        update = update_ewma(
            prev_mean=float(row["ewma_mean"]),
            prev_variance=float(row["ewma_variance"]),
            prev_sample_count=row["sample_count"],
            observation=observed,
        )

        new_consecutive = row["consecutive_anomalies"] + 1 if update.is_anomaly else 0
        await conn.execute(
            """
            UPDATE nautgate.model_baselines
               SET ewma_mean = $4, ewma_variance = $5, sample_count = $6,
                   consecutive_anomalies = $7,
                   last_observed = $8, last_z_score = $9,
                   updated_at = now()
             WHERE provider = $1 AND model = $2 AND metric_name = $3
            """,
            provider,
            model,
            metric_name,
            update.new_mean,
            update.new_variance,
            update.new_sample_count,
            new_consecutive,
            observed,
            update.z_score,
        )

    if update.is_anomaly:
        stddev = update.new_variance**0.5
        await _write_anomaly(
            pool,
            provider=provider,
            model=model,
            metric_name=metric_name,
            z_score=update.z_score or 0.0,
            observed=observed,
            baseline_mean=update.new_mean,
            baseline_stddev=stddev,
            decision_id=decision_id,
        )

    # Cluster gate: only escalate after N consecutive anomalies.
    if new_consecutive >= ANOMALY_CLUSTER_THRESHOLD:
        await _maybe_raise_alert(
            pool,
            provider=provider,
            model=model,
            metric_name=metric_name,
            observed=observed,
            baseline_mean=update.new_mean,
            z_score=update.z_score or 0.0,
        )

    if not update.is_anomaly:
        # Normal sample — nudge open alerts toward resolution.
        await _maybe_resolve_alert(
            pool,
            provider=provider,
            model=model,
            metric_name=metric_name,
        )


async def _write_anomaly(
    pool: asyncpg.Pool,
    *,
    provider: str,
    model: str,
    metric_name: str,
    z_score: float,
    observed: float,
    baseline_mean: float,
    baseline_stddev: float,
    decision_id,
) -> None:
    await pool.execute(
        """
        INSERT INTO nautgate.model_anomalies
            (provider, model, metric_name, z_score, observed_value,
             baseline_mean, baseline_stddev, decision_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        provider,
        model,
        metric_name,
        z_score,
        observed,
        baseline_mean,
        baseline_stddev,
        decision_id,
    )


async def _maybe_raise_alert(
    pool: asyncpg.Pool,
    *,
    provider: str,
    model: str,
    metric_name: str,
    observed: float,
    baseline_mean: float,
    z_score: float,
) -> None:
    """Open a drift_alerts row if there's no open one for this triple, or
    update the peak if there is."""
    direction = alert_direction(observed, baseline_mean)
    open_alert = await pool.fetchrow(
        """
        SELECT id, peak_z_score, sample_count
          FROM nautgate.drift_alerts
         WHERE provider = $1 AND model = $2 AND metric_name = $3
           AND resolved_at IS NULL
        """,
        provider,
        model,
        metric_name,
    )
    if open_alert is None:
        new_alert_id = await pool.fetchval(
            """
            INSERT INTO nautgate.drift_alerts
                (provider, model, metric_name, direction,
                 peak_z_score, peak_observed, baseline_at_alert, sample_count)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 1)
            RETURNING id
            """,
            provider,
            model,
            metric_name,
            direction,
            z_score,
            observed,
            baseline_mean,
        )
        log.warning(
            "drift_alert_opened",
            provider=provider,
            model=model,
            metric=metric_name,
            direction=direction,
            z=round(z_score, 2),
            observed=observed,
            baseline=round(baseline_mean, 4),
        )
        # Fire-and-forget: investigator decides whether to actually run
        # based on cooldown + daily budget + enabled flag.
        try:
            from app.drift_investigator import maybe_auto_investigate

            await maybe_auto_investigate(
                pool,
                alert_id=new_alert_id,
                provider=provider,
                model=model,
                metric_name=metric_name,
            )
        except Exception as exc:
            log.warning("drift_invest_dispatch_failed", error=str(exc))
    else:
        # Update peak if this z is more extreme.
        if abs(z_score) > abs(float(open_alert["peak_z_score"])):
            await pool.execute(
                """
                UPDATE nautgate.drift_alerts
                   SET peak_z_score = $2, peak_observed = $3, sample_count = sample_count + 1
                 WHERE id = $1
                """,
                open_alert["id"],
                z_score,
                observed,
            )
        else:
            await pool.execute(
                "UPDATE nautgate.drift_alerts SET sample_count = sample_count + 1 WHERE id = $1",
                open_alert["id"],
            )


async def _maybe_resolve_alert(
    pool: asyncpg.Pool,
    *,
    provider: str,
    model: str,
    metric_name: str,
) -> None:
    """If there's an open alert and we've now seen enough normal samples in
    a row (consecutive_anomalies has been 0 for ALERT_RESOLUTION_SAMPLES
    samples since the last update), resolve it.

    We approximate "consecutive normal" by checking the baseline's
    consecutive_anomalies counter — it's already 0, so we resolve.
    Simpler than maintaining a separate consecutive_normal counter.
    """
    await pool.execute(
        """
        UPDATE nautgate.drift_alerts
           SET resolved_at = now()
         WHERE provider = $1 AND model = $2 AND metric_name = $3
           AND resolved_at IS NULL
           AND started_at < now() - INTERVAL '1 second' * $4
        """,
        provider,
        model,
        metric_name,
        ALERT_RESOLUTION_SAMPLES * 5,  # rough: 5s per sample assumed for cooldown
    )


# --- Read APIs for the UI -------------------------------------------------


async def get_drift_overview(pool: asyncpg.Pool) -> dict:
    """Returns: open alerts list + per-(provider, model, metric) baselines."""
    alerts = await pool.fetch(
        """
        SELECT id::text, provider, model, metric_name, direction,
               started_at, resolved_at, peak_z_score, peak_observed,
               baseline_at_alert, sample_count
          FROM nautgate.drift_alerts
         ORDER BY (resolved_at IS NULL) DESC, started_at DESC
         LIMIT 100
        """
    )
    baselines = await pool.fetch(
        """
        SELECT provider, model, metric_name,
               ewma_mean, ewma_variance, sample_count,
               consecutive_anomalies, last_observed, last_z_score, updated_at
          FROM nautgate.model_baselines
         ORDER BY provider, model, metric_name
        """
    )

    return {
        "alerts": [
            {
                "id": a["id"],
                "provider": a["provider"],
                "model": a["model"],
                "metric": a["metric_name"],
                "direction": a["direction"],
                "started_at": a["started_at"].isoformat() if a["started_at"] else None,
                "resolved_at": a["resolved_at"].isoformat() if a["resolved_at"] else None,
                "peak_z_score": float(a["peak_z_score"]),
                "peak_observed": float(a["peak_observed"]),
                "baseline_at_alert": float(a["baseline_at_alert"]),
                "sample_count": a["sample_count"],
                "is_open": a["resolved_at"] is None,
            }
            for a in alerts
        ],
        "baselines": [
            {
                "provider": b["provider"],
                "model": b["model"],
                "metric": b["metric_name"],
                "mean": float(b["ewma_mean"]),
                "stddev": float(b["ewma_variance"]) ** 0.5,
                "sample_count": b["sample_count"],
                "consecutive_anomalies": b["consecutive_anomalies"],
                "last_observed": float(b["last_observed"])
                if b["last_observed"] is not None
                else None,
                "last_z_score": float(b["last_z_score"]) if b["last_z_score"] is not None else None,
                "updated_at": b["updated_at"].isoformat() if b["updated_at"] else None,
            }
            for b in baselines
        ],
    }


async def get_recent_anomalies(
    pool: asyncpg.Pool,
    *,
    provider: str,
    model: str,
    metric_name: str,
    limit: int = 50,
) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT id::text, decision_id::text, z_score, observed_value,
               baseline_mean, baseline_stddev, ts
          FROM nautgate.model_anomalies
         WHERE provider = $1 AND model = $2 AND metric_name = $3
         ORDER BY ts DESC
         LIMIT $4
        """,
        provider,
        model,
        metric_name,
        limit,
    )
    return [
        {
            "id": r["id"],
            "decision_id": r["decision_id"],
            "z_score": float(r["z_score"]),
            "observed_value": float(r["observed_value"]),
            "baseline_mean": float(r["baseline_mean"]),
            "baseline_stddev": float(r["baseline_stddev"]),
            "ts": r["ts"].isoformat() if r["ts"] else None,
        }
        for r in rows
    ]
