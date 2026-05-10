"""Tests for drift detection (app/drift.py) — pure functions only."""

from app.drift import (
    ANOMALY_CLUSTER_THRESHOLD,
    DEFAULT_Z_THRESHOLD,
    METRIC_NAMES,
    MIN_SAMPLES_FOR_ANOMALY,
    alert_direction,
    compute_session_id,
    detect_compaction,
    extract_metrics,
    update_ewma,
)

# --- update_ewma -----------------------------------------------------------


def test_first_sample_initializes_baseline():
    u = update_ewma(prev_mean=0.0, prev_variance=0.0, prev_sample_count=0, observation=42.0)
    assert u.new_mean == 42.0
    assert u.new_variance == 0.0
    assert u.new_sample_count == 1
    assert u.z_score is None
    assert not u.is_anomaly


def test_z_score_none_until_warmup():
    state = {"mean": 0.0, "var": 0.0, "n": 0}
    for i in range(MIN_SAMPLES_FOR_ANOMALY - 1):
        u = update_ewma(prev_mean=state["mean"], prev_variance=state["var"], prev_sample_count=state["n"], observation=10.0)
        state.update(mean=u.new_mean, var=u.new_variance, n=u.new_sample_count)
        assert u.z_score is None, f"z_score should be None on sample {i}"
    # Final sample completes warmup but variance is still 0 (constant input) → still None.
    u = update_ewma(prev_mean=state["mean"], prev_variance=state["var"], prev_sample_count=state["n"], observation=10.0)
    assert u.z_score is None


def test_anomaly_fires_on_outlier_after_warmup():
    # Stream 20 samples around 100, then one at 500 — should be anomaly.
    state = {"mean": 0.0, "var": 0.0, "n": 0}
    for i in range(20):
        # Vary slightly to give variance > 0.
        v = 100.0 + (i % 3 - 1) * 5.0
        u = update_ewma(prev_mean=state["mean"], prev_variance=state["var"], prev_sample_count=state["n"], observation=v)
        state.update(mean=u.new_mean, var=u.new_variance, n=u.new_sample_count)
    outlier = update_ewma(prev_mean=state["mean"], prev_variance=state["var"], prev_sample_count=state["n"], observation=500.0)
    assert outlier.is_anomaly
    assert outlier.z_score is not None and outlier.z_score > DEFAULT_Z_THRESHOLD


def test_normal_sample_after_warmup_is_not_anomaly():
    state = {"mean": 0.0, "var": 0.0, "n": 0}
    for _ in range(20):
        u = update_ewma(prev_mean=state["mean"], prev_variance=state["var"], prev_sample_count=state["n"], observation=100.0 + (_ % 3 - 1) * 2.0)
        state.update(mean=u.new_mean, var=u.new_variance, n=u.new_sample_count)
    next_normal = update_ewma(prev_mean=state["mean"], prev_variance=state["var"], prev_sample_count=state["n"], observation=101.0)
    assert not next_normal.is_anomaly


# --- detect_compaction -----------------------------------------------------


def test_compaction_fires_on_sharp_drop():
    # 80 turns then 10 — 87.5% drop on a long conversation.
    assert detect_compaction(prev_count=80, current_count=10)


def test_compaction_does_not_fire_on_growth():
    assert not detect_compaction(prev_count=10, current_count=11)


def test_compaction_does_not_fire_on_short_conversation():
    # Below the COMPACTION_MIN_PREV_COUNT threshold.
    assert not detect_compaction(prev_count=4, current_count=1)


def test_compaction_does_not_fire_on_small_drop():
    # 30 → 25 is only 17% drop, well below the 50% trigger.
    assert not detect_compaction(prev_count=30, current_count=25)


def test_compaction_safe_with_nones():
    assert not detect_compaction(prev_count=None, current_count=10)
    assert not detect_compaction(prev_count=10, current_count=None)


# --- compute_session_id ----------------------------------------------------


