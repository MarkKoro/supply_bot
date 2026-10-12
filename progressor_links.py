"""Source-owned settings cache and idempotent confirmed profile commands.

Canonical in Core shared/. Mirror as progressor_links.py in each source.
Only an owner's worker writes its own DB. Network calls stay outside transactions.
"""
import asyncio
import json
import math
import sqlite3
import time
from contextlib import closing
from datetime import datetime,timezone

SCHEMA='''CREATE TABLE IF NOT EXISTS progressor_link_cache(
 user_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,body TEXT NOT NULL,updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS progressor_owner_commands(
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL,status TEXT NOT NULL,result TEXT NOT NULL,created_at REAL NOT NULL);
'''

def ensure_schema(path):
    with closing(sqlite3.connect(path,timeout=5)) as db:db.executescript(SCHEMA);db.commit()

def cache_settings(path,items):
    with closing(sqlite3.connect(path,timeout=5)) as db,db:
        for item in items:
            uid=str(item['user_id']);p=item['preferences']
            if not uid.isascii() or not uid.isdecimal() or not 0<int(uid)<2**63 or type(p['revision']) is not int:raise ValueError('invalid_link_cache')
            db.execute('INSERT INTO progressor_link_cache VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET revision=excluded.revision,body=excluded.body,updated_at=excluded.updated_at WHERE excluded.revision>=progressor_link_cache.revision',
                       (uid,p['revision'],json.dumps(p,ensure_ascii=False,separators=(',',':')),time.time()))

def settings(path,user_id):
    try:
        with closing(sqlite3.connect(path,timeout=1)) as db:
            row=db.execute('SELECT body FROM progressor_link_cache WHERE user_id=?',(str(user_id),)).fetchone()
        return json.loads(row[0]) if row else {}
    except (sqlite3.Error,ValueError,TypeError):return {}

def resting(path,user_id):
    p=settings(path,user_id)
    return bool(p.get('paused') and (p.get('pause_until') is None or p['pause_until']>time.time()))

def notifications_allowed(path,user_id):
    p=settings(path,user_id)
    return not resting(path,user_id) and p.get('notifications',True)

def day_goal(path,user_id,base,day=None):
    from datetime import date
    try:
        from progressor_day_goal import goal_key
        p=settings(path,user_id);rhythm=p.get('rhythm');selected=date.fromisoformat(day) if day else datetime.now(timezone.utc).astimezone(__import__('zoneinfo').ZoneInfo('Europe/Moscow')).date()
        if (not rhythm or resting(path,user_id) or not all(p.get('sources',{}).get(s,False) for s in ('food','gym'))
                or time.time()-p.get('synced_at',0)>120 or rhythm['expires_at']<=time.time()
                or goal_key(base)!=rhythm['base_goal_key']):return base
        if selected<datetime.fromtimestamp(rhythm['confirmed_at'],__import__('zoneinfo').ZoneInfo('Europe/Moscow')).date():return base
        return {**dict(base),**rhythm['days'][selected.weekday()]}
    except (KeyError,TypeError,ValueError,ImportError):return base

def own_people(path,source,after='',limit=100):
    column={'gym':'tg_id','food':'telegram_id','supply':'tg_user_id'}[source]
    with closing(sqlite3.connect(path,timeout=2)) as db:
        return [str(r[0]) for r in db.execute(f'SELECT {column} FROM users WHERE {column}>? ORDER BY {column} LIMIT ?',
                                            (int(after or 0),limit)) if type(r[0]) is int and 0<r[0]<2**63]

def _instant(value):
    if not value:return None
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None:parsed=parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

