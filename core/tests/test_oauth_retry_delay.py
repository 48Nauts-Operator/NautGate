"""A 529 retry must actually wait.

Anthropic answers some 529s with `retry-after: 0`. Honouring that verbatim
collapsed the backoff to nothing and the client saw the 529 the retry loop
exists to absorb.
"""

from app.anthropic_oauth_forwarder import _RETRY_CAP_S, _retry_delay


def test_retry_after_zero_cannot_disable_backoff():
    assert _retry_delay(0, 0.0) >= 0.4
    assert _retry_delay(1, 0.0) >= 0.8


def test_absent_header_keeps_the_exponential_ladder():
    assert _retry_delay(0, None) < _retry_delay(1, None) < _retry_delay(2, None)


def test_a_longer_retry_after_still_wins():
    assert _retry_delay(0, 3.0) == 3.0


def test_delay_never_exceeds_the_cap():
    assert _retry_delay(9, None) <= _RETRY_CAP_S + 1.0
