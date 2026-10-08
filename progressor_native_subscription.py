"""Owner-local financial coverage. Never infer a purchase from an access flag.

Canonical in Core/shared; mirrored to each bot. These tables are exported only in
the existing private snapshot. A gift/trial/admin extension is not a cash credit.
"""
import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone

GRANTS = '''CREATE TABLE IF NOT EXISTS progressor_native_paid_grants(
grant_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,starts_at TEXT NOT NULL,
expires_at TEXT NOT NULL,currency TEXT NOT NULL,amount INTEGER NOT NULL,
status TEXT NOT NULL DEFAULT 'active')'''
TARIFFS = '''CREATE TABLE IF NOT EXISTS progressor_native_tariffs(
source TEXT PRIMARY KEY,body TEXT NOT NULL,updated_at REAL NOT NULL)'''
ACTIVITY = '''CREATE TABLE IF NOT EXISTS progressor_bot_activity(
user_id TEXT NOT NULL,day TEXT NOT NULL,count INTEGER NOT NULL,last_at TEXT NOT NULL,
last_update INTEGER NOT NULL,PRIMARY KEY(user_id,day))'''

def instant(value):
    dt = value if isinstance(value,datetime) else datetime.fromisoformat(str(value).replace('Z','+00:00'))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)

def values(grant_id,user_id,starts_at,expires_at,currency,amount):
    start,end=instant(starts_at),instant(expires_at)
    if not str(user_id).isdecimal() or not 0<int(user_id)<2**63 or currency not in ('RUB','XTR') or type(amount) is not int or amount<=0 or not start<end:
        raise ValueError('invalid_paid_coverage')
    return str(grant_id),str(user_id),start.isoformat(),end.isoformat(),currency,amount

def record_paid(conn,*args):
    conn.execute(GRANTS)
    row=values(*args)
    old=conn.execute('SELECT grant_id,user_id,starts_at,expires_at,currency,amount FROM progressor_native_paid_grants WHERE grant_id=?',(row[0],)).fetchone()
    if old and tuple(old)!=row:raise ValueError('native_grant_conflict')
    conn.execute('INSERT OR IGNORE INTO progressor_native_paid_grants(grant_id,user_id,starts_at,expires_at,currency,amount) VALUES(?,?,?,?,?,?)',row)

async def record_paid_async(conn,*args):
    await conn.execute(GRANTS)
    row=values(*args)
    cursor=await conn.execute('SELECT grant_id,user_id,starts_at,expires_at,currency,amount FROM progressor_native_paid_grants WHERE grant_id=?',(row[0],))
    old=await cursor.fetchone()
    if old and tuple(old)!=row:raise ValueError('native_grant_conflict')
    await conn.execute('INSERT OR IGNORE INTO progressor_native_paid_grants(grant_id,user_id,starts_at,expires_at,currency,amount) VALUES(?,?,?,?,?,?)',row)

def _legacy_food(db):
    # Replay only verified receipts, and accept only an exact reconciliation with
    # the native expiry. Extra gifted days make the reconstruction ambiguous.
    plans={'w1':7,'m1':30,'m3':90,'m6':180,'y1':365}
    for user in db.execute('SELECT telegram_id,premium_until FROM users WHERE premium_until IS NOT NULL'):
        rows=[];end=None
        for p in db.execute("SELECT * FROM payments WHERE user_id=? AND (COALESCE(telegram_charge_id,'')<>'' OR COALESCE(provider_charge_id,'')<>'') ORDER BY ts,id",(user['telegram_id'],)):
            payload=p['payload'] or '';plan=None;days=None
            if payload.startswith('food:'):
                order=db.execute("SELECT plan_id,days,kind,status FROM payment_orders WHERE id=?",(payload[5:],)).fetchone()
                if order and order['kind']=='subscription' and order['status']=='paid':days=order['days']
            elif payload.startswith(('sub:','sub_stars:')):
                plan=payload.split(':')[1]
            else:
                try:
                    meta=json.loads(payload)
                    if meta.get('test') is True or meta.get('kind') not in (None,'subscription','sub'):continue
                    plan=meta.get('plan_key') or meta.get('plan_id')
                except (ValueError,AttributeError):pass
            days=days or plans.get(plan)
            currency=p['currency'] or ('XTR' if p['amount_stars'] else 'RUB')
            amount=p['amount_stars'] if currency=='XTR' else p['amount_rub']*100
            if not days or not amount:continue
            paid=instant(p['payment_succeeded_at']) if p['payment_succeeded_at'] else datetime.fromtimestamp(p['ts'],timezone.utc)
            start=max(end,paid) if end else paid;end=start+timedelta(days=days)
            rows.append(('food:'+str(p['id']),user['telegram_id'],start,end,currency,amount))
        if end and abs((instant(user['premium_until'])-end).total_seconds())<=120:
            for row in rows:record_paid(db,*row)

