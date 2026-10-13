"""Private, single-worker SMS gateway for Free Mobile subscriber notifications."""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

DB_PATH = os.getenv('DB_PATH', '/data/alerts.sqlite3')
FREE_USER = os.environ['FREE_USER']
FREE_API_KEY = os.environ['FREE_API_KEY']
if not re.fullmatch(r'\d{8}', FREE_USER):
    raise RuntimeError('FREE_USER must be an 8-digit Free Mobile subscriber login')
if not FREE_API_KEY.strip() or FREE_API_KEY.startswith(('YOUR_', 'REPLACE_')):
    raise RuntimeError('FREE_API_KEY must not be an example placeholder')
CLIENT_KEYS = json.loads(os.environ['CLIENT_KEYS'])
if (not isinstance(CLIENT_KEYS, dict) or not CLIENT_KEYS or
        any(not isinstance(n, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,32}', n)
                or not isinstance(k, str) or len(k) < 32 or k.startswith(('YOUR_', 'REPLACE_'))
                for n, k in CLIENT_KEYS.items()) or
        len(set(CLIENT_KEYS.values())) != len(CLIENT_KEYS)):
    raise RuntimeError('CLIENT_KEYS must use distinct, random tokens >= 32 characters')


def positive_int(name, default, lower=1, upper=604800):
    value = int(os.getenv(name, str(default)))
    if not lower <= value <= upper:
        raise RuntimeError(f'{name} must be between {lower} and {upper}')
    return value


INTERVAL = positive_int('MIN_SMS_INTERVAL_SEC', 60)
DAILY = positive_int('DAILY_SMS_LIMIT', 100)
PER_CLIENT_HOUR = positive_int('CLIENT_HOURLY_LIMIT', 20)
DEDUP = positive_int('DEDUP_WINDOW_SEC', 300, lower=0)
ALERT_TTL = positive_int('DEFAULT_ALERT_TTL_SEC', 3600, lower=60)
MAX_QUEUE = positive_int('MAX_PENDING_ALERTS', 1000)
RETENTION_DAYS = positive_int('RETENTION_DAYS', 30, upper=365)
MAX_BACKLOG_AGE = positive_int('MAX_BACKLOG_AGE_SEC', 600, lower=60)
MAX_ATTEMPTS = positive_int('MAX_SEND_ATTEMPTS', 6, upper=20)
log = logging.getLogger('gateway')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
# SQLite operations are serialized within one Uvicorn process. Explicit transactions
# also protect the claims if multiple processes inadvertently share the volume.
lock = asyncio.Lock()
worker_task = None
worker_heartbeat = 0.0


def connect():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA busy_timeout=10000')
    return c


def init_db():
    os.umask(0o077)  # New SQLite database and WAL files must not be world-readable.
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with connect() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('''CREATE TABLE IF NOT EXISTS alerts(
            id INTEGER PRIMARY KEY AUTOINCREMENT, client TEXT NOT NULL,
            fingerprint TEXT NOT NULL, severity TEXT NOT NULL, message TEXT NOT NULL,
            status TEXT NOT NULL, created REAL NOT NULL, next_attempt REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
            sent_at REAL, request_id TEXT, expires_at REAL,
            UNIQUE(client, request_id))''')
        # Online migration from v0.1; do not replace the existing database.
        cols = {r['name'] for r in c.execute('PRAGMA table_info(alerts)')}
        if 'expires_at' not in cols:
            c.execute('ALTER TABLE alerts ADD COLUMN expires_at REAL')
            # Legacy queued events should not be silently delivered days later.
            c.execute('UPDATE alerts SET expires_at=created+? WHERE expires_at IS NULL', (ALERT_TTL,))
        c.execute('''CREATE TABLE IF NOT EXISTS delivery_attempts(
            id INTEGER PRIMARY KEY AUTOINCREMENT, alert_id INTEGER NOT NULL,
            started REAL NOT NULL, completed REAL, result TEXT)''')
        c.execute('CREATE INDEX IF NOT EXISTS ix_attempt_started ON delivery_attempts(started)')
        c.execute('CREATE INDEX IF NOT EXISTS ix_alerts_work ON alerts(status,next_attempt)')
        c.execute('CREATE INDEX IF NOT EXISTS ix_alerts_dedup ON alerts(client,fingerprint,created)')
        # A crash during an HTTP request has an ambiguous delivery outcome.
        c.execute("UPDATE alerts SET status='uncertain', last_error='Worker restarted during send' WHERE status='sending'")
        c.execute("UPDATE delivery_attempts SET result='uncertain_restart' WHERE result='sending'")


