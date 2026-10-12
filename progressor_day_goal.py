"""Explicitly confirmed weekly redistribution; base goal/history stay intact."""
import hashlib,json,math

def goal_key(values):
    clean={k:float(values[k]) for k in ('kcal','protein','fat','carbs')}
    return hashlib.sha256(json.dumps(clean,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def redistribute(values,weekdays,extra):
    if (not isinstance(weekdays,list) or not 1<=len(weekdays)<=5 or len(set(weekdays))!=len(weekdays)
            or any(type(d) is not int or not 0<=d<=6 for d in weekdays) or extra not in (0,100,150)):
        raise ValueError('invalid_rhythm')
    base={k:float(values[k]) for k in ('kcal','protein','fat','carbs')}
    if any(not math.isfinite(v) or v<0 for v in base.values()) or not 1200<=base['kcal']<=5000:raise ValueError('manual_goal_required')
    off=-extra*len(weekdays)/(7-len(weekdays))
    if base['carbs']+off/4<40:raise ValueError('manual_goal_required')
    week=[]
    for day in range(7):
        delta=extra if day in weekdays else off
        week.append({**base,'kcal':round(base['kcal']+delta,3),'carbs':round(base['carbs']+delta/4,3)})
    return {'days':week,'weekly_kcal':round(base['kcal']*7,3),'includes_usual_activity':True,
        'training_extra_kcal':extra,'basis':'user_confirmed_redistribution; not_measured_energy; no_eatback',
        'base_goal_key':goal_key(base)}
