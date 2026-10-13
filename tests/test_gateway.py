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


def insert_alert(gateway, *, status='queued', severity='critical', created=None, expires_at=None, fingerprint='event'):
    import time
    created = time.time() if created is None else created
    with gateway.connect() as c:
        return c.execute('''INSERT INTO alerts(client,fingerprint,severity,message,status,created,next_attempt,expires_at)
            VALUES(?,?,?,?,?,?,?,?)''',
            ('monitoring', fingerprint, severity, 'generic test alert', status, created, created,
             expires_at if expires_at is not None else created + 3600)).lastrowid


class FakeHTTP:
    def __init__(self, code=200):
        self.code = code
        self.calls = 0

    async def post(self, *args, **kwargs):
        self.calls += 1
        return type('Response', (), {'status_code': self.code})()


@pytest.mark.asyncio
async def test_every_send_attempt_is_recorded(gateway):
    insert_alert(gateway)
    http = FakeHTTP(402)
    await gateway.process_next(http)
    with gateway.connect() as c:
        row = c.execute('SELECT COUNT(*) AS n, MAX(started) AS t FROM delivery_attempts').fetchone()
        assert row['n'] == 1 and row['t'] > 0


@pytest.mark.asyncio
async def test_global_throttle_applies_after_rejected_send(gateway):
    insert_alert(gateway, fingerprint='first')
    http = FakeHTTP(402)
    await gateway.process_next(http)
    insert_alert(gateway, fingerprint='second')
    await gateway.process_next(http)
    assert http.calls == 1
    with gateway.connect() as c:
        assert c.execute('SELECT COUNT(*) AS n FROM delivery_attempts').fetchone()['n'] == 1


@pytest.mark.asyncio
async def test_global_daily_attempt_quota(gateway, monkeypatch):
    monkeypatch.setattr(gateway, 'DAILY', 1)
    insert_alert(gateway)
    with gateway.connect() as c:
        c.execute('INSERT INTO delivery_attempts(alert_id,started,result) VALUES(?,?,?)',
                  (1, gateway.time.time()-120, 'http_402'))
    http = FakeHTTP()
    await gateway.process_next(http)
    assert http.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('code', [500, 502, 503, 404, 418])
async def test_ambiguous_http_is_not_retried(gateway, code):
    alert_id = insert_alert(gateway)
    http = FakeHTTP(code)
    await gateway.process_next(http)
    with gateway.connect() as c:
        row = c.execute('SELECT status,last_error FROM alerts WHERE id=?', (alert_id,)).fetchone()
    assert row['status'] == 'uncertain'
    assert 'unknown' in row['last_error']
    assert http.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('code', [400, 401, 403])
async def test_permanent_rejection_failed(gateway, code):
    alert_id = insert_alert(gateway)
    await gateway.process_next(FakeHTTP(code))
    with gateway.connect() as c:
        assert c.execute('SELECT status FROM alerts WHERE id=?', (alert_id,)).fetchone()['status'] == 'failed'


@pytest.mark.asyncio
async def test_expired_alert_never_reaches_free(gateway):
    import time
    alert_id = insert_alert(gateway, created=time.time()-500, expires_at=time.time()-100)
    http = FakeHTTP()
    await gateway.process_next(http)
    assert http.calls == 0
    with gateway.connect() as c:
        assert c.execute('SELECT status FROM alerts WHERE id=?', (alert_id,)).fetchone()['status'] == 'expired'


def test_ttl_validated_and_reported(client):
    too_short = client.post('/v1/alerts', json={'message':'hello','ttl_seconds':59}, headers=AUTH)
    assert too_short.status_code == 422
    response = client.post('/v1/alerts', json={'message':'hello','ttl_seconds':90}, headers=AUTH)
    assert response.status_code == 202
    row = client.get('/v1/alerts/'+str(response.json()['id']), headers=AUTH).json()
    assert 89 <= row['expires_at']-row['created'] <= 91


