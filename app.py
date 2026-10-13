import asyncio, hashlib, hmac, json, logging, os, re, sqlite3, time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

DB_PATH=os.getenv('DB_PATH','/data/alerts.sqlite3')
FREE_USER=os.environ['FREE_USER']
FREE_API_KEY=os.environ['FREE_API_KEY']
# Never accept example credentials as valid configuration.
if not re.fullmatch(r'\d{8}', FREE_USER):
    raise RuntimeError('FREE_USER must be an 8-digit Free Mobile subscriber login')
if not FREE_API_KEY.strip() or FREE_API_KEY.startswith(('YOUR_', 'REPLACE_')):
    raise RuntimeError('FREE_API_KEY must be a real API key, not an example placeholder')
# CLIENT_KEYS is a JSON mapping of generic client names to random secrets.
CLIENT_KEYS=json.loads(os.environ['CLIENT_KEYS'])
if (not isinstance(CLIENT_KEYS, dict) or not CLIENT_KEYS or
        any(not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,32}', name)
            or not isinstance(secret, str) or len(secret) < 32
            or secret.startswith(('YOUR_', 'REPLACE_'))
            for name, secret in CLIENT_KEYS.items()) or
        len(set(CLIENT_KEYS.values())) != len(CLIENT_KEYS)):
    raise RuntimeError('CLIENT_KEYS must contain unique 32+ character random tokens; example placeholders are forbidden')
INTERVAL=int(os.getenv('MIN_SMS_INTERVAL_SEC','60'))
DAILY=int(os.getenv('DAILY_SMS_LIMIT','100'))
PER_CLIENT_HOUR=int(os.getenv('CLIENT_HOURLY_LIMIT','20'))
DEDUP=int(os.getenv('DEDUP_WINDOW_SEC','300'))
if any(n < 1 for n in (INTERVAL, DAILY, PER_CLIENT_HOUR)) or DEDUP < 0:
    raise RuntimeError('Rate limits must be positive (DEDUP_WINDOW_SEC can be zero)')
logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
log=logging.getLogger('gateway')
lock=asyncio.Lock()

def connect():
    c=sqlite3.connect(DB_PATH,timeout=30)
    c.row_factory=sqlite3.Row
    return c

def init_db():
    Path(DB_PATH).parent.mkdir(parents=True,exist_ok=True)
    with connect() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('''CREATE TABLE IF NOT EXISTS alerts(
          id INTEGER PRIMARY KEY AUTOINCREMENT, client TEXT NOT NULL, fingerprint TEXT NOT NULL,
          severity TEXT NOT NULL, message TEXT NOT NULL, status TEXT NOT NULL,
          created REAL NOT NULL, next_attempt REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          last_error TEXT NOT NULL DEFAULT '', sent_at REAL, request_id TEXT,
          UNIQUE(client,request_id))''')
        c.execute('CREATE INDEX IF NOT EXISTS ix_alerts_work ON alerts(status,next_attempt)')
        c.execute('CREATE INDEX IF NOT EXISTS ix_alerts_dedup ON alerts(client,fingerprint,created)')

class Alert(BaseModel):
    message: str=Field(min_length=1,max_length=500)
    severity: str=Field(default='warning',pattern='^(info|warning|critical)$')
    fingerprint: str|None=Field(default=None,max_length=120)
    request_id: str|None=Field(default=None,min_length=8,max_length=120)

def authenticate(authorization: str|None)->str:
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(401,'Bearer token required')
    value=authorization[7:]
    for name,secret in CLIENT_KEYS.items():
        if hmac.compare_digest(value.encode(),str(secret).encode()):
            return name
    raise HTTPException(401,'Invalid token')

async def send_worker():
    async with httpx.AsyncClient(timeout=12) as http:
        while True:
            try:
                await process_next(http)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('Worker unexpected error')
            await asyncio.sleep(2)

