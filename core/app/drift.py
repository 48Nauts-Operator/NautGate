"""Behavior-drift detection — catches silent provider-side changes.

A model that quietly starts compacting context, switching tokenizers, or
slowing down by 2× is a real operational risk: it doesn't error, the
scorecard doesn't catch it (request quality may be fine), but something
*changed*. Drift detection notices.

Approach: per (provider, model, metric) we maintain an exponentially-weighted
moving mean + variance. Each new observation gets a z-score against the
baseline. ``|z| > 3`` writes an anomaly row. ``N`` consecutive anomalies on
the same metric raise an alert (cluster), which auto-resolves once normal
samples return.

Five metrics tracked:

  ``input_tokens_per_byte``       prompt_tokens ÷ request_size_bytes
                                  Tokenizer or cache-policy change → ratio
                                  shifts.
  ``response_size_bytes``         what the model wrote back, in bytes
                                  Style/verbosity drift.
  ``first_byte_ms``               TTFB
                                  Provider routing / queue / hot-path change.
  ``duration_ms``                 total roundtrip
                                  End-to-end perf regression.
  ``messages_count_delta``        per-session: this turn's count − previous
                                  Compaction events (large negative delta on
                                  what should be a monotone-up sequence).

Compaction is flagged unconditionally when the delta is large negative,
because z-score against an "always grows" baseline catches it but the
*event itself* is what we want to surface, not just the statistical edge.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

# Higher alpha = more weight on recent samples. 0.05 means the EWMA "remembers"
# roughly 1/alpha = 20 samples back.
DEFAULT_EWMA_ALPHA = 0.05

# A metric needs this many samples before z-scoring is meaningful — early
# on, variance estimates are too noisy.
MIN_SAMPLES_FOR_ANOMALY = 10

# |z| threshold for a single sample to count as an anomaly.
DEFAULT_Z_THRESHOLD = 3.0

# Number of consecutive anomalies that escalate to a cluster alert.
# Two is the sweet spot: one anomaly is noise, two in a row is a pattern.
# Three+ would never trigger because EWMA absorbs the second outlier fast
# enough that the third sample's z drops below threshold.
ANOMALY_CLUSTER_THRESHOLD = 2

# When a sample IS an anomaly, slow down the baseline's adaptation so one
# wild outlier doesn't swallow the baseline (otherwise consecutive anomalies
# can't accumulate). 0.1× alpha means anomalous samples barely move the
# baseline; clean samples adapt normally.
ANOMALY_ALPHA_DAMPING = 0.1

# Number of normal samples needed after an alert before we mark it resolved.
ALERT_RESOLUTION_SAMPLES = 5

# Compaction trigger — drop in messages_count by at least this fraction of the
# previous count, AND at least this absolute size. Both prevent false positives
# on very short conversations.
COMPACTION_DROP_FRACTION = 0.50
COMPACTION_MIN_PREV_COUNT = 5

METRIC_NAMES: tuple[str, ...] = (
    "input_tokens_per_byte",
    "response_size_bytes",
    "first_byte_ms",
    "duration_ms",
    "messages_count_delta",
)


@dataclass(frozen=True)
class BaselineUpdate:
    """Result of feeding one observation into a baseline."""

    new_mean: float
    new_variance: float
    new_sample_count: int
    z_score: float | None  # None when baseline is too cold for scoring
    is_anomaly: bool


def update_ewma(
    *,
    prev_mean: float,
    prev_variance: float,
    prev_sample_count: int,
    observation: float,
    alpha: float = DEFAULT_EWMA_ALPHA,
    z_threshold: float = DEFAULT_Z_THRESHOLD,
) -> BaselineUpdate:
    """Update an EWMA mean+variance with one new observation.

    On the first sample, the mean *is* the observation and variance is 0;
    z-score is None until we have at least MIN_SAMPLES_FOR_ANOMALY samples,
    at which point variance estimates are stable enough to score against.
    """
    if prev_sample_count == 0:
        return BaselineUpdate(
            new_mean=observation,
            new_variance=0.0,
            new_sample_count=1,
            z_score=None,
            is_anomaly=False,
        )

    # First, score the observation against the *existing* baseline so we
    # know if it's an outlier *before* deciding how much to adapt.
    delta = observation - prev_mean
    new_count = prev_sample_count + 1
    z: float | None = None
    is_anomaly = False
    if new_count >= MIN_SAMPLES_FOR_ANOMALY and prev_variance > 0:
        stddev = prev_variance**0.5
        z = delta / stddev
        is_anomaly = abs(z) >= z_threshold

    # Now adapt — slowly when the sample is an outlier so anomalies can
    # accumulate, normally otherwise. Welford-ish EWMA.
    eff_alpha = alpha * ANOMALY_ALPHA_DAMPING if is_anomaly else alpha
    new_mean = prev_mean + eff_alpha * delta
    new_variance = (1 - eff_alpha) * (prev_variance + eff_alpha * delta * delta)

    return BaselineUpdate(
        new_mean=new_mean,
        new_variance=new_variance,
        new_sample_count=new_count,
        z_score=z,
        is_anomaly=is_anomaly,
    )


def compute_session_id(agent_id: str | None, messages: list | None) -> str | None:
    """Heuristic session id — hash(agent_id + first user message snippet).

    Stable across the whole conversation as long as the agent keeps shipping
    the original first user message in history. Unique per conversation if
    the first message differs. Returns None when we can't compute.
    """
    if not agent_id or not messages:
        return None
    first_user_text = ""
    for m in messages:
        if not isinstance(m, dict):
            continue
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str):
            first_user_text = content
        elif isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    t = blk.get("text", "")
                    if isinstance(t, str):
                        first_user_text = t
                        break
        if first_user_text:
            break
    if not first_user_text:
        return None
    digest = hashlib.sha1(
        f"{agent_id}|{first_user_text[:200]}".encode(),
    ).hexdigest()
    return digest[:16]


def detect_compaction(prev_count: int | None, current_count: int | None) -> bool:
    """Did this turn drop the messages count sharply?

    Per agentic CLI behavior: history grows monotonically until the agent
    actively summarizes/compacts older turns to stay under context. A large
    negative delta is the smoking gun.
    """
    if prev_count is None or current_count is None:
        return False
    if prev_count < COMPACTION_MIN_PREV_COUNT:
        return False
    if current_count >= prev_count:
        return False
    drop = (prev_count - current_count) / prev_count
    return drop >= COMPACTION_DROP_FRACTION


def extract_metrics(
    *,
    prompt_tokens: int | None,
    request_size_bytes: int | None,
    response_size_bytes: int | None,
    first_byte_ms: int | None,
    duration_ms: int | None,
    messages_count_delta: int | None,
) -> dict[str, float]:
    """Pull the five drift metrics from an outcome record. Returns a dict
    keyed by metric_name; missing inputs are simply omitted (no zero-fill).
    """
    out: dict[str, float] = {}
    if prompt_tokens is not None and request_size_bytes and request_size_bytes > 0:
        out["input_tokens_per_byte"] = prompt_tokens / request_size_bytes
    if response_size_bytes is not None:
        out["response_size_bytes"] = float(response_size_bytes)
    if first_byte_ms is not None:
        out["first_byte_ms"] = float(first_byte_ms)
    if duration_ms is not None:
        out["duration_ms"] = float(duration_ms)
    if messages_count_delta is not None:
        out["messages_count_delta"] = float(messages_count_delta)
    return out


def alert_direction(observed: float, baseline_mean: float) -> str:
    """``up`` if observed exceeds baseline, ``down`` otherwise. Used for
    the drift_alerts.direction column so the UI can show ↑ vs ↓ clearly.
    """
    return "up" if observed > baseline_mean else "down"
