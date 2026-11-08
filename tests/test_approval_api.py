import pytest
from fastapi.testclient import TestClient

from order_mcp.approval_api import create_app

H = {"X-Admin-Token": "secret-token"}


@pytest.fixture
def client(engine, settings):
    return TestClient(create_app(engine, settings))


def test_requires_token(client):
    assert client.get("/api/actions").status_code == 401
    assert client.get("/api/actions", headers={"X-Admin-Token": "wrong"}).status_code == 401


def test_list_approve_reject_flow(client, gateway):
    a = gateway.request_order_cancellation(1, "customer changed their mind")["action_id"]
    b = gateway.request_order_cancellation(5, "duplicate order placed by mistake")["action_id"]
    pending = client.get("/api/actions?status=pending", headers=H).json()
    assert {x["id"] for x in pending} == {a, b}

    r = client.post(f"/api/actions/{a}/approve", headers=H, json={"approver": "kshitij"})
    assert r.status_code == 200 and r.json()["status"] == "executed"
    assert client.post(f"/api/actions/{a}/approve", headers=H, json={"approver": "kshitij"}).status_code == 409

    r = client.post(f"/api/actions/{b}/reject", headers=H, json={"approver": "kshitij", "note": "verify first"})
    assert r.json()["status"] == "rejected"
    assert client.post("/api/actions/999/approve", headers=H, json={"approver": "x"}).status_code == 404

    audit = client.get("/api/audit", headers=H).json()
    assert any(e["actor"] == "human:kshitij" and e["outcome"] == "executed" for e in audit)


def test_refuses_to_start_without_token(engine, settings):
    from dataclasses import replace
    with pytest.raises(RuntimeError):
        create_app(engine, replace(settings, admin_token=""))


def test_dashboard_served(client):
    assert "Pending agent actions" in client.get("/").text
