"""Owner-confirmed composition and possible overlap; never add doses or change plans."""
import json,math,time
import db
from progressor_food_sources import REGISTRY,matches

SCHEMA='''CREATE TABLE IF NOT EXISTS progressor_supplement_composition(
 user_id INTEGER NOT NULL,supplement_id INTEGER NOT NULL,revision INTEGER NOT NULL,
 body TEXT NOT NULL,updated_at REAL NOT NULL,PRIMARY KEY(user_id,supplement_id));'''

async def owner_id(conn,tg_id):
    row=await db.fetchone(conn,'SELECT id FROM users WHERE tg_user_id=?',(int(tg_id),))
    if not row:raise ValueError('owner_missing')
    return int(row['id'])

async def owned(conn,tg_id,sid):
    uid=await owner_id(conn,tg_id)
    row=await db.fetchone(conn,'''SELECT s.id,COALESCE(NULLIF(s.name_ru,''),s.name) AS name,s.slug FROM supplements s
      WHERE s.id=? AND (EXISTS(SELECT 1 FROM user_supplements us WHERE us.user_id=? AND us.supplement_id=s.id)
       OR EXISTS(SELECT 1 FROM user_complex_items ci JOIN user_complexes c ON c.id=ci.complex_id WHERE c.user_id=? AND ci.supplement_id=s.id))''',(sid,uid,uid))
    if not row:raise ValueError('supplement_not_owned')
    return uid,dict(row)

async def composition(conn,tg_id,sid):
    uid,item=await owned(conn,tg_id,sid);await conn.executescript(SCHEMA)
    row=await db.fetchone(conn,'SELECT * FROM progressor_supplement_composition WHERE user_id=? AND supplement_id=?',(uid,sid))
    codes=matches(item['name']) or matches(item['slug'])
    entries=json.loads(row['body']) if row else [{'code':c,'form':None,'amount':None,'unit':None,'source':'catalogue_named_substance'} for c in codes]
    return {**item,'revision':row['revision'] if row else 0,'entries':entries,'confirmed_by_user':bool(row),
      'note':'Проверяем названные вещества и состав, подтверждённый вами по этикетке. Неизвестные форму и количество не угадываем. Дозировки не складываем и не меняем.'}

def validate(entries):
    if not isinstance(entries,list) or len(entries)>16:raise ValueError('invalid_composition')
    clean=[];seen=set()
    for item in entries:
        if not isinstance(item,dict) or set(item)-{'code','form','amount','unit','source'}:raise ValueError('invalid_composition')
        code=item.get('code');form=item.get('form') or None;amount=item.get('amount');unit=item.get('unit') or None
        if code not in REGISTRY or code in seen or form is not None and (not isinstance(form,str) or len(form)>80):raise ValueError('invalid_substance')
        if amount is not None and (type(amount) not in (int,float) or not math.isfinite(amount) or not 0<=amount<=1000000 or unit not in ('g','mg','ug','IU')):raise ValueError('invalid_label_amount')
        if amount is None:unit=None
        clean.append({'code':code,'form':form,'amount':amount,'unit':unit,'source':'user_confirmed_label'});seen.add(code)
    return clean

async def save(conn,tg_id,sid,entries,revision):
    uid,_=await owned(conn,tg_id,sid);clean=validate(entries)
    if type(revision) is not int or revision<0:raise ValueError('invalid_revision')
    await conn.executescript(SCHEMA)
    body=json.dumps(clean,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    async with db.transaction(conn) as tx:
        row=await db.fetchone(tx,'SELECT revision,body FROM progressor_supplement_composition WHERE user_id=? AND supplement_id=?',(uid,sid))
        if not row or row['body']!=body:
            if (row['revision'] if row else 0)!=revision:raise ValueError('composition_changed')
            await tx.execute('INSERT INTO progressor_supplement_composition VALUES(?,?,?,?,?) ON CONFLICT(user_id,supplement_id) DO UPDATE SET revision=excluded.revision,body=excluded.body,updated_at=excluded.updated_at',(uid,sid,revision+1,body,time.time()))
    return await composition(conn,tg_id,sid)

async def check(conn,tg_id):
    uid=await owner_id(conn,tg_id);await conn.executescript(SCHEMA)
    groups=await db.get_today_plan_groups(conn,tg_id,db.get_user_local_date(await db.get_user_tz(conn,tg_id)))
    products={};containers={}
    for group in groups:
        for item in group['items']:
            sid=item['supplement_id'];products[sid]=item['name_ru'];containers.setdefault(sid,set()).add('Отдельный приём · '+group['time'])
        for complex_item in group['complexes']:
            for item in complex_item['items']:
                sid=item['supplement_id'];products[sid]=item.get('name_ru') or item.get('name') or 'Добавка'
                containers.setdefault(sid,set()).add('Комплекс '+complex_item['name']+' · '+group['time'])
    split_rows=await db.fetchall(conn,'SELECT supplement_id,base_supplement_id FROM user_supplement_split_groups WHERE user_id=?',(uid,))
    split={int(r['supplement_id']):int(r['base_supplement_id']) for r in split_rows}
    substances={};unknown=0;items=[]
    for sid,name in products.items():
        c=await composition(conn,tg_id,sid);items.append(c)
        if not c['entries']:unknown+=1
        for entry in c['entries']:
            key=(entry['code'],split.get(sid,sid))
            if key in substances:continue # Native deliberate split doses remain one product.
            substances[key]={'supplement_id':sid,'name':name,'containers':sorted(containers[sid]),**entry}
    warnings=[]
    for code in sorted({key[0] for key in substances}):
        sources=[v for (c,_),v in substances.items() if c==code]
        if len(sources)>1:
            warnings.append({'code':code,'name':REGISTRY[code]['name'],'products':sources,
                'message':REGISTRY[code]['name']+' встречается в нескольких активных добавках. Проверьте этикетки и то, что сочетание запланировано. Количество и дозы автоматически не суммируем.'})
    for sid,places in containers.items():
        if len(places)>1 and any(s.startswith('Комплекс ') for s in places) and any(s.startswith('Отдельный приём') for s in places):
            warnings.append({'supplement_id':sid,'name':products[sid],'products':[],
                'message':products[sid]+' есть в комплексе и отдельном приёме. Если вы намеренно разделили суточную дозу, сохраните этот план; иначе проверьте расписание.'})
    return {'warnings':warnings[:12],'items':items[:100],'unknown_composition_count':unknown,
      'note':'Совпадение вещества не доказывает передозировку. Формы и единицы показаны по подтверждённой этикетке; неизвестные значения остаются неизвестными. План и дозы сохраняются.'}