def test_queue_capacity_is_bounded(client, gateway, monkeypatch):
    monkeypatch.setattr(gateway, 'MAX_QUEUE', 1)
    assert client.post('/v1/alerts', json={'message': 'alert one'}, headers=AUTH).status_code == 202
    assert client.post('/v1/alerts', json={'message': 'alert two'}, headers=AUTH).status_code == 503


def test_hourly_quota_is_enforced(client, gateway, monkeypatch):
    monkeypatch.setattr(gateway, 'PER_CLIENT_HOUR', 1)
    assert client.post('/v1/alerts', json={'message': 'alert one'}, headers=AUTH).status_code == 202
    assert client.post('/v1/alerts', json={'message': 'alert two'}, headers=AUTH).status_code == 429


def test_health_vs_readiness(client):
    assert client.get('/healthz').status_code == 200
    # Unit tests don't run the lifespan worker.
    ready = client.get('/readyz')
    assert ready.status_code == 503
    assert ready.json()['reason'] == 'worker_unhealthy'


def test_critical_uncertain_causes_readiness_failure(client, gateway, monkeypatch):
    import time
    class Active:
        def done(self):
            return False
    monkeypatch.setattr(gateway, 'worker_task', Active())
    monkeypatch.setattr(gateway, 'worker_heartbeat', time.monotonic())
    assert client.get('/readyz').status_code == 200
    insert_alert(gateway, status='uncertain')
    ready = client.get('/readyz')
    assert ready.status_code == 503
    assert ready.json()['reason'] == 'critical_delivery_unresolved'


def test_old_backlog_causes_readiness_failure(client, gateway, monkeypatch):
    import time
    class Active:
        def done(self):
            return False
    monkeypatch.setattr(gateway, 'worker_task', Active())
    monkeypatch.setattr(gateway, 'worker_heartbeat', time.monotonic())
    insert_alert(gateway, status='queued', created=time.time()-1000, expires_at=time.time()+500)
    ready = client.get('/readyz')
    assert ready.status_code == 503
    assert ready.json()['reason'] == 'backlog_stale'


