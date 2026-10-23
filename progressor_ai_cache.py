"""Bounded source-owned AI response cache. Authorization/attempt checks precede it.

Only exact request inputs on unchanged authorized context match. Transient clock
labels are excluded; observations, consent/rights, profile/goal and prompt remain.
No raw images, prompts, tokens or another account's values are stored as keys.
"""
import hashlib,json,sqlite3,time
from contextlib import closing
SCHEMA='''CREATE TABLE IF NOT EXISTS progressor_ai_cache(
 user_id TEXT NOT NULL,scope TEXT NOT NULL,key TEXT NOT NULL,body TEXT NOT NULL,
 created_at REAL NOT NULL,expires_at REAL NOT NULL,PRIMARY KEY(user_id,scope,key));
CREATE INDEX IF NOT EXISTS progressor_ai_cache_expiry ON progressor_ai_cache(expires_at);'''
TRANSIENT=frozenset(('generated_at','last_success','synced_at','request_correlation_id'))

def stable(value):
    if isinstance(value,dict):return {k:stable(v) for k,v in value.items() if k not in TRANSIENT}
    if isinstance(value,list):return [stable(v) for v in value]
    return value

def key(inputs):
    return hashlib.sha256(json.dumps({'cache_schema':1,'inputs':stable(inputs)},sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def get(path,user_id,scope,request_key,now=None):
    stamp=time.time() if now is None else now
    try:
        with closing(sqlite3.connect(path,timeout=2)) as db:
            row=db.execute('SELECT body FROM progressor_ai_cache WHERE user_id=? AND scope=? AND key=? AND expires_at>?',(str(user_id),scope,request_key,stamp)).fetchone()
        return json.loads(row[0]) if row else None
    except (sqlite3.Error,ValueError,TypeError):return None

def put(path,user_id,scope,request_key,result,ttl=3600,now=None,*,publish_if=None):
    """Optionally fence publication by a trusted owner predicate in this write.

    The predicate reads this same connection under BEGIN IMMEDIATE, before
    expiry cleanup/eviction/insertion. It must be local, synchronous and free
    of writes or network calls. Existing callers without it keep their path.
    """
    stamp=time.time() if now is None else now
    if not 60<=ttl<=21600 or len(request_key)!=64:raise ValueError('invalid_cache_entry')
    body=json.dumps(result,ensure_ascii=False,separators=(',',':'),allow_nan=False)
    if len(body.encode())>131072:return False
    try:
        with closing(sqlite3.connect(path,timeout=2)) as db,db:
            db.executescript(SCHEMA);db.execute('BEGIN IMMEDIATE')
            if publish_if is not None and not publish_if(db):return False
            db.execute('DELETE FROM progressor_ai_cache WHERE expires_at<=?',(stamp,))
            db.execute('INSERT OR REPLACE INTO progressor_ai_cache VALUES(?,?,?,?,?,?)',(str(user_id),scope,request_key,body,stamp,stamp+ttl))
            db.execute('DELETE FROM progressor_ai_cache WHERE rowid IN (SELECT rowid FROM progressor_ai_cache WHERE user_id=? ORDER BY created_at DESC LIMIT -1 OFFSET 12)',(str(user_id),))
            db.execute('DELETE FROM progressor_ai_cache WHERE rowid IN (SELECT rowid FROM progressor_ai_cache ORDER BY created_at DESC LIMIT -1 OFFSET 512)')
    except sqlite3.Error:return False
    return True

def purge(path,user_id):
    with closing(sqlite3.connect(path,timeout=3)) as db,db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='progressor_ai_cache'").fetchone():db.execute('DELETE FROM progressor_ai_cache WHERE user_id=?',(str(user_id),))