class Alert(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    severity: str = Field(default='warning', pattern='^(info|warning|critical)$')
    fingerprint: str | None = Field(default=None, max_length=120)
    request_id: str | None = Field(default=None, min_length=8, max_length=120)
    ttl_seconds: int | None = Field(default=None, ge=60, le=604800)


def authenticate(authorization: str | None) -> str:
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(401, 'Bearer token required')
    supplied = authorization[7:]
    for name, secret in CLIENT_KEYS.items():
        if hmac.compare_digest(supplied.encode(), secret.encode()):
            return name
    raise HTTPException(401, 'Invalid token')


def housekeeping(c, now):
    c.execute("UPDATE alerts SET status='expired',last_error='TTL exceeded' "
              "WHERE status='queued' AND expires_at IS NOT NULL AND expires_at<=?", (now,))
    cutoff = now - RETENTION_DAYS * 86400
    # Keep queued/sending alerts even if old; uncertainty requires manual review.
    c.execute("DELETE FROM alerts WHERE status IN ('sent','failed','expired','acknowledged') AND created<?", (cutoff,))
    c.execute('DELETE FROM delivery_attempts WHERE started<?', (cutoff,))


async def process_next(http):
    now = time.time()
    async with lock:
        with connect() as c:
            c.execute('BEGIN IMMEDIATE')
            housekeeping(c, now)
            last = c.execute('SELECT MAX(started) AS t FROM delivery_attempts').fetchone()['t']
            if last is not None and now - last < INTERVAL:
                return
            count = c.execute('SELECT COUNT(*) AS n FROM delivery_attempts WHERE started>?',
                              (now - 86400,)).fetchone()['n']
            if count >= DAILY:
                return
            row = c.execute("""SELECT * FROM alerts WHERE status='queued' AND next_attempt<=?
                ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                         created LIMIT 1""", (now,)).fetchone()
            if row is None:
                return
            c.execute("UPDATE alerts SET status='sending',attempts=attempts+1 WHERE id=?", (row['id'],))
            c.execute("INSERT INTO delivery_attempts(alert_id,started,result) VALUES(?,?,'sending')",
                      (row['id'], now))
            attempt_id = c.execute('SELECT last_insert_rowid() AS id').fetchone()['id']

    # The only place where a real SMS HTTP request is performed.
    status = None
    try:
        response = await http.post('https://smsapi.free-mobile.fr/sendmsg', data={
            'user': FREE_USER,
            'pass': FREE_API_KEY,
            'msg': f"[{row['client']} / {row['severity'].upper()}] {row['message']}",
        })
        status = response.status_code
    except httpx.RequestError:
        # A timeout or connection loss may happen AFTER acceptance by Free.
        pass

    async with lock:
        with connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('UPDATE delivery_attempts SET completed=?,result=? WHERE id=?',
                      (time.time(), f'http_{status}' if status is not None else 'network_uncertain', attempt_id))
            if status == 200:
                c.execute("UPDATE alerts SET status='sent',sent_at=?,last_error='' WHERE id=?",
                          (time.time(), row['id']))
                log.info('Free accepted SMS alert_id=%s client=%s', row['id'], row['client'])
            elif status in (400, 401, 403):
                c.execute('UPDATE alerts SET status=?,last_error=? WHERE id=?',
                          ('failed', f'HTTP {status}', row['id']))
                log.error('Free rejected SMS alert_id=%s HTTP=%s', row['id'], status)
            elif status in (402, 429):
                # Explicit rate-limit rejection: a bounded retry is reasonable.
                next_at = time.time() + min(3600, max(300, 60 * (2 ** min(row['attempts'], 5))))
                if row['attempts'] + 1 >= MAX_ATTEMPTS:
                    c.execute('UPDATE alerts SET status=?,last_error=? WHERE id=?',
                              ('failed', f'HTTP {status}: retries exhausted', row['id']))
                else:
                    c.execute('UPDATE alerts SET status=?,next_attempt=?,last_error=? WHERE id=?',
                              ('queued', next_at, f'HTTP {status}', row['id']))
                log.warning('Free rate limited SMS alert_id=%s HTTP=%s', row['id'], status)
            else:
                # HTTP 5xx, unexpected 4xx or transport errors are ambiguous:
                # a retry might duplicate an accepted message.
                err = f'HTTP {status} (outcome unknown)' if status is not None else 'Transport error (outcome unknown)'
                c.execute('UPDATE alerts SET status=?,last_error=? WHERE id=?',
                          ('uncertain', err, row['id']))
                log.warning('SMS outcome uncertain alert_id=%s', row['id'])


async def send_worker():
    global worker_heartbeat
    async with httpx.AsyncClient(timeout=12) as http:
        while True:
            worker_heartbeat = time.monotonic()
            try:
                await process_next(http)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('Worker processing failure')
            await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global worker_task, worker_heartbeat
    init_db()
    worker_heartbeat = time.monotonic()
    worker_task = asyncio.create_task(send_worker())
    try:
        yield
    finally:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        worker_task = None


app = FastAPI(title='Free SMS Gateway', docs_url=None, redoc_url=None, lifespan=lifespan)


@app.get('/healthz')
def health():
    # Liveness only; does not guarantee the worker or SMS delivery.
    return {'ok': True}


@app.get('/readyz')
def ready():
    # Expose only to localhost or an authenticated private reverse proxy.
    if worker_task is None or worker_task.done() or time.monotonic() - worker_heartbeat > 30:
        return JSONResponse(status_code=503, content={'ready': False, 'reason': 'worker_unhealthy'})
    try:
        with connect() as c:
            c.execute('SELECT 1').fetchone()
            earliest = c.execute("SELECT MIN(created) AS oldest FROM alerts WHERE status IN ('queued','sending')").fetchone()['oldest']
            critical = c.execute("SELECT COUNT(*) AS n FROM alerts WHERE severity='critical' AND "
                                 "status IN ('failed','uncertain','expired') AND created>?",
                                 (time.time() - RETENTION_DAYS * 86400,)).fetchone()['n']
        if earliest is not None and time.time() - earliest > MAX_BACKLOG_AGE:
            return JSONResponse(status_code=503, content={'ready': False, 'reason': 'backlog_stale'})
        if critical:
            return JSONResponse(status_code=503, content={'ready': False, 'reason': 'critical_delivery_unresolved'})
    except sqlite3.Error:
        return JSONResponse(status_code=503, content={'ready': False, 'reason': 'database_unhealthy'})
    return {'ready': True}


@app.post('/v1/alerts', status_code=202)
async def alert(body: Alert, authorization: str | None = Header(default=None)):
    client = authenticate(authorization)
    now = time.time()
    fingerprint = body.fingerprint or hashlib.sha256(
        f'{body.severity}:{body.message}'.encode()).hexdigest()
    async with lock:
        with connect() as c:
            c.execute('BEGIN IMMEDIATE')
            housekeeping(c, now)
            if body.request_id:
                old = c.execute('SELECT id,status FROM alerts WHERE client=? AND request_id=?',
                                (client, body.request_id)).fetchone()
                if old:
                    return {'id': old['id'], 'status': old['status'], 'duplicate': True}
            existing = c.execute('SELECT id,status FROM alerts WHERE client=? AND fingerprint=? '
                                 'AND created>? ORDER BY created DESC LIMIT 1',
                                 (client, fingerprint, now - DEDUP)).fetchone() if DEDUP else None
            if existing:
                return {'id': existing['id'], 'status': existing['status'], 'deduplicated': True}
            hourly = c.execute('SELECT COUNT(*) AS n FROM alerts WHERE client=? AND created>?',
                               (client, now - 3600)).fetchone()['n']
            if hourly >= PER_CLIENT_HOUR:
                raise HTTPException(429, 'Hourly client quota exceeded')
            pending = c.execute("SELECT COUNT(*) AS n FROM alerts WHERE status IN ('queued','sending')").fetchone()['n']
            if pending >= MAX_QUEUE:
                raise HTTPException(503, 'Queue capacity reached')
            c.execute('INSERT INTO alerts(client,fingerprint,severity,message,status,created,next_attempt,request_id,expires_at) '
                      'VALUES(?,?,?,?,?,?,?,?,?)',
                      (client, fingerprint, body.severity, body.message, 'queued', now, now,
                       body.request_id, now + (body.ttl_seconds or ALERT_TTL)))
            alert_id = c.execute('SELECT last_insert_rowid() AS id').fetchone()['id']
            return {'id': alert_id, 'status': 'queued'}


@app.get('/v1/alerts/{alert_id}')
def get_alert(alert_id: int, authorization: str | None = Header(default=None)):
    client = authenticate(authorization)
    with connect() as c:
        row = c.execute('SELECT id,status,severity,created,sent_at,attempts,last_error,expires_at '
                        'FROM alerts WHERE id=? AND client=?', (alert_id, client)).fetchone()
    if not row:
        raise HTTPException(404, 'Not found')
    return dict(row)
