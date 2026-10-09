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
    log=logging.getLogger('progressor_bot')
    def keyboard(rows):
        return InlineKeyboardMarkup([*rows,[InlineKeyboardButton('⬅️ В меню',callback_data=back_callback)]])
    async def show(update,context,text,rows=()):
        await render(update,context,text,keyboard(rows))
    def private(update):return update.effective_chat and update.effective_chat.type=='private'

    async def menu(update,context):
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
        await show(update,context,text,[[InlineKeyboardButton('Общая подписка',callback_data='pg:menu')]])
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
