"""Unit tests for the gateway. Free Mobile is never contacted."""
import importlib
import sys

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_USER", "00000000")
    monkeypatch.setenv("FREE_API_KEY", "TEST_KEY_NOT_REAL")
    monkeypatch.setenv("CLIENT_KEYS", '{"monitoring":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","automation":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}')
    monkeypatch.setenv("DB_PATH", str(tmp_path / "alerts.sqlite3"))
    monkeypatch.setenv("MIN_SMS_INTERVAL_SEC", "60")
    monkeypatch.setenv("DAILY_SMS_LIMIT", "100")
    monkeypatch.setenv("CLIENT_HOURLY_LIMIT", "20")
    monkeypatch.setenv("DEDUP_WINDOW_SEC", "300")
    sys.modules.pop("app", None)
    app = importlib.import_module("app")
    app.init_db()
    yield app
    sys.modules.pop("app", None)


@pytest.fixture
def client(gateway):
    # TestClient without entering lifespan: worker must not contact real Free API.
    return TestClient(gateway.app)


AUTH = {"Authorization": "Bearer " + "a" * 32}
OTHER_AUTH = {"Authorization": "Bearer " + "b" * 32}


def test_health(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_authentication_is_required(client):
    assert client.post("/v1/alerts", json={"message": "ok"}).status_code == 401
    assert client.post("/v1/alerts", json={"message": "ok"}, headers={"Authorization": "Bearer invalid"}).status_code == 401


def test_enqueue_and_per_client_visibility(client):
    response = client.post("/v1/alerts", json={"message": "generic event"}, headers=AUTH)
    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "queued"
    assert client.get(f"/v1/alerts/{data['id']}", headers=AUTH).json()["status"] == "queued"
    assert client.get(f"/v1/alerts/{data['id']}", headers=OTHER_AUTH).status_code == 404


def test_fingerprint_deduplication(client):
    body = {"message": "gateway online", "fingerprint": "status-change"}
    first = client.post("/v1/alerts", json=body, headers=AUTH).json()
    second = client.post("/v1/alerts", json=body, headers=AUTH).json()
    assert second["id"] == first["id"]
    assert second["deduplicated"] is True


def test_idempotency_request_id(client):
    first = client.post("/v1/alerts", json={"message": "one", "request_id": "request-0001"}, headers=AUTH).json()
    second = client.post("/v1/alerts", json={"message": "two", "request_id": "request-0001"}, headers=AUTH).json()
    assert second["id"] == first["id"]
    assert second["duplicate"] is True


def test_invalid_severity(client):
    assert client.post("/v1/alerts", json={"message": "x", "severity": "unknown"}, headers=AUTH).status_code == 422


@pytest.mark.asyncio
async def test_worker_accepts_success(gateway):
    class FakeHTTP:
        async def post(self, *args, **kwargs):
            return type("Response", (), {"status_code": 200})()

    now_client = "monitoring"
    with gateway.connect() as c:
        c.execute("INSERT INTO alerts(client,fingerprint,severity,message,status,created,next_attempt) VALUES(?,?,?,?,'queued',0,0)", (now_client, "test", "critical", "mock incident"))
    await gateway.process_next(FakeHTTP())
    with gateway.connect() as c:
        assert c.execute("SELECT status FROM alerts LIMIT 1").fetchone()["status"] == "sent"


@pytest.mark.asyncio
async def test_rate_limit_requeues(gateway):
    class FakeHTTP:
        async def post(self, *args, **kwargs):
            return type("Response", (), {"status_code": 402})()

    with gateway.connect() as c:
        c.execute("INSERT INTO alerts(client,fingerprint,severity,message,status,created,next_attempt) VALUES(?,?,?,?,'queued',0,0)", ("monitoring", "test", "critical", "mock incident"))
    await gateway.process_next(FakeHTTP())
    with gateway.connect() as c:
        row = c.execute("SELECT status,next_attempt FROM alerts LIMIT 1").fetchone()
        assert row["status"] == "queued"
        assert row["next_attempt"] > 0

@pytest.mark.asyncio
async def test_network_error_marked_uncertain(gateway):
    import httpx

    class UnreachableHTTP:
        async def post(self, *args, **kwargs):
            raise httpx.ConnectError("network offline")

    with gateway.connect() as c:
        c.execute("INSERT INTO alerts(client,fingerprint,severity,message,status,created,next_attempt) VALUES(?,?,?,?,'queued',0,0)", ("monitoring", "test", "critical", "generic test"))
    await gateway.process_next(UnreachableHTTP())
    with gateway.connect() as c:
        assert c.execute("SELECT status FROM alerts LIMIT 1").fetchone()["status"] == "uncertain"


def test_rejects_example_credentials(monkeypatch):
    monkeypatch.setenv("FREE_USER", "00000000")
    monkeypatch.setenv("FREE_API_KEY", "YOUR_NEW_FREE_SMS_API_KEY")
    monkeypatch.setenv("CLIENT_KEYS", '{"monitoring":"REPLACE_WITH_RANDOM_SECRET_OF_AT_LEAST_32_CHARACTERS"}')
    sys.modules.pop("app", None)
    with pytest.raises(RuntimeError, match="placeholder"):
        importlib.import_module("app")
    sys.modules.pop("app", None)
