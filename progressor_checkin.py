"""Optional, deterministic observations beside a poor check-in; never a cause."""
import asyncio
import json

def is_poor(answers):
    return answers.get('energy')==1 or answers.get('stress')==3

async def changes(tg_id,answers,control=None):
    if not is_poor(answers):return []
    if control is None:
        from progressor_client import call
        control=call
    try:
        result=await control('supply','/internal/v1/connections/changes',{'user_id':str(tg_id)},timeout=.8)
        return [r['text'][:300] for r in result.get('items',[])[:3] if isinstance(r.get('text'),str)]
    except Exception:return []

def summary(items):
    return ('В записях за неделю изменилось:\n'+'\n'.join('• '+v for v in items)+
        '\nЭто совпадения по времени, а не установленная причина самочувствия. Можно проверить записи и расписание.') if items else ''