def _legacy_gym(db):
    for p in db.execute("SELECT p.*,u.tg_id FROM payments p JOIN users u ON u.id=p.user_id WHERE p.provider IN ('telegram','yookassa') AND p.external_id IS NOT NULL AND p.status IN ('paid','succeeded','paid_invoice') AND p.finalized_at IS NOT NULL AND p.amount>0 AND p.currency IN ('RUB','XTR')"):
        try:
            meta=json.loads(p['payload_json'] or '{}')
            if meta.get('test') is True:continue
            provider_meta=meta.get('metadata') or {}
            if provider_meta.get('kind') not in (None,'subscription'):continue
            if provider_meta.get('user_id') is not None and str(provider_meta['user_id'])!=str(p['user_id']):continue
            plan=meta.get('plan_code') or meta.get('plan') or provider_meta.get('plan_id')
            days=int(meta.get('granted_days') or {'1m':30,'6m':180,'12m':365}.get(plan,0))
            if not 0<days<=36600:continue
        except (ValueError,KeyError,TypeError):continue
        candidates=[r for r in db.execute('SELECT started_at,expires_at FROM subscriptions WHERE user_id=? AND plan=?',(p['user_id'],plan)) if abs((instant(r['started_at'])-instant(p['finalized_at'])).total_seconds())<10]
        if len(candidates)!=1:continue
        end=instant(candidates[0]['expires_at']);start=end-timedelta(days=days)
        amount=int(p['amount'])
        record_paid(db,'gym:'+str(p['id']),p['tg_id'],start,end,p['currency'],amount)

def _legacy_supply(db):
    import calendar
    for user in db.execute('SELECT id,tg_user_id,premium_until_utc FROM users WHERE premium_until_utc IS NOT NULL'):
        rows=[];end=None
        for p in db.execute("SELECT * FROM payments WHERE user_id=? AND status='paid' AND amount>0 AND (COALESCE(telegram_payment_charge_id,'')<>'' OR COALESCE(provider_payment_charge_id,'')<>'') ORDER BY created_at_utc,id",(user['id'],)):
            try:
                plan=str(p['plan_id']);months=int(plan[1:]) if plan in ('m1','m3','m6','m12') else None
                if not months:continue
                paid=instant(p['created_at_utc']);start=max(end,paid) if end else paid
                year,month=divmod(start.year*12+start.month-1+months,12);month+=1
                end=start.replace(year=year,month=month,day=min(start.day,calendar.monthrange(year,month)[1]))
                rows.append(('supply:'+str(p['id']),user['tg_user_id'],start,end,p['currency'],int(p['amount'])))
            except (ValueError,TypeError):continue
        if end and abs((instant(user['premium_until_utc'])-end).total_seconds())<=120:
            for row in rows:record_paid(db,*row)

def prepare_metadata(path,source,monthly_prices):
    if source not in ('gym','food','supply') or any(type(monthly_prices.get(c)) is not int or not 1<=monthly_prices[c]<=10000000 for c in ('RUB','XTR')):
        raise ValueError('invalid_native_tariffs')
    with closing(sqlite3.connect(path,timeout=5)) as db:
        db.row_factory=sqlite3.Row
        with db:
            for statement in (GRANTS,TARIFFS,ACTIVITY):db.execute(statement)
            # Once per owner startup; ongoing purchases record their exact grant
            # inside the payment transaction. Never repeatedly rewrite old proof.
            if not db.execute('SELECT 1 FROM progressor_native_tariffs WHERE source=?',(source,)).fetchone():
                globals()['_legacy_'+source](db)
            db.execute('INSERT INTO progressor_native_tariffs VALUES(?,?,?) ON CONFLICT(source) DO UPDATE SET body=excluded.body,updated_at=excluded.updated_at',(source,json.dumps(monthly_prices,sort_keys=True),time.time()))
            # Revocation is source-owned: a refunded/deleted payment cannot remain
            # a credit just because a historical grant row exists.
            status=" WHERE status IN ('paid','succeeded','paid_invoice')" if source=='gym' else " WHERE status='paid'" if source=='supply' else ''
            paid_ids={str(r[0]) for r in db.execute('SELECT id FROM payments'+status)}
            for grant in db.execute("SELECT grant_id FROM progressor_native_paid_grants WHERE status='active'"):
                if grant[0].split(':',1)[-1] not in paid_ids:db.execute("UPDATE progressor_native_paid_grants SET status='revoked' WHERE grant_id=?",(grant[0],))

def record_activity(path,user_id,update_id,now=None):
    now=instant(now or datetime.now(timezone.utc));day=now.date().isoformat()
    with closing(sqlite3.connect(path,timeout=2)) as db:
        with db:
            db.execute(ACTIVITY)
            db.execute('INSERT INTO progressor_bot_activity VALUES(?,?,1,?,?) ON CONFLICT(user_id,day) DO UPDATE SET count=count+1,last_at=excluded.last_at,last_update=excluded.last_update WHERE excluded.last_update>progressor_bot_activity.last_update',(str(user_id),day,now.isoformat(),int(update_id)))
            db.execute('DELETE FROM progressor_bot_activity WHERE day<?',((now-timedelta(days=32)).date().isoformat(),))

def menu_anchor(path,chat_id,message_id=None):
    """Durable native Gym anchor; no Telegram/user message contents are stored."""
    try:
        with closing(sqlite3.connect(path,timeout=.2)) as db:
            if message_id is not None:
                with db:
                    db.execute('CREATE TABLE IF NOT EXISTS progressor_menu_anchor(chat_id INTEGER PRIMARY KEY,message_id INTEGER NOT NULL)')
                    db.execute('INSERT INTO progressor_menu_anchor VALUES(?,?) ON CONFLICT(chat_id) DO UPDATE SET message_id=excluded.message_id',(int(chat_id),int(message_id)))
            else:
                row=db.execute('SELECT message_id FROM progressor_menu_anchor WHERE chat_id=?',(int(chat_id),)).fetchone()
                return row[0] if row else None
    except sqlite3.Error:return None