def apply_weight(path,source,command):
    uid=str(command['user_id']);body=command['body'];weight=body['weight_kg']
    if (body.get('kind')!='confirmed_weight' or not uid.isascii() or not uid.isdecimal() or not 0<int(uid)<2**63
            or type(weight) not in (int,float) or not math.isfinite(weight) or not 30<=weight<=350):raise ValueError('invalid_owner_command')
    with closing(sqlite3.connect(path,timeout=5)) as db,db:
        db.row_factory=sqlite3.Row;db.execute('BEGIN IMMEDIATE')
        old=db.execute('SELECT user_id,result FROM progressor_owner_commands WHERE id=?',(command['id'],)).fetchone()
        if old:
            if old['user_id']!=uid:raise ValueError('command_subject_changed')
            return json.loads(old['result'])
        result={'id':command['id'],'user_id':uid,'status':'applied','weight_kg':weight}
        column={'gym':'tg_id','food':'telegram_id','supply':'tg_user_id'}[source]
        user=db.execute(f'SELECT * FROM users WHERE {column}=?',(int(uid),)).fetchone()
        current=None;stamp=None;profile=None
        if command['expires_at']<=time.time():result['status']='expired'
        elif not user:result['status']='missing_owner'
        elif source=='supply' and (not user['consent_given'] or user['is_banned'] or user['deleted_at_utc']):result['status']='consent_required'
        else:
            if source=='gym':
                profile=db.execute("SELECT * FROM user_measurements WHERE user_id=? AND metric='body_weight' ORDER BY created_at DESC,id DESC LIMIT 1",(user['id'],)).fetchone()
                if profile:current=profile['value'];stamp=profile['created_at']
            elif source=='food':
                profile=db.execute('SELECT * FROM progressor_food_profile WHERE user_id=?',(int(uid),)).fetchone()
                if profile:current=json.loads(profile['profile_json']).get('weight_kg');stamp=profile['updated_at']
            else:
                profile=db.execute('SELECT * FROM user_health_profile WHERE user_id=?',(user['id'],)).fetchone()
                if profile:current=profile['weight_kg'];stamp=profile['updated_at_utc'] or profile['created_at_utc']
            expected=body.get('expected')
            same=type(current) in (int,float) and abs(current-weight)<.0001
            guard=(current is None and expected is None) or (expected is not None and current is not None
                and abs(float(current)-float(expected['value']))<.0001 and _instant(stamp)==_instant(expected['observed_at']))
            if not same and not guard:
                result.update(status='conflict',reason='Профиль изменился после выбора. Обновите данные и подтвердите ещё раз.')
                result['weight_kg']=current
            elif not same:
                now=datetime.now(timezone.utc).isoformat()
                if source=='gym':
                    db.execute("INSERT INTO user_measurements(user_id,metric,value,extra,created_at) VALUES(?,'body_weight',?,?,?)",
                               (user['id'],weight,json.dumps({'source':'confirmed_profile','operation_id':command['id']}),now))
                elif source=='food':
                    values=json.loads(profile['profile_json']) if profile else {};values['weight_kg']=weight
                    db.execute('INSERT INTO progressor_food_profile VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET profile_json=excluded.profile_json,updated_at=excluded.updated_at',
                               (int(uid),json.dumps(values,ensure_ascii=False),now,profile['sharing_enabled'] if profile else 0))
                else:
                    if profile:db.execute('UPDATE user_health_profile SET weight_kg=?,updated_at_utc=? WHERE user_id=?',(weight,now,user['id']))
                    else:db.execute('INSERT INTO user_health_profile(user_id,weight_kg,created_at_utc,updated_at_utc) VALUES(?,?,?,?)',(user['id'],weight,now,now))
        db.execute('INSERT INTO progressor_owner_commands VALUES(?,?,?,?,?)',
                   (command['id'],uid,result['status'],json.dumps(result,ensure_ascii=False),time.time()))
        return result

async def sync(source,path,after=''):
    from progressor_client import call
    people=await asyncio.to_thread(own_people,path,source,after)
    response=await call(source,'/internal/v1/connections/sync',{'people':people},timeout=3)
    await asyncio.to_thread(cache_settings,path,response['settings'])
    if source=='food':
        def weekly_sender_rows():
            with closing(sqlite3.connect(path,timeout=5)) as db,db:
                for item in response['settings']:
                    if item['preferences'].get('weekly_plan') or item['preferences'].get('weight_notifications'):
                        db.execute('INSERT OR IGNORE INTO progressor_food_preferences VALUES(?,0,0,\'Europe/Moscow\',?,?)',(int(item['user_id']),time.time(),time.time()))
        await asyncio.to_thread(weekly_sender_rows)
    acks=[]
    for command in response['commands']:
        acks.append(await asyncio.to_thread(apply_weight,path,source,command))
    if acks:
        await call(source,'/internal/v1/connections/sync',{'people':[],'acks':acks},timeout=3)
    return people[-1] if len(people)==100 else ''

def purge(path,user_id):
    with closing(sqlite3.connect(path,timeout=5)) as db,db:
        for table in ('progressor_link_cache','progressor_owner_commands'):
            if db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone():
                db.execute(f'DELETE FROM {table} WHERE user_id=?',(str(user_id),))
