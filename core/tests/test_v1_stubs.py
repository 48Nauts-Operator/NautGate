import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path,expected_coming_in",
    [
        ("POST", "/v1/messages", "week-1b"),
        ("POST", "/v1/responses", "week-1b"),
        ("GET", "/v1/models", "week-1"),
        ("GET", "/v1/stats", "week-1"),
        ("GET", "/v1/profile", "week-1"),
    ],
)
async def test_v1_stub_returns_501_with_coming_in_header(client, method, path, expected_coming_in):
    resp = await client.request(method, path)
    assert resp.status_code == 501, (path, resp.text)
    assert resp.headers.get("x-nautgate-coming-in") == expected_coming_in
    body = resp.json()
    assert body["error"] == "not_implemented"
    assert "message" in body
