"""Owner-local timezone transitions; historical events use their original zone.

The reader also works on Core's private snapshot. No historical timestamps or
source-local dates are rewritten. A transition is written in the owner's same
transaction before changing its current zone.
"""
from datetime import datetime,timezone,timedelta
from zoneinfo import ZoneInfo
import re,time

SCHEMA='''CREATE TABLE IF NOT EXISTS progressor_timezone_history(
 user_id TEXT NOT NULL,changed_at REAL NOT NULL,before_zone TEXT NOT NULL,
 after_zone TEXT NOT NULL,PRIMARY KEY(user_id,changed_at));'''

def zone(name):
    match=re.fullmatch(r'UTC([+-])(\d{2}):(\d{2})',str(name))
    if match:
        hours,minutes=map(int,match.groups()[1:])
        if hours>14 or minutes>59 or (hours==14 and minutes):raise ValueError('invalid_timezone')
        return timezone(timedelta(minutes=(hours*60+minutes)*(1 if match[1]=='+' else -1)))
    if not isinstance(name,str) or len(name)>80:raise ValueError('invalid_timezone')
    try:return ZoneInfo(name)
    except (ValueError,KeyError):raise ValueError('invalid_timezone')

async def record(conn,user_id,before,after):
    zone(after)
    if before==after:return
    # CREATE without executescript preserves the owner's existing transaction.
    await conn.execute(SCHEMA)
    await conn.execute('INSERT INTO progressor_timezone_history VALUES(?,?,?,?)',
                       (str(user_id),time.time(),before or 'UTC',after))

def at(db,user_id,stamp,current):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='progressor_timezone_history'").fetchone():return current
    instant=datetime.fromisoformat(stamp.replace('Z','+00:00'))
    if instant.tzinfo is None:instant=instant.replace(tzinfo=timezone.utc)
    row=db.execute('SELECT before_zone FROM progressor_timezone_history WHERE user_id=? AND changed_at>? ORDER BY changed_at LIMIT 1',(str(user_id),instant.timestamp())).fetchone()
    return row[0] if row else current

def preview(current,selected,slots=(),now=None):
    instant=now or datetime.now(timezone.utc)
    old=instant.astimezone(zone(current));new=instant.astimezone(zone(selected))
    return {'current_timezone':current,'timezone':selected,'clock_before':old.isoformat(timespec='minutes'),
      'clock_after':new.isoformat(timespec='minutes'),'slots':list(slots)[:30],
      'note':'Местное время будущих напоминаний сохраняется, часовой пояс изменится. Уже сохранённые даты и записи остаются в истории. Пропущенные напоминания не догоняются.'}