async def process_next(http: httpx.AsyncClient):
    now=time.time()
    async with lock:
        with connect() as c:
            # global window quotas; delayed alerts remain on disk
            last=c.execute("SELECT MAX(sent_at) AS t FROM alerts WHERE status='sent'").fetchone()['t']
            if last and now-last<INTERVAL: return
            count=c.execute("SELECT COUNT(*) AS n FROM alerts WHERE status='sent' AND sent_at>?",(now-86400,)).fetchone()['n']
            if count>=DAILY: return
            row=c.execute("SELECT * FROM alerts WHERE status='queued' AND next_attempt<=? ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, created LIMIT 1",(now,)).fetchone()
            if not row:return
            c.execute("UPDATE alerts SET status='sending', attempts=attempts+1 WHERE id=?",(row['id'],))
    status=None
    try:
        r=await http.post('https://smsapi.free-mobile.fr/sendmsg',data={'user':FREE_USER,'pass':FREE_API_KEY,'msg':f"[{row['client']} / {row['severity'].upper()}] {row['message']}"})
        status=r.status_code
    except httpx.RequestError:
        status=None
    async with lock:
        with connect() as c:
            if status==200:
                c.execute("UPDATE alerts SET status='sent',sent_at=?,last_error='' WHERE id=?",(time.time(),row['id']))
                log.info('SMS accepted by Free id=%s client=%s',row['id'],row['client'])
            elif status in (400,403):
                c.execute("UPDATE alerts SET status='failed',last_error=? WHERE id=?",(f'HTTP {status}',row['id']))
                log.error('Free permanent failure id=%s HTTP=%s',row['id'],status)
            elif status is None:
                # Outcome unknown: retrying could create duplicate SMS; require manual resolution.
                c.execute("UPDATE alerts SET status='uncertain',last_error='Transport error: Free may have accepted SMS' WHERE id=?",(row['id'],))
                log.warning('SMS uncertain id=%s; no automatic retry',row['id'])
            else:
                if row['attempts']+1>=6:
                    c.execute("UPDATE alerts SET status='failed',last_error=? WHERE id=?",(f'HTTP {status}',row['id']))
                else:
                    delay=max(300 if status==402 else 30,min(3600,2**(row['attempts']+1)*30))
                    c.execute("UPDATE alerts SET status='queued',next_attempt=?,last_error=? WHERE id=?",(time.time()+delay,f'HTTP {status}',row['id']))
                log.warning('Free rejected id=%s HTTP=%s',row['id'],status)

@asynccontextmanager
async def lifespan(app:FastAPI):
    init_db()
    # If worker exited abnormally while sending, outcome is unknown; do not replay blindly.
    with connect() as c:
        c.execute("UPDATE alerts SET status='uncertain',last_error='Worker restarted during send' WHERE status='sending'")
    task=asyncio.create_task(send_worker())
    yield
    task.cancel()
    try:await task
    except asyncio.CancelledError:pass

app=FastAPI(title='Free SMS Gateway',docs_url=None,redoc_url=None,lifespan=lifespan)

@app.get('/healthz')
def health():return {'ok':True}

@app.post('/v1/alerts',status_code=202)
async def alert(req:Request, body:Alert, authorization:str|None=Header(default=None)):
    client=authenticate(authorization)
    if len((await req.body()))>2048:raise HTTPException(413,'Payload too large')
    now=time.time()
    fingerprint=body.fingerprint or hashlib.sha256(f'{body.severity}:{body.message}'.encode()).hexdigest()
    async with lock:
        with connect() as c:
            if body.request_id:
                row=c.execute('SELECT id,status FROM alerts WHERE client=? AND request_id=?',(client,body.request_id)).fetchone()
                if row:return {'id':row['id'],'status':row['status'],'duplicate':True}
            hourly=c.execute('SELECT COUNT(*) AS n FROM alerts WHERE client=? AND created>?',(client,now-3600)).fetchone()['n']
            if hourly>=PER_CLIENT_HOUR:raise HTTPException(429,'Hourly client quota exceeded')
            existing=c.execute("SELECT id,status FROM alerts WHERE client=? AND fingerprint=? AND created>? ORDER BY created DESC LIMIT 1",(client,fingerprint,now-DEDUP)).fetchone()
            if existing:return {'id':existing['id'],'status':existing['status'],'deduplicated':True}
            c.execute('INSERT INTO alerts(client,fingerprint,severity,message,status,created,next_attempt,request_id) VALUES(?,?,?,?,?,?,?,?)',
                      (client,fingerprint,body.severity,body.message,'queued',now,now,body.request_id))
            id=c.execute('SELECT last_insert_rowid() AS id').fetchone()['id']
            return {'id':id,'status':'queued'}

@app.get('/v1/alerts/{alert_id}')
def get_alert(alert_id:int,authorization:str|None=Header(default=None)):
    client=authenticate(authorization)
    with connect() as c:
        row=c.execute('SELECT id,status,severity,created,sent_at,attempts,last_error FROM alerts WHERE id=? AND client=?',(alert_id,client)).fetchone()
    if not row:raise HTTPException(404,'Not found')
    return dict(row)