def test_session_id_stable_across_growing_history():
    """Same first user message → same session_id, even as the conversation grows."""
    msgs1 = [{"role": "user", "content": "build me an iOS app"}]
    msgs2 = msgs1 + [
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "now add login"},
    ]
    sid1 = compute_session_id("pi", msgs1)
    sid2 = compute_session_id("pi", msgs2)
    assert sid1 == sid2 and sid1 is not None


def test_different_first_messages_get_different_sessions():
    sid_a = compute_session_id("pi", [{"role": "user", "content": "one thing"}])
    sid_b = compute_session_id("pi", [{"role": "user", "content": "another thing"}])
    assert sid_a != sid_b


def test_different_agents_isolated():
    msgs = [{"role": "user", "content": "x"}]
    assert compute_session_id("pi", msgs) != compute_session_id("claude-code", msgs)


def test_session_id_handles_block_content():
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "hello world"}]},
    ]
    assert compute_session_id("pi", msgs) is not None


def test_session_id_returns_none_when_unidentifiable():
    assert compute_session_id(None, [{"role": "user", "content": "x"}]) is None
    assert compute_session_id("pi", []) is None
    assert compute_session_id("pi", [{"role": "system", "content": "x"}]) is None


# --- extract_metrics -------------------------------------------------------


def test_extract_metrics_omits_missing_inputs():
    m = extract_metrics(
        prompt_tokens=None, request_size_bytes=None,
        response_size_bytes=1234, first_byte_ms=None,
        duration_ms=None, messages_count_delta=None,
    )
    assert m == {"response_size_bytes": 1234.0}


def test_extract_metrics_computes_tokens_per_byte():
    m = extract_metrics(
        prompt_tokens=400, request_size_bytes=1600,
        response_size_bytes=None, first_byte_ms=None,
        duration_ms=None, messages_count_delta=None,
    )
    assert m["input_tokens_per_byte"] == 0.25


def test_extract_metrics_handles_zero_request_size():
    m = extract_metrics(
        prompt_tokens=400, request_size_bytes=0,
        response_size_bytes=None, first_byte_ms=None,
        duration_ms=None, messages_count_delta=None,
    )
    assert "input_tokens_per_byte" not in m


# --- alert_direction -------------------------------------------------------


def test_alert_direction_up_and_down():
    assert alert_direction(observed=200.0, baseline_mean=100.0) == "up"
    assert alert_direction(observed=50.0, baseline_mean=100.0) == "down"


# --- metric set monotonic --------------------------------------------------


def test_metric_names_includes_compaction():
    assert "messages_count_delta" in METRIC_NAMES
    assert len(METRIC_NAMES) == 5


def test_cluster_threshold_is_meaningful():
    """Tightness check — cluster threshold should be ≥2 to avoid noise."""
    assert ANOMALY_CLUSTER_THRESHOLD >= 2


def test_consecutive_anomalies_can_accumulate():
    """Regression: EWMA used to absorb outliers so fast that consecutive
    anomalies couldn't reach the cluster threshold. Damping fixes this.
    Inject a clear, sustained shift and verify ≥2 consecutive anomalies fire.
    """
    state = {"mean": 0.0, "var": 0.0, "n": 0}
    # Warm up around 100, slight noise.
    for i in range(20):
        v = 100.0 + (i % 3 - 1) * 5.0
        u = update_ewma(prev_mean=state["mean"], prev_variance=state["var"], prev_sample_count=state["n"], observation=v)
        state.update(mean=u.new_mean, var=u.new_variance, n=u.new_sample_count)
    # Now sustained shift to ~600 (z ≈ 50 vs original baseline).
    consecutive = 0
    for _ in range(5):
        u = update_ewma(prev_mean=state["mean"], prev_variance=state["var"], prev_sample_count=state["n"], observation=600.0)
        state.update(mean=u.new_mean, var=u.new_variance, n=u.new_sample_count)
        consecutive = consecutive + 1 if u.is_anomaly else 0
        if consecutive >= ANOMALY_CLUSTER_THRESHOLD:
            return
    raise AssertionError(f"Cluster threshold {ANOMALY_CLUSTER_THRESHOLD} never reached during sustained shift")