def test_sqlite_migrates_v01_queue_safely(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / 'legacy.sqlite3'
    with sqlite3.connect(path) as c:
        c.execute('''CREATE TABLE alerts(id INTEGER PRIMARY KEY AUTOINCREMENT,client TEXT NOT NULL,
            fingerprint TEXT NOT NULL,severity TEXT NOT NULL,message TEXT NOT NULL,status TEXT NOT NULL,
            created REAL NOT NULL,next_attempt REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '', sent_at REAL,request_id TEXT,
            UNIQUE(client, request_id))''')
        c.execute("INSERT INTO alerts(client,fingerprint,severity,message,status,created,next_attempt) "
                  "VALUES('monitoring','old','warning','legacy data','queued',100,100)")
    monkeypatch.setenv('DB_PATH', str(path))
    monkeypatch.setenv('FREE_USER', '00000000')
    monkeypatch.setenv('FREE_API_KEY', 'TEST_KEY_NOT_REAL')
    monkeypatch.setenv('CLIENT_KEYS', '{"monitoring":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}')
    sys.modules.pop('app', None)
    gateway = importlib.import_module('app')
    gateway.init_db()
    with gateway.connect() as c:
        row = c.execute('SELECT status,expires_at FROM alerts WHERE fingerprint=?', ('old',)).fetchone()
        assert row['expires_at'] == 100 + gateway.ALERT_TTL
    gateway.init_db()
    with gateway.connect() as c:
        assert c.execute('SELECT COUNT(*) AS n FROM alerts').fetchone()['n'] == 1
    sys.modules.pop('app', None)


def test_restart_marks_ambiguous_inflight_as_uncertain(gateway):
    alert_id = insert_alert(gateway, status='sending')
    with gateway.connect() as c:
        c.execute("INSERT INTO delivery_attempts(alert_id,started,result) VALUES(?,?,?)",
                  (alert_id, gateway.time.time(), 'sending'))
    gateway.init_db()
    with gateway.connect() as c:
        assert c.execute('SELECT status FROM alerts WHERE id=?', (alert_id,)).fetchone()['status'] == 'uncertain'
        assert c.execute('SELECT result FROM delivery_attempts LIMIT 1').fetchone()['result'] == 'uncertain_restart'


def test_retention_preserves_uncertain_and_removes_old_terminal(gateway, monkeypatch):
    import time
    monkeypatch.setattr(gateway, 'RETENTION_DAYS', 1)
    ago = time.time()-2*86400
    sent = insert_alert(gateway, status='sent', created=ago, fingerprint='sent-old')
    uncertain = insert_alert(gateway, status='uncertain', created=ago, fingerprint='uncertain-old')
    with gateway.connect() as c:
        gateway.housekeeping(c, time.time())
        assert c.execute('SELECT id FROM alerts WHERE id=?', (sent,)).fetchone() is None
        assert c.execute('SELECT id FROM alerts WHERE id=?', (uncertain,)).fetchone() is not None


def test_dedup_disabled_never_suppresses_distinct_requests(client, gateway, monkeypatch):
    monkeypatch.setattr(gateway, 'DEDUP', 0)
    one = client.post('/v1/alerts', json={'message':'same'}, headers=AUTH).json()
    two = client.post('/v1/alerts', json={'message':'same'}, headers=AUTH).json()
    assert one['id'] != two['id']


def test_ops_backup_is_consistent_and_permissions_strict(gateway, tmp_path, monkeypatch):
    import importlib.util
    import os
    import sqlite3
    from pathlib import Path
    script = Path(__file__).parents[1] / 'scripts' / 'ops.py'
    spec = importlib.util.spec_from_file_location('gateway_ops', script)
    ops = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ops)
    monkeypatch.setenv('DB_PATH', gateway.DB_PATH)
    insert_alert(gateway, fingerprint='backup-test')
    backup = tmp_path / 'backup.sqlite3'
    ops.backup(str(backup))
    assert backup.exists()
    assert os.stat(backup).st_mode & 0o777 == 0o600
    with sqlite3.connect(str(backup)) as c:
        assert c.execute("SELECT COUNT(*) FROM alerts WHERE fingerprint='backup-test'").fetchone()[0] == 1
        assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    with pytest.raises(RuntimeError, match='overwrite'):
        ops.backup(str(backup))


def test_acknowledge_is_explicit_and_never_resends(gateway, tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    script = Path(__file__).parents[1] / 'scripts' / 'ops.py'
    spec = importlib.util.spec_from_file_location('gateway_ops_2', script)
    ops = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ops)
    monkeypatch.setenv('DB_PATH', gateway.DB_PATH)
    alert_id = insert_alert(gateway, status='uncertain')
    ops.acknowledge(alert_id, 'Checked separately')
    with gateway.connect() as c:
        row = c.execute('SELECT status,attempts,last_error FROM alerts WHERE id=?', (alert_id,)).fetchone()
        assert row['status'] == 'acknowledged'
        assert row['attempts'] == 0
        assert 'Checked separately' in row['last_error']
    with pytest.raises(RuntimeError):
        ops.acknowledge(alert_id, 'Checked separately')


def test_stuck_sending_alert_causes_readiness_failure(client, gateway, monkeypatch):
    import time
    class Active:
        def done(self):
            return False
    monkeypatch.setattr(gateway, 'worker_task', Active())
    monkeypatch.setattr(gateway, 'worker_heartbeat', time.monotonic())
    insert_alert(gateway, status='sending', created=time.time() - 1100, expires_at=time.time()+200)
    assert client.get('/readyz').json()['reason'] == 'backlog_stale'
