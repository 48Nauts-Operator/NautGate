"""The dashboard's embedded admin token must never leave this machine.

NAUTGATE_LOCAL_ADMIN_TOKEN is documented "local single-operator use only", but
the check was missing: the server binds 0.0.0.0, so any LAN or tailnet peer could
GET /dashboard, scrape the token from the HTML and drive the whole API with it.
Confirmed by doing exactly that against a live instance over its tailnet address.
"""

from types import SimpleNamespace

from app.main import _is_loopback


def _req(host):
    return SimpleNamespace(client=SimpleNamespace(host=host) if host else None)


def test_loopback_callers_are_local():
    assert _is_loopback(_req("127.0.0.1"))
    assert _is_loopback(_req("::1"))


def test_lan_and_tailnet_callers_are_not():
    assert not _is_loopback(_req("100.108.101.97"))  # the tailnet address used to prove the leak
    assert not _is_loopback(_req("192.168.1.50"))
    assert not _is_loopback(_req("10.0.0.9"))
    assert not _is_loopback(_req("203.0.113.7"))


def test_missing_client_is_not_treated_as_local():
    assert not _is_loopback(_req(None))


def test_a_spoofable_forwarded_header_cannot_make_you_local():
    # X-Forwarded-For is caller-supplied; the check must ignore it entirely.
    r = _req("100.108.101.97")
    r.headers = {"x-forwarded-for": "127.0.0.1", "x-real-ip": "127.0.0.1"}
    assert not _is_loopback(r)
