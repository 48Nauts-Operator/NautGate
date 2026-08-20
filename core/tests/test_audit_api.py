import json
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException

from app.routes import v1

RECEIPT_ID = "00000000-0000-7000-8000-000000000072"
CHECKPOINT_ID = "10000000-0000-7000-8000-000000000073"


def _request():
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=object())))


@pytest.fixture
def authenticated(monkeypatch):
    async def fake_authenticate(_pool, _request):
        return "agent-a"

    monkeypatch.setattr(v1, "authenticate", fake_authenticate)


@pytest.mark.asyncio
async def test_receipt_status_is_scoped_to_authenticated_agent(monkeypatch, authenticated):
    request = _request()
    seen = {}

    async def fake_get(_pool, *, receipt_id, agent_id):
        seen.update(receipt_id=receipt_id, agent_id=agent_id)
        return {"receipt_id": str(receipt_id), "evidence_status": "pending", "attested": False}

    monkeypatch.setattr(v1.queries, "get_audit_receipt", fake_get)
    response = await v1.audit_receipt_status(RECEIPT_ID, request)
    assert json.loads(response.body)["attested"] is False
    assert seen == {"receipt_id": UUID(RECEIPT_ID), "agent_id": "agent-a"}


@pytest.mark.asyncio
async def test_checkpoint_only_exposes_signature_when_query_marks_verified(
    monkeypatch, authenticated
):
    request = _request()

    async def fake_get(_pool, *, checkpoint_id, agent_id):
        assert checkpoint_id == UUID(CHECKPOINT_ID)
        assert agent_id == "agent-a"
        return {"evidence_status": "verified", "attested": True, "signature": {"value": "x"}}

    monkeypatch.setattr(v1.queries, "get_audit_checkpoint", fake_get)
    response = await v1.audit_checkpoint_status(CHECKPOINT_ID, request)
    assert json.loads(response.body)["attested"] is True


@pytest.mark.asyncio
async def test_unknown_or_other_agent_receipt_has_stable_not_found(monkeypatch, authenticated):
    async def fake_get(*_args, **_kwargs):
        return None

    monkeypatch.setattr(v1.queries, "get_audit_receipt", fake_get)
    with pytest.raises(HTTPException) as exc:
        await v1.audit_receipt_status(RECEIPT_ID, _request())
    assert (exc.value.status_code, exc.value.detail) == (404, "receipt_not_found")


@pytest.mark.asyncio
async def test_audit_status_is_tenant_scoped(monkeypatch, authenticated):
    async def fake_status(_pool, *, agent_id):
        assert agent_id == "agent-a"
        return {"schema": "dev.nautgate.audit-status/v1", "pending": 2, "verified": 4}

    monkeypatch.setattr(v1.queries, "get_audit_status", fake_status)
    response = await v1.audit_status(_request())
    assert json.loads(response.body)["pending"] == 2


@pytest.mark.asyncio
async def test_key_history_requires_authentication(monkeypatch):
    async def reject(_pool, _request):
        raise HTTPException(status_code=401, detail="invalid_api_key")

    monkeypatch.setattr(v1, "authenticate", reject)
    with pytest.raises(HTTPException) as exc:
        await v1.audit_signing_key_history(_request())
    assert exc.value.status_code == 401
