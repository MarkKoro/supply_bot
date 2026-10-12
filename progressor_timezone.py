import db
from progressor_timezone_history import preview,record,zone

async def change(conn,tg_id,body):
    selected=body.get('timezone');zone(selected)
    if body.get('action') not in ('preview','apply'):raise ValueError('invalid_action')
    user=await db.fetchone(conn,'SELECT id,tz_name FROM users WHERE tg_user_id=?',(tg_id,))
    if not user:raise ValueError('missing_owner')
    current=user['tz_name'] or 'UTC'
    slots=[]
    for item in (await db.get_user_supplements_with_enabled(conn,tg_id))[:30]:
        if not item['enabled'] or item['user_frozen']:continue
        config=await db.get_effective_reminders_for_supplement(conn,tg_id,item['supplement_id'])
        slots.extend([item['name']+' · '+slot for slot in config['times']])
    value=preview(current,selected,slots)
    if body['action']=='apply':
        async with db.transaction(conn) as tx:
            fresh=await db.fetchone(tx,'SELECT tz_name FROM users WHERE tg_user_id=?',(tg_id,))
            if body.get('expected_timezone')!=(fresh['tz_name'] or 'UTC'):raise ValueError('timezone_changed')
            await record(tx,tg_id,current,selected)
            await tx.execute('UPDATE users SET tz_name=? WHERE tg_user_id=?',(selected,tg_id))
    return {**value,'applied':body['action']=='apply'}
