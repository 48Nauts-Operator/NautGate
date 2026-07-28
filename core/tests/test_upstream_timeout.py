"""Upstream read timeout is configurable, and a timeout is diagnosable.

Regression for a live failure: Cockpit research calls to a thinking model died at
exactly 120s with `502 upstream_failed` and a log line reading `error: ""`. The
work upstream was still in progress — NautGate simply gave up, and the empty
error message made it look like a hard upstream failure.
"""

import httpx
import pytest

from app.services.nautrouter import NautRouterClient
from app.settings import Settings


def test_timeout_defaults_to_something_a_long_report_can_finish_in():
    # 120s was not enough for a real research generation; the default must leave
    # room for a thinking model writing a long report.
    assert Settings().nautgate_upstream_timeout_s >= 300


def test_timeout_is_configurable():
    assert Settings(nautgate_upstream_timeout_s=42.0).nautgate_upstream_timeout_s == 42.0


def test_client_applies_the_configured_read_timeout():
    c = NautRouterClient("http://localhost:8404", timeout_s=333.0)
    assert c._client.timeout.read == 333.0
    # Connect stays fast so a genuinely down sidecar still fails immediately
    # rather than hanging for the whole read budget.
    assert c._client.timeout.connect == 2.0


def test_client_default_matches_settings_default():
    c = NautRouterClient("http://localhost:8404")
    assert c._client.timeout.read == pytest.approx(Settings().nautgate_upstream_timeout_s)


def test_httpx_timeout_str_is_empty_which_is_why_we_log_the_type():
    # The property that made the original failure undiagnosable.
    assert str(httpx.ReadTimeout("")) == ""
    assert type(httpx.ReadTimeout("")).__name__ == "ReadTimeout"
