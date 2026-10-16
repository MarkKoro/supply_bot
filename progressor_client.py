"""Progressor private control protocol v1; canonical copy in Core shared/.

Calls run outside domain transactions. Native features remain usable on Core outages.
Domains own their entitlement cache and successful-payment outbox.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
import tempfile
import uuid
from contextlib import closing
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlparse

FINANCIAL_SCHEDULE = 2

SCHEMA = '''CREATE TABLE IF NOT EXISTS progressor_bundle_cache(user_id TEXT PRIMARY KEY,expires_at TEXT NOT NULL,revision INTEGER NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS progressor_bundle_schedules(user_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,intervals TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS progressor_payment_outbox(receipt_id TEXT PRIMARY KEY,body TEXT NOT NULL,created_at TEXT NOT NULL,delivered INTEGER NOT NULL DEFAULT 0);'''

def canonical(value): return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)

def signing_key():
    return Path(os.environ['PROGRESSOR_SIGNING_KEY_FILE']).read_text().strip()

def control_token(source,path,body,key,now=None):
    import base64
    stamp=int(time.time()) if now is None else now
    payload={'typ':'progressor.control.v1','source':source,'path':path,'hash':hashlib.sha256(body).hexdigest(),'iat':stamp,'nonce':uuid.uuid4().hex}
    encoded=base64.urlsafe_b64encode(canonical(payload).encode()).decode().rstrip('=')
    return encoded+'.'+hmac.new(key.encode(),encoded.encode(),hashlib.sha256).hexdigest()

def verify_control(token,path,body,key,now=None):
    import base64
    if not isinstance(token,str) or len(token)>2048 or len(key)<32: raise ValueError('invalid_control')
    encoded,mac=token.split('.')
    if not hmac.compare_digest(mac,hmac.new(key.encode(),encoded.encode(),hashlib.sha256).hexdigest()): raise ValueError('invalid_control')
    payload=json.loads(base64.b64decode(encoded+'='*(-len(encoded)%4),altchars=b'-_',validate=True))
    stamp=int(time.time()) if now is None else now
    if payload.get('typ')!='progressor.control.v1' or payload.get('source') not in ('gym','food','supply') or type(payload.get('iat')) is not int or not -10<=stamp-payload['iat']<=60 or payload.get('path')!=path or payload.get('hash')!=hashlib.sha256(body).hexdigest(): raise ValueError('invalid_control')
    return payload['source']

def configured(): return os.getenv('PROGRESSOR_CLIENT_ENABLED','0')=='1'

async def call(source,path,body,timeout=1.5):
    import httpx
    if not configured(): raise RuntimeError('progressor_disabled')
    origin=os.getenv('PROGRESSOR_INTERNAL_URL','http://127.0.0.1:8040')
    parsed=urlparse(origin)
    if parsed.hostname not in ('127.0.0.1','::1') or parsed.scheme!='http' or parsed.path not in ('','/'): raise ValueError('nonlocal_progressor_url')
    raw=canonical(body).encode()
    async with httpx.AsyncClient(timeout=timeout,trust_env=False) as client:
        response=await client.post(origin.rstrip('/')+path,content=raw,headers={'Content-Type':'application/json','X-Progressor-Control':control_token(source,path,raw,signing_key())})
        response.raise_for_status()
        return response.json()

async def ai_context(source,user_id):
    try: return await call(source,'/internal/v1/ai-context',{'user_id':str(user_id)},timeout=.8)
    except Exception: return None

def parse_until(value):
    try:
        dt=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except (ValueError,TypeError): return None

def bundle_until(db_path,user_id):
    if not configured(): return None
    try:
        with closing(sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.5)) as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='progressor_bundle_schedules'").fetchone():
                schedule=conn.execute('SELECT intervals,revision FROM progressor_bundle_schedules WHERE user_id=?',(str(user_id),)).fetchone()
                cache=conn.execute('SELECT revision FROM progressor_bundle_cache WHERE user_id=?',(str(user_id),)).fetchone()
                if schedule and cache and schedule[1]==cache[0]:
                    now=datetime.now(timezone.utc)
                    values=[parse_until(r['expires_at']) for r in json.loads(schedule[0]) if parse_until(r['starts_at'])<=now<parse_until(r['expires_at'])]
                    return max(values) if values else None
            row=conn.execute('SELECT expires_at FROM progressor_bundle_cache WHERE user_id=?',(str(user_id),)).fetchone()
            value=parse_until(row[0]) if row else None
            return value if value and value>datetime.now(timezone.utc) else None
    except (sqlite3.Error,ValueError,TypeError,KeyError): return None

def ensure_schema(db_path):
    with closing(sqlite3.connect(db_path,timeout=5)) as conn:
        conn.executescript(SCHEMA); conn.commit()

def _known_schedule_projection(row,old,previous,value,intervals):
    """Heal a legacy clock-only summary at an unchanged immutable schedule.

    Earlier Core versions could retire the summary without incrementing revision.
    Local authorization already checks the versioned intervals against real time;
    this exception cannot add an interval or invent an expiry. Keep the cached
    summary unchanged and let the next actual revision replace it normally.
    """
    if not previous or previous[0]!=row['revision'] or previous[1]!=value:
        return False
    ends={parse_until(r['expires_at']) for r in intervals}
    ends.add(datetime(1970,1,1,tzinfo=timezone.utc))
    return parse_until(old[0]) in ends and parse_until(row['expires_at']) in ends

def apply_grants(db_path,items,source=None):
    with closing(sqlite3.connect(db_path,timeout=5)) as conn:
        with conn:
            for row in items:
                if not str(row['user_id']).isdecimal() or not 0<int(row['user_id'])<2**63 or not parse_until(row['expires_at']) or type(row['revision']) is not int or row['revision']<1: raise ValueError('invalid_grant')
                old=conn.execute('SELECT expires_at,revision FROM progressor_bundle_cache WHERE user_id=?',(str(row['user_id']),)).fetchone()
                expiry_conflict=bool(old and old[1]==row['revision'] and old[0]!=row['expires_at'])
                if 'intervals' in row:
                    if source and row.get('source')!=source:raise ValueError('grant_source_mismatch')
                    intervals=row['intervals']
                    if not isinstance(intervals,list) or len(intervals)>1000 or any(not parse_until(r.get('starts_at')) or not parse_until(r.get('expires_at')) or not parse_until(r['starts_at'])<parse_until(r['expires_at']) for r in intervals):raise ValueError('invalid_grant_schedule')
                    value=canonical(intervals)
                    previous=conn.execute('SELECT revision,intervals FROM progressor_bundle_schedules WHERE user_id=?',(str(row['user_id']),)).fetchone()
                    if previous and previous[0]==row['revision'] and previous[1]!=value:raise ValueError('grant_revision_conflict')
                    if expiry_conflict and not _known_schedule_projection(row,old,previous,value,intervals):
                        raise ValueError('grant_revision_conflict')
                    if not old or row['revision']>=old[1]:
                        conn.execute('INSERT INTO progressor_bundle_schedules VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET revision=excluded.revision,intervals=excluded.intervals WHERE excluded.revision>progressor_bundle_schedules.revision',(str(row['user_id']),row['revision'],value))
                elif expiry_conflict:
                    raise ValueError('grant_revision_conflict')
                conn.execute('INSERT INTO progressor_bundle_cache VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at,revision=excluded.revision,updated_at=excluded.updated_at WHERE excluded.revision>progressor_bundle_cache.revision',(str(row['user_id']),row['expires_at'],row['revision'],datetime.now(timezone.utc).isoformat()))

def record_receipt(db_path,body):
    # The trusted Telegram consumer records the receipt before any network operation.
    ensure_schema(db_path)
    receipt_id=body['kind']+':'+body['charge_id']
    value=canonical(body)
    with closing(sqlite3.connect(db_path,timeout=5)) as conn:
        with conn:
            old=conn.execute('SELECT body FROM progressor_payment_outbox WHERE receipt_id=?',(receipt_id,)).fetchone()
            if old and old[0]!=value: raise ValueError('receipt_conflict')
            conn.execute('INSERT OR IGNORE INTO progressor_payment_outbox VALUES(?,?,?,0)',(receipt_id,value,datetime.now(timezone.utc).isoformat()))

async def sync(source,db_path):
    cursor=''
    while True:
        response=await call(source,'/internal/v1/grants',{'after':cursor})
        await asyncio.to_thread(apply_grants,db_path,response['items'],source)
        if not response.get('next'): break
        cursor=response['next']

def publish_source_snapshot(db_path,destination):
    """Owner-produced online SQLite backup, atomically published without touching live rows.

    Core reads this DELETE-journal copy in a read-only mount. The owner alone opens its
    own live WAL database; Core never needs writable source directories or SHM files.
    """
    source=Path(db_path).resolve(strict=True);target=Path(destination)
    if target.is_symlink() or target.parent.is_symlink() or source==target.resolve():
        raise ValueError('unsafe_source_snapshot_path')
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    target.parent.chmod(0o700)
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(prefix='.source-',suffix='.sqlite3',dir=target.parent,delete=False) as stream:
            temporary=Path(stream.name)
        deadline=time.monotonic()+10
        def progress(status,remaining,total):
            if time.monotonic()>deadline:raise TimeoutError('source_snapshot_deadline')
        with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True,timeout=1)) as original:
            original.execute('PRAGMA query_only=ON')
            with closing(sqlite3.connect(temporary,timeout=1)) as copy:
                original.backup(copy,pages=256,progress=progress,sleep=.02)
                copy.execute('PRAGMA journal_mode=DELETE')
                if copy.execute('PRAGMA quick_check').fetchone()[0]!='ok':
                    raise ValueError('invalid_source_snapshot')
        with temporary.open('rb') as stream:os.fsync(stream.fileno())
        temporary.replace(target)
    finally:
        if temporary and temporary.exists():temporary.unlink()

async def source_snapshot_worker(source,db_path,destination,monthly_prices=None):
    while True:
        try:
            if source=='gym':
                try:
                    from progressor_free_workout import expire
                    await asyncio.to_thread(expire,db_path)
                except Exception:logging.getLogger('progressor_client').warning('free_session_finish_unavailable')
            if monthly_prices:
                from progressor_native_subscription import prepare_metadata
                await asyncio.to_thread(prepare_metadata,db_path,source,monthly_prices())
            await asyncio.to_thread(publish_source_snapshot,db_path,destination)
        except Exception:logging.getLogger('progressor_client').warning('source_snapshot_unavailable source=%s',source)
        await asyncio.sleep(30)

def install_cache_worker(app,source,db_path,monthly_prices=None):
    if not configured(): return
    from contextlib import asynccontextmanager
    original=app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(instance):
        async with original(instance):
            await asyncio.to_thread(ensure_schema,db_path)
            from progressor_links import ensure_schema as ensure_links, sync as sync_links
            await asyncio.to_thread(ensure_links,db_path)
            async def worker():
                cursor=''
                while True:
                    try: await sync(source,db_path)
                    except Exception: logging.getLogger('progressor_client').warning('bundle_sync_unavailable source=%s',source)
                    try:cursor=await sync_links(source,db_path,cursor)
                    except Exception:logging.getLogger('progressor_client').warning('connection_sync_unavailable source=%s',source)
                    await asyncio.sleep(15)
            task=asyncio.create_task(worker())
            snapshot_task=None
            if os.getenv('PROGRESSOR_SOURCE_SNAPSHOT_ENABLED','0')=='1':
                if source not in ('gym','food','supply'):raise ValueError('invalid_snapshot_source')
                root=Path('/var/lib/progressor-sources')
                if root.is_symlink():raise ValueError('unsafe_snapshot_root')
                snapshot_task=asyncio.create_task(source_snapshot_worker(source,db_path,root/source/'source.sqlite3',monthly_prices))
            try: yield
            finally:
                task.cancel()
                if snapshot_task:snapshot_task.cancel()
                try: await task
                except asyncio.CancelledError: pass
                if snapshot_task:
                    try:await snapshot_task
                    except asyncio.CancelledError:pass
    app.router.lifespan_context=lifespan
