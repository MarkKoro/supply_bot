"""Meal-triggered reminder candidates share the existing native delivery ledger.

Only a newly logged meal within 90 minutes BEFORE a chosen native slot can move
that reminder earlier. Missing/late/stale Food leaves the original slot intact.
No extra sender or dose, no second reminder after the native slot was claimed.
"""
import time
from datetime import datetime,timedelta,timezone
import db

SCHEMA='''CREATE TABLE IF NOT EXISTS progressor_meal_anchors(
 user_id INTEGER NOT NULL,supplement_id INTEGER NOT NULL,time_local TEXT NOT NULL,
 enabled_at REAL NOT NULL,PRIMARY KEY(user_id,supplement_id,time_local));'''

async def get(conn,tg_id,sid):
    if not await db.user_has_supplement(conn,tg_id,sid):raise ValueError('supplement_not_owned')
    await conn.executescript(SCHEMA)
    times=(await db.get_effective_reminders_for_supplement(conn,tg_id,sid))['times']
    rows=await db.fetchall(conn,'SELECT time_local FROM progressor_meal_anchors WHERE user_id=? AND supplement_id=?',(tg_id,sid))
    on={r['time_local'] for r in rows}
    return {'supplement_id':sid,'slots':[{'time':t,'enabled':t in on} for t in times],
      'note':'Если вы запишете еду в Food не раньше чем за 90 минут до выбранного приёма, напоминание придёт после записи. Если еды нет, она записана поздно или связь недоступна, остаётся обычное время. Повторного уведомления в этом слоте не будет. Выбранная доза сохраняется.'}

async def save(conn,tg_id,sid,local_time,enabled):
    current=await get(conn,tg_id,sid)
    if type(enabled) is not bool or local_time not in {i['time'] for i in current['slots']}:raise ValueError('slot_changed')
    async with db.transaction(conn) as tx:
        if enabled:await tx.execute('INSERT OR IGNORE INTO progressor_meal_anchors VALUES(?,?,?,?)',(tg_id,sid,local_time,time.time()))
        else:await tx.execute('DELETE FROM progressor_meal_anchors WHERE user_id=? AND supplement_id=? AND time_local=?',(tg_id,sid,local_time))
    return await get(conn,tg_id,sid)

async def due(conn,after=0,control=None,now=None):
    exists=await db.fetchone(conn,"SELECT 1 FROM sqlite_master WHERE name='progressor_meal_anchors'")
    if not exists:return [],0
    people=await db.fetchall(conn,'SELECT DISTINCT user_id FROM progressor_meal_anchors WHERE user_id>? ORDER BY user_id LIMIT 100',(after,))
    if not people:return [],0
    ids=[int(r['user_id']) for r in people];cursor=ids[-1] if len(ids)==100 else 0
    if control is None:
        from progressor_client import call
        control=call
    try:response=await control('supply','/internal/v1/connections/meal-events',{'people':[str(v) for v in ids]},timeout=2)
    except Exception:return [],cursor
    now=now or datetime.now(timezone.utc);result=[]
    by_id={str(r['user_id']):r.get('meals',[])[:3] for r in response.get('items',[])[:100]}
    for tg_id in ids:
        if str(tg_id) not in by_id:continue
        user=await db.fetchone(conn,'''SELECT u.*,COALESCE(p.reminders_enabled,1) AS reminders_on FROM users u
          LEFT JOIN user_notification_prefs p ON p.user_id=u.id WHERE u.tg_user_id=?''',(tg_id,))
        if not user or user['is_banned'] or user['deleted_at_utc'] or not user['reminders_on']:continue
        tz=db.parse_utc_tz(user['tz_name']);local=now.astimezone(tz)
        rows=await db.fetchall(conn,'''SELECT a.* FROM progressor_meal_anchors a JOIN user_supplements s
          ON s.user_id=? AND s.supplement_id=a.supplement_id WHERE a.user_id=? AND s.is_active=1 AND s.enabled=1 AND COALESCE(s.user_frozen,0)=0''',(user['id'],tg_id))
        for row in rows:
            # A changed schedule invalidates the old anchor until reselected.
            settings=await get(conn,tg_id,row['supplement_id'])
            if row['time_local'] not in {s['time'] for s in settings['slots']}:continue
            at=datetime.combine(local.date(),datetime.strptime(row['time_local'],'%H:%M').time(),tzinfo=tz)
            if not now.astimezone(tz)<=at<=local+timedelta(minutes=90):continue
            for meal in by_id[str(tg_id)]:
                try:stamp=datetime.fromisoformat(meal['occurred_at'].replace('Z','+00:00'))
                except (ValueError,KeyError,TypeError):continue
                if stamp.tzinfo is None:continue
                if (stamp.timestamp()>row['enabled_at'] and now-timedelta(minutes=10)<=stamp<=now
                        and at-timedelta(minutes=90)<=stamp.astimezone(tz)<=at):
                    result.append({'user_id':tg_id,'supplement_id':row['supplement_id'],'day':local.date().isoformat(),'time_local':row['time_local'],'meal_trigger':True});break
    return result,cursor
