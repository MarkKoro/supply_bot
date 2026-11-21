"""Common subscriptions in the existing consumer and the owner's single menu.

The invoice is opened from a managed menu button; financial service receipts are
never tracked or deleted as menus. No extra Telegram consumer or AI request.
"""
import asyncio
import json
import logging
import os
import time
import uuid
from telegram import InlineKeyboardButton,InlineKeyboardMarkup
from telegram.ext import CommandHandler,CallbackQueryHandler,MessageHandler,PreCheckoutQueryHandler,ApplicationHandlerStop,filters
import progressor_client as client

TERMS='progressor.bundle.v2'
class NotificationDeferred(Exception):
    """No Telegram request was made: keep an input flow and retry later."""

async def retire_owned_menu(bot,chat_id,message_id):
    """Only tracked menu ids. Cleanup failure is never a send rejection."""
    from telegram.error import TelegramError
    try:await bot.delete_message(chat_id=chat_id,message_id=message_id)
    except TelegramError:
        try:await bot.edit_message_reply_markup(chat_id=chat_id,message_id=message_id,reply_markup=None)
        except TelegramError:pass

def notification_preflight(application,user_id,source,*,ignore_recent=False):
    path=application.bot_data.get('progressor_connection_db')
    if path and not ignore_recent:
        from progressor_links import notifications_allowed
        if not notifications_allowed(path,user_id):raise NotificationDeferred
    data=application.user_data.get(user_id,{})
    if not ignore_recent and time.monotonic()-data.get('progressor_last_interaction',-1e9)<90:
        raise NotificationDeferred
    if source=='food' and (data.get('state') or data.get('pending_meal')):
        raise NotificationDeferred
    if source=='gym' and (data.get('wizard_onboarding') or data.get('running_input') or any(value for key,value in data.items() if key.startswith('await') or key.endswith('_input'))):
        raise NotificationDeferred
    if source=='supply' and (data.get('waiting') or data.get('onboarding') or data.get('add_to_stack_pending') or data.get('split_busy')):
        raise NotificationDeferred
MENU_TEXT='Progressor · тренировки, питание и БАДы\n\nОдна подписка на три продукта. Доплата учитывает подтверждённые оплаченные дни. Пробные и подарочные дни сохраняются, но не считаются денежным платежом. Лимиты ИИ каждого продукта сохраняются. Без автопродления.\n\nМожно подключить остальные продукты до текущей даты окончания либо выбрать новый общий период. /paysupport — помощь по платежам.'

