"""A covered Food source review inside an existing Supply reminder, at most weekly."""
import asyncio
import sqlite3
import time
from contextlib import closing
from progressor_client import call,configured

def reserve(path,user_id,key,now=None):
    stamp=time.time() if now is None else now
    with closing(sqlite3.connect(path,timeout=2)) as db,db:
        db.execute('CREATE TABLE IF NOT EXISTS progressor_supply_hint_delivery(user_id INTEGER PRIMARY KEY,rule TEXT NOT NULL,presented_at REAL NOT NULL)')
        return db.execute('INSERT INTO progressor_supply_hint_delivery VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET rule=excluded.rule,presented_at=excluded.presented_at WHERE progressor_supply_hint_delivery.presented_at<?',(user_id,key,stamp,stamp-7*86400)).rowcount==1

async def prepare(path,user_id):
    if not configured():return None
    try:
        data=await call('supply','/internal/v1/bot-context',{'user_id':str(user_id)},timeout=.8)
        hint=data.get('inline_hint') or {}
        if hint.get('key') not in ('food_sources_iron.v1','food_sources_vitamin_b12.v1'):return None
        if not await asyncio.to_thread(reserve,path,user_id,hint['key']):return None
        text='\nПо записанным блюдам Food: '+hint['message']+'\nОценка состава приблизительная; пропущенные блюда неизвестны. Это обзор пищевых источников, а не вывод о дефиците организма или назначение дозы.'
        return {'text':text[:1000],'catalog':('iron' if hint['key']=='food_sources_iron.v1' else 'b12') if hint.get('secondary_action') else None}
    except Exception:return None