def install(app,source,db_path,*,render=None,back_callback=None,notify=None):
    if not client.configured():return
    if render is None or not back_callback:raise ValueError('native_menu_renderer_required')
    app.bot_data['progressor_connection_db']=db_path
    log=logging.getLogger('progressor_bot')
    def keyboard(rows):
        return InlineKeyboardMarkup([*rows,[InlineKeyboardButton('⬅️ В меню',callback_data=back_callback)]])
    async def show(update,context,text,rows=()):
        await render(update,context,text,keyboard(rows))
    def private(update):return update.effective_chat and update.effective_chat.type=='private'

    async def account_menu(update,context):
        uid=str(update.effective_user.id)
        try:value=await client.call(source,'/internal/v1/connections/account',{'user_id':uid})
        except Exception:
            await show(update,context,'Связь продуктов временно недоступна. Записи и текущий продукт продолжают работать.');return
        from progressor_links import ensure_schema,cache_settings
        await asyncio.to_thread(ensure_schema,db_path)
        await asyncio.to_thread(cache_settings,db_path,[{'user_id':uid,'preferences':value['preferences']}])
        p=value['preferences'];context.user_data['progressor_connection_screen']=value
        labels={'gym':'Тренировки','food':'Еда','supply':'БАДы'}
        rows=[[InlineKeyboardButton(('✓ ' if p['sources'][s] else '○ ')+labels[s],callback_data='pg:link:'+s)] for s in labels]
        for field,label in [('hints','Подсказки'),('notifications','Обычные напоминания'),('weight_notifications','Новый вес → проверить цель питания'),('goal_links','Связанные действия после смены цели')]:
            rows.append([InlineKeyboardButton(('✓ ' if p[field] else '○ ')+label,callback_data='pg:pref:'+field)])
        rows.append([InlineKeyboardButton('Вернуться из отдыха' if p['rest_active'] else 'Отдохнуть от напоминаний',callback_data='pg:rest')])
        rows.append([InlineKeyboardButton('Вес и источники',callback_data='pg:weights'),InlineKeyboardButton('Моя неделя',callback_data='pg:weekly')])
        rows.append([InlineKeyboardButton('Сегодня вместе',callback_data='pg:today')])
        rows.append([InlineKeyboardButton(('✓ ' if p['weekly_plan'] else '○ ')+'План питания раз в неделю',callback_data='pg:pref:weekly_plan')])
        rows.append([InlineKeyboardButton('Предпросмотр плана',callback_data='pg:weekly_plan'),InlineKeyboardButton('Ритм тренировок',callback_data='pg:rhythm')])
        rows.append([InlineKeyboardButton('Питание для тренера',callback_data='pg:coach_permissions')])
        rows.append([InlineKeyboardButton('Как всё работает',callback_data='pg:help')])
        if source=='gym':rows.append([InlineKeyboardButton('Завершить свободную тренировку',callback_data='pg:free_finish')])
        text='Связь продуктов\n\nВыбирайте, какие данные помогают друг другу. Напоминания и подсказки можно выключить. Данные не удаляются.\n\n'
        text+=('Сейчас отдых. При возврате продолжатся следующие актуальные напоминания. ' if p['rest_active'] else '')+'На отдыхе оплаченный срок подписки продолжается.'
        await show(update,context,text,rows)

    async def account_action(update,context):
        query=update.callback_query;parts=query.data.split(':');uid=str(query.from_user.id)
        if parts[1]=='settings':await account_menu(update,context);return
        if parts[1] in ('link','pref','rest'):
            value=await client.call(source,'/internal/v1/connections/account',{'user_id':uid});p=value['preferences']
            if parts[1]=='link':patch={'sources':{**p['sources'],parts[2]:not p['sources'][parts[2]]}}
            elif parts[1]=='pref':
                if parts[2] not in ('hints','notifications','weight_notifications','goal_links','weekly_plan','day_prompt'):return
                patch={parts[2]:not p[parts[2]]}
            else:patch={'paused':not p['rest_active'],'pause_until':None}
            await client.call(source,'/internal/v1/connections/account',{'user_id':uid,'action':'preferences','patch':patch,'revision':p['revision']})
            await account_menu(update,context);return
        back=[[InlineKeyboardButton('⬅️ Связь продуктов',callback_data='pg:settings')]]
        if parts[1]=='help':
            value=await client.call(source,'/internal/v1/connections/account',{'user_id':uid})
            await show(update,context,'Как всё работает\n\n'+'\n\n'.join(h['title']+'\n'+h['text'] for h in value['help']),back)
        elif parts[1]=='weights':
            value=await client.call(source,'/internal/v1/connections/account',{'user_id':uid});context.user_data['progressor_weight_choice']=value['weight']
            rows=[[InlineKeyboardButton(f"{i['source'].title()}: {i['value']:g} кг — выбрать",callback_data='pg:weight:'+i['source'])] for i in value['weight']['items']]
            await show(update,context,value['weight']['explanation'] if rows else 'Свежего разрешённого веса пока нет. Заполните свой профиль и включите нужные связи.',rows+back)
        elif parts[1]=='weight':
            value=context.user_data.get('progressor_weight_choice') or {}
            item=next((i for i in value.get('items',[]) if i['source']==parts[2]),None)
            if not item:await account_menu(update,context);return
            await show(update,context,f"Использовать {item['value']:g} кг в выбранных профилях? История измерений и сохранённая цель питания останутся. Если профиль уже изменился, бот попросит новый выбор.",[[InlineKeyboardButton('Да, согласовать вес',callback_data='pg:weight_confirm:'+parts[2])]]+back)
        elif parts[1]=='weight_confirm':
            value=context.user_data.get('progressor_weight_choice') or {};item=next((i for i in value.get('items',[]) if i['source']==parts[2]),None)
            if not item:await account_menu(update,context);return
            result=await client.call(source,'/internal/v1/connections/account',{'user_id':uid,'action':'weight','event_id':item['event_id'],'fingerprint':value['fingerprint'],'operation_id':str(uuid.uuid4())})
            context.user_data.pop('progressor_weight_choice',None)
            await show(update,context,'Выбор сохранён. Владельцы профилей проверят свежесть и обновят вес. Обычно это занимает до минуты. Если одно значение успело измениться, оно сохранится для нового выбора.',back)
        elif parts[1]=='today':
            value=await client.call(source,'/internal/v1/connections/today',{'user_id':uid})
            lines=[value['title']]
            lines += [item['name']+'\n'+item['text']+ ('\n'+item['day']+' · '+item['timezone'] if item['source']=='supply' else '') for item in value['sections']]
            if value.get('relationship'):lines.append(value['relationship'])
            lines.append('Что сейчас связано\n'+'\n'.join(item['name']+' — '+item['label'] for item in value['health']['items']))
            lines.extend([value['day_label'],value['coverage']])
            await show(update,context,'\n\n'.join(lines),[[InlineKeyboardButton('Обновить обзор',callback_data='pg:today')]]+back)
        elif parts[1]=='weekly':
            value=await client.call(source,'/internal/v1/connections/weekly',{'user_id':uid});lines=['Моя неделя']
            n=value.get('nutrition');w=value.get('workouts');s=value.get('supply')
            if n:
                a=n['averages_on_logged_days'];lines.append(f"Еда: {n['logged_days']} записанных дней из 7. Среднее по ним: {a['kcal']} ккал; Б {a['protein']}, Ж {a['fat']}, У {a['carbs']} г.")
            if w:lines.append(f"Тренировки: сессий {w['recorded_sessions']}, завершено {w['completed_sessions']}; рабочих подходов {w['working_sets']}.")
            if s:lines.append(f"БАДы: активных добавок {s['active_supplements']}; отметок приёма {s['recorded_intakes']}.")
            lines.append(value['coverage']);await show(update,context,'\n\n'.join(lines),back)
        elif parts[1]=='weekly_plan':
            value=await client.call(source,'/internal/v1/connections/weekly-plan',{'user_id':uid})
            await show(update,context,value['text']+'\n\nВремя сводки можно выбрать в приложении → Связь продуктов.',back)
        elif parts[1]=='rhythm':
            value=await client.call(source,'/internal/v1/connections/account',{'user_id':uid});context.user_data['progressor_rhythm']=value['rhythm'];r=value['rhythm'];rows=[];text=r['proposal']['message']
            if r['active']:
                text='Ритм подтверждён. Недельная сумма калорий сохранена. Точный план по дням — в приложении → Связь продуктов.'
                rows.append([InlineKeyboardButton('Обычная одинаковая цель',callback_data='pg:rhythm_off')])
            elif r['proposal']['status']=='proposal':
                labels=('Пн','Вт','Ср','Чт','Пт','Сб','Вс');text+='\nДни: '+', '.join(labels[d] for d in r['proposal']['weekdays'])
                for plan in r['proposal']['previews']:
                    extra=plan['training_extra_kcal'];text+='\n\n'+('Без изменения КБЖУ' if extra==0 else f'В тренировочный день +{extra} ккал; в другие дни пропорционально меньше')+'\n'+' · '.join(labels[i]+': '+str(round(day['kcal'])) for i,day in enumerate(plan['days']))
                    rows.append([InlineKeyboardButton('Подтвердить '+('без изменения КБЖУ' if extra==0 else f'+{extra} ккал в дни тренировок'),callback_data='pg:rhythm_confirm:'+str(extra))])
            await show(update,context,text+'\n\nПодтверждение действует 28 дней. Смена цели или системы вернёт обычную цель. Это перераспределение вашей цели, а не измерение потраченных калорий.',rows+back)
        elif parts[1]=='rhythm_confirm':
            r=(context.user_data.get('progressor_rhythm') or {}).get('proposal')
            if not r:await account_menu(update,context);return
            await client.call(source,'/internal/v1/connections/account',{'user_id':uid,'action':'rhythm','fingerprint':r['fingerprint'],'base_goal_key':r['base_goal_key'],'extra':int(parts[2])})
            context.user_data.pop('progressor_rhythm',None)
            await show(update,context,'Ритм подтверждён. Раздел «Еда» применит план после синхронизации, обычно в течение минуты. Недельная сумма сохранена.',back)
        elif parts[1]=='rhythm_off':
            await client.call(source,'/internal/v1/connections/account',{'user_id':uid,'action':'rhythm_off'})
            await show(update,context,'После синхронизации раздел «Еда» вернётся к вашей одинаковой цели на каждый день.',back)
        elif parts[1]=='coach_permissions':
            value=await client.call(source,'/internal/v1/connections/account',{'user_id':uid});rows=[]
            for item in value['coach']['items']:
                rows.append([InlineKeyboardButton(('✓ ' if item['macros'] else '○ ')+'КБЖУ · '+item['label'],callback_data='pg:coach_macros:'+item['coach_id'])])
                rows.append([InlineKeyboardButton(('✓ ' if item['top_dishes'] else '○ ')+'5 частых блюд',callback_data='pg:coach_dishes:'+item['coach_id'])])
            await show(update,context,value['coach']['explanation']+'\n\nРазрешение из этого меню действует 30 дней. Срок 7/90 дней можно выбрать в приложении.' if rows else 'Сначала подтвердите связь с тренером в разделе «Тренировки».',rows+back)
        elif parts[1] in ('coach_macros','coach_dishes'):
            value=await client.call(source,'/internal/v1/connections/account',{'user_id':uid});item=next((i for i in value['coach']['items'] if i['coach_id']==parts[2]),None)
            if not item:await account_menu(update,context);return
            macros=not item['macros'] if parts[1]=='coach_macros' else item['macros'];dishes=not item['top_dishes'] if parts[1]=='coach_dishes' else item['top_dishes']
            if dishes and not macros:dishes=False
            await client.call(source,'/internal/v1/connections/account',{'user_id':uid,'action':'coach','coach_id':item['coach_id'],'macros':macros,'top_dishes':macros and dishes,'days':30})
            await account_menu(update,context)
        elif parts[1]=='coach_view' and source=='gym':
            def owner_link():
                import sqlite3
                from contextlib import closing
                with closing(sqlite3.connect(db_path,timeout=2)) as db:
                    return db.execute("SELECT pupil.tg_id FROM trainer_links l JOIN users coach ON coach.id=l.trainer_user_id JOIN users pupil ON pupil.id=l.trainee_user_id WHERE coach.tg_id=? AND coach.is_trainer=1 AND pupil.id=? AND l.status='accepted'",(int(uid),int(parts[2]))).fetchone()
            link=await asyncio.to_thread(owner_link)
            if not link:await show(update,context,'Подтверждённая связь с подопечным недоступна.',back);return
            try:value=await client.call('gym','/internal/v1/connections/coach',{'coach_id':uid,'trainee_id':str(link[0])})
            except Exception:
                await show(update,context,'Питание не открыто. Подопечный сам включает КБЖУ в «Связь продуктов». Для связи нужны действующий доступ и свежие данные.',back);return
            t=value['totals'];text='Питание подопечного · '+value['date']+'\n\n'
            text+=(f"{t['kcal']:g} ккал; белки {t['protein']:g} г, жиры {t['fat']:g} г, углеводы {t['carbs']:g} г." if t else 'Записанных блюд за этот день пока нет.')
            text+='\nТолько сохранённые КБЖУ; дневник может быть неполным.'
            if value.get('top_dishes_enabled'):text+='\n\n5 частых блюд за 30 дней:\n'+'\n'.join(f"• {i['name']} — {i['logged_count']}" for i in value.get('top_dishes',[]))
            await show(update,context,text,[[InlineKeyboardButton('⬅️ К подопечному',callback_data='trainer:view:'+parts[2])]])
        elif parts[1]=='free_finish' and source=='gym':
            from progressor_free_workout import current,finish
            session=await asyncio.to_thread(current,db_path,int(uid))
            if not session:await show(update,context,'Сегодня свободная тренировка ещё не записана.',back);return
            result=await asyncio.to_thread(finish,db_path,session['user_id'],session['id'])
            await show(update,context,'Свободная тренировка завершена. Записанные подходы учтены один раз.' if result['finished'] else 'Сначала сохраните выполненные подходы или кардио.',back)

    async def menu(update,context,*,other_periods=False):
        if not private(update):return
        try:catalog=await client.call(source,'/internal/v1/bundle/catalog',{'user_id':str(update.effective_user.id)})
        except Exception:
            await show(update,context,'Персональная стоимость общей подписки сейчас недоступна. Текущий продукт продолжает работать. Попробуйте позже.');return
        rows=[];lines=[MENU_TEXT]
        view=catalog.get('subscriptions')
        if view:
            lines.append('\nСейчас у вас:')
            for product in view['products']:
                state=product['state'];end=product.get('expires_at')
                label={'active':'✓ Доступ активен','trial':'◷ Пробный доступ','free':'○ Бесплатный дневник','inactive':'Нет подписки','unknown':'Уточняем доступ'}[state]
                if end:label+=' до '+end[:10]
                lines.append(product['name']+' — '+label)
        for p in catalog['plans']:
            if p['id'] != 'align' and (p['id'] in {'m1','m6','y1'}) == other_periods:
                continue
            if other_periods and p['id'] == 'align':continue
            price=p['prices']['XTR'];label=p['label']
            if price['already_covered']:
                lines.append('\n'+label+' — все три продукта уже доступны по оплаченным подпискам.');continue
            rub=p.get('prices',{}).get('RUB',{})
            amounts=[];credits=[]
            if catalog.get('rub_available') and p.get('RUB'):
                amounts.append(f"{p['RUB']/100:g} ₽")
                if rub.get('credit_amount'):credits.append(f"{rub['credit_amount']/100:g} ₽")
            amounts.append(f"{p['XTR']} ⭐")
            if price['credit_amount']:credits.append(f"{price['credit_amount']} ⭐")
            lines.append('\n'+label+': '+' / '.join(amounts)+(' · зачтено '+' / '.join(credits) if credits else ''))
            pair=[]
            if catalog.get('rub_available') and p.get('RUB'):
                from urllib.parse import urlencode
                url=catalog['website_url']+'?'+urlencode({'source':source,'plan':p['id']})
                pair.append(InlineKeyboardButton(f"{p['RUB']/100:g} ₽ · {label}",url=url))
            pair.append(InlineKeyboardButton(f"{p['XTR']} ⭐ · {label}",callback_data='pg:buy:'+p['id']))
            rows.append(pair)
        if other_periods or any(p['id'] not in {'m1','m6','y1','align'} for p in catalog['plans']):
            rows.append([InlineKeyboardButton('Основные сроки' if other_periods else 'Другие сроки',callback_data='pg:menu' if other_periods else 'pg:periods')])
        pending=catalog.get('pending_order')
        if pending:
            lines.append('\nЕсть неоплаченный счёт. Чтобы выбрать другой срок или способ оплаты, сначала отмените его. Если деньги уже списаны, дождитесь подтверждения.')
            if not pending['checkout_id'] and not pending['provider_id']:
                rows.append([InlineKeyboardButton('Отменить неоплаченный счёт',callback_data='pg:cancel:'+pending['id'])])
        await show(update,context,'\n'.join(lines),rows)

    async def command(update,context):
        await menu(update,context);raise ApplicationHandlerStop

    async def callback(update,context):
        query=update.callback_query;await query.answer()
        if not private(update):raise ApplicationHandlerStop
        if query.data=='pg:menu':await menu(update,context)
        elif query.data=='pg:periods':await menu(update,context,other_periods=True)
        elif query.data.split(':')[1] in ('settings','link','pref','rest','help','weights','weight','weight_confirm','today','weekly','weekly_plan','rhythm','rhythm_confirm','rhythm_off','coach_permissions','coach_macros','coach_dishes','coach_view','free_finish'):
            try:await account_action(update,context)
            except Exception:await show(update,context,'Данные изменились или связь временно недоступна. Обновите настройки.',[[InlineKeyboardButton('Связь продуктов',callback_data='pg:settings')]])
        elif query.data.startswith('pg:buy:'):
            order=None
            try:
                order=await client.call(source,'/internal/v1/bundle/quote',{'user_id':str(query.from_user.id),'plan':query.data.split(':')[-1],'order_id':str(uuid.uuid4()),'terms':TERMS})
                link=await client.call(source,'/internal/v1/bundle/invoice-link',{'user_id':str(query.from_user.id),'order_id':order['id']},timeout=10)
                frozen=json.loads(order['pricing_json'])
                text=f"Progressor · три продукта\n\nК оплате: {order['amount']} ⭐. Зачтено оплаченного доступа: {frozen['credit_amount']} ⭐.\n{frozen['label']}. Срок нового периода начинается после подтверждения оплаты; продление продолжает уже оплаченный общий период. Вариант до текущей даты заканчивается в указанную дату.\n\nБез автопродления. Лимиты каждого продукта сохраняются. Нажимая «Оплатить», вы принимаете условия выше. Цена счёта действует 20 минут."
                await show(update,context,text,[[InlineKeyboardButton(f"Оплатить {order['amount']} ⭐",url=link['invoice_url'])],[InlineKeyboardButton('Изменить выбор',callback_data='pg:cancel:'+order['id'])]])
            except Exception:
                rows=[[InlineKeyboardButton('К вариантам подписки',callback_data='pg:menu')]]
                if order:rows.insert(0,[InlineKeyboardButton('Отменить неоплаченный счёт',callback_data='pg:cancel:'+order['id'])])
                await show(update,context,'Не удалось подготовить счёт. Если у вас уже открыт другой счёт, вернитесь к вариантам подписки. Если деньги списаны, повторно оплачивать не нужно. /paysupport',rows)
        elif query.data.startswith('pg:cancel:'):
            try:
                await client.call(source,'/internal/v1/bundle/cancel',{'user_id':str(query.from_user.id),'order_id':query.data.split(':')[-1]})
                await menu(update,context)
            except Exception:await show(update,context,'Платёж уже проверяется. Дождитесь подтверждения; повторно оплачивать не нужно.',[[InlineKeyboardButton('Общая подписка',callback_data='pg:menu')]])
        elif query.data.startswith('pg:login:'):
            try:
                await client.call(source,'/internal/v1/website/approve',{'challenge':query.data.split(':')[-1],'user_id':str(query.from_user.id)})
                await show(update,context,'Вход подтверждён. Вернитесь на страницу Progressor в браузере.')
            except Exception:await show(update,context,'Ссылка входа истекла. Создайте новую на сайте.')
        raise ApplicationHandlerStop

    async def start(update,context):
        arg=context.args[0] if context.args else ''
        if arg=='pgsettings':
            if private(update):await account_menu(update,context)
            raise ApplicationHandlerStop
        if arg.startswith('pgplan_'):
            await menu(update,context);raise ApplicationHandlerStop
        if not arg.startswith('pglogin_'):return
        challenge=arg.removeprefix('pglogin_')
        if not private(update) or len(challenge)!=32 or any(c not in '0123456789abcdef' for c in challenge):raise ApplicationHandlerStop
        await show(update,context,'Подтвердите вход в ваш аккаунт Progressor на отдельном сайте. Нажимайте только если вы сами открыли страницу входа.',[[InlineKeyboardButton('Подтвердить мой вход',callback_data='pg:login:'+challenge)]])
        raise ApplicationHandlerStop

    async def precheckout(update,context):
        q=update.pre_checkout_query
        if not q.invoice_payload.startswith('progressor:'):return
        approved=False
        try:
            _,owner,order_id=q.invoice_payload.split(':')
            if owner!=source:raise ValueError('wrong_source')
            await client.call(source,'/internal/v1/bundle/precheckout',{'order_id':order_id,'source':source,'payload':q.invoice_payload,'user_id':str(q.from_user.id),'amount':q.total_amount,'currency':q.currency,'checkout_id':q.id})
            approved=True
            await q.answer(ok=True)
        except Exception as error:
            from telegram.error import BadRequest
            # Only an explicit first-answer rejection proves no checkout was accepted.
            # A timeout remains reserved: money may already have been authorized.
            known_rejection=isinstance(error,BadRequest) and ('query is too old' in str(error).lower() or 'query id is invalid' in str(error).lower())
            if approved and known_rejection:
                try:await client.call(source,'/internal/v1/bundle/precheckout-rejected',{'order_id':order_id,'user_id':str(q.from_user.id),'checkout_id':q.id})
                except Exception:pass
            if not approved:
                try:await q.answer(ok=False,error_message='Не удалось подтвердить счёт. Откройте варианты подписки. Если деньги уже списаны, повторно оплачивать не нужно.')
                except Exception:pass
        raise ApplicationHandlerStop

    async def payment(update,context):
        message=update.effective_message;p=message.successful_payment or message.refunded_payment
        if not p or not p.invoice_payload.startswith('progressor:'):return
        _,owner,order_id=p.invoice_payload.split(':')
        if owner!=source:raise ApplicationHandlerStop
        kind='paid' if message.successful_payment else 'refunded'
        body={'kind':kind,'source':source,'order_id':order_id,'payload':p.invoice_payload,'user_id':str(update.effective_user.id),'amount':p.total_amount,'currency':p.currency,'charge_id':p.telegram_payment_charge_id}
        await asyncio.to_thread(client.record_receipt,db_path,body)
        completed=False
        try:
            await client.call(source,'/internal/v1/bundle/receipt',body);await client.sync(source,db_path);completed=True
        except Exception:pass
        text=('Общая подписка Progressor подтверждена. Доступ обновляется во всех трёх продуктах.' if completed else 'Платёж сохранён. Доступ обновится после восстановления связи; повторно оплачивать не нужно. /paysupport') if kind=='paid' else 'Возврат сохранён. Отзывается только период этой общей покупки; отдельные подписки и история сохраняются.'
        try:notification_preflight(context.application,update.effective_user.id,source,ignore_recent=True)
        except NotificationDeferred:raise ApplicationHandlerStop
        rows=[[InlineKeyboardButton('Общая подписка',callback_data='pg:menu')]]
        if completed and kind=='paid':rows.insert(0,[InlineKeyboardButton('Настроить связь продуктов',callback_data='pg:settings')])
        await show(update,context,text,rows)
        raise ApplicationHandlerStop

    async def support(update,context):
        if private(update):await show(update,context,'Укажите дату оплаты и бот, через который оплачивали. Не отправляйте данные банковской карты.',[[InlineKeyboardButton('Связаться с поддержкой',url=os.getenv('PROGRESSOR_SUPPORT_URL','https://t.me/ObotizactiaSuppBot?start'))]])
        raise ApplicationHandlerStop

    async def activity(update,context):
        if not private(update) or not update.effective_user:return
        context.user_data['progressor_last_interaction']=time.monotonic()
        try:
            from progressor_native_subscription import record_activity
            await asyncio.to_thread(record_activity,db_path,update.effective_user.id,update.update_id)
        except Exception:log.warning('bot_activity_unavailable source=%s',source)
    app.add_handler(MessageHandler(filters.TEXT|filters.PHOTO|filters.VOICE|filters.Document.ALL,activity),group=-30)
    app.add_handler(CallbackQueryHandler(activity),group=-30)
    app.add_handler(CommandHandler('progressor',command),group=-20)
    app.add_handler(CommandHandler('progressor_terms',command),group=-20)
    app.add_handler(CommandHandler('paysupport',support),group=-20)
    app.add_handler(CommandHandler('start',start),group=-20)
    app.add_handler(CallbackQueryHandler(callback,pattern=r'^pg:'),group=-20)
    app.add_handler(PreCheckoutQueryHandler(precheckout),group=-20)
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT|filters.StatusUpdate.REFUNDED_PAYMENT,payment),group=-20)

    async def expiry(context):
        from telegram.error import Forbidden,RetryAfter
        cursor=context.application.bot_data.get('progressor_expiry_cursor','')
        try:
            batch=await client.call(source,'/internal/v1/subscription-notices/claim',{'after':cursor},timeout=5)
        except Exception:return
        context.application.bot_data['progressor_expiry_cursor']=batch.get('next') or ''
        for item in batch.get('items',[])[:20]:
            if not isinstance(item.get('user_id'),str) or not item['user_id'].isdecimal() or not isinstance(item.get('text'),str) or len(item['text'])>3500 or not isinstance(item.get('event_key'),str) or len(item['event_key'])!=64:continue
            status='sent';delay=0
            try:
                if notify:await notify(int(item['user_id']),item['text'],keyboard([[InlineKeyboardButton('Общая подписка',callback_data='pg:menu')]]))
                else:raise RuntimeError('native_notification_sender_required')
            except RetryAfter as error:
                status='retry';delay=error.retry_after.total_seconds() if hasattr(error.retry_after,'total_seconds') else float(error.retry_after)
            except Forbidden:status='blocked'
            except NotificationDeferred:status='deferred';delay=60
            except Exception:status='uncertain'
            try:await client.call(source,'/internal/v1/subscription-notices/ack',{'event_key':item['event_key'],'status':status,'retry_after':delay})
            except Exception:log.warning('subscription_notice_ack_unavailable source=%s',source)
    if app.job_queue and notify:
        app.job_queue.run_repeating(expiry,interval=60,first=45,name='progressor_subscription_expiry_v2')
    elif notify:
        # Food has no APScheduler dependency. Use its existing consumer lifecycle,
        # just like its native opt-in reminder worker, without another consumer.
        previous_init=app.post_init;previous_stop=app.post_stop;task=None
        async def init(instance):
            nonlocal task
            if previous_init:await previous_init(instance)
            from types import SimpleNamespace
            async def worker():
                await asyncio.sleep(45)
                while True:
                    try:await expiry(SimpleNamespace(application=instance))
                    except Exception:log.warning('subscription_notice_cycle_unavailable source=%s',source)
                    await asyncio.sleep(60)
            task=asyncio.create_task(worker(),name='progressor_subscription_expiry_v2')
        async def stop(instance):
            if task:
                task.cancel()
                try:await task
                except asyncio.CancelledError:pass
            if previous_stop:await previous_stop(instance)
        app.post_init=init;app.post_stop=stop
