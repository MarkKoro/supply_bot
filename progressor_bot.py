"""Bundle menu and trusted Telegram receipts. Install into the existing consumer only."""
import asyncio
import uuid
from telegram import InlineKeyboardButton,InlineKeyboardMarkup,LabeledPrice
from telegram.ext import CommandHandler,CallbackQueryHandler,MessageHandler,PreCheckoutQueryHandler,ApplicationHandlerStop,filters
import progressor_client as client

TERMS='progressor.bundle.v1'
MENU_TEXT='Progressor · тренировки, питание и БАДы\n\nОдна подписка открывает три продукта. Лимиты ИИ каждого продукта сохраняются. Срок продлевается только для общей подписки; отдельные подписки действуют независимо. Без автопродления.\n\nНажимая «Оплатить», вы соглашаетесь с этими условиями. Помощь по платежам: /paysupport.'

def install(app,source,db_path):
    if not client.configured(): return

    async def menu(update,context):
        if update.effective_chat.type!='private':
            await update.effective_message.reply_text('Общую подписку можно оформить в личном чате с ботом.'); return
        try: catalog=await client.call(source,'/internal/v1/bundle/catalog',{})
        except Exception:
            await update.effective_message.reply_text('Общая подписка временно недоступна. Попробуйте позже.'); return
        buttons=[[InlineKeyboardButton(f"Оплатить {p['label']} · {p['XTR']} ⭐",callback_data='pg:buy:'+p['id'])] for p in catalog['plans']]
        await update.effective_message.reply_text(MENU_TEXT,reply_markup=InlineKeyboardMarkup(buttons))

    async def command(update,context):
        await menu(update,context); raise ApplicationHandlerStop

    async def callback(update,context):
        query=update.callback_query; await query.answer()
        if query.data=='pg:menu': await menu(update,context)
        elif query.data.startswith('pg:buy:'):
            if update.effective_chat.type!='private': raise ApplicationHandlerStop
            try:
                order=await client.call(source,'/internal/v1/bundle/quote',{'user_id':str(query.from_user.id),'plan':query.data.split(':')[-1],'order_id':str(uuid.uuid4()),'terms':TERMS})
                await context.bot.send_invoice(chat_id=query.from_user.id,title='Progressor · 3 продукта',description=f"Тренировки, питание и БАДы · {order['days']} дней. Без автопродления. Сохраняются лимиты каждого продукта.",payload='progressor:'+source+':'+order['id'],provider_token='',currency='XTR',prices=[LabeledPrice('Общая подписка',order['amount'])])
            except Exception: await query.message.reply_text('Счёт сейчас недоступен. Оплата не проведена; попробуйте позже.')
        elif query.data.startswith('pg:login:'):
            try:
                await client.call(source,'/internal/v1/website/approve',{'challenge':query.data.split(':')[-1],'user_id':str(query.from_user.id)})
                await query.message.reply_text('Вход подтверждён. Вернитесь на страницу Progressor в браузере.')
            except Exception: await query.message.reply_text('Ссылка входа истекла. Создайте новую на сайте.')
        raise ApplicationHandlerStop

    async def start(update,context):
        arg=context.args[0] if context.args else ''
        if not arg.startswith('pglogin_'): return
        challenge=arg.removeprefix('pglogin_')
        if len(challenge)!=32 or any(c not in '0123456789abcdef' for c in challenge): raise ApplicationHandlerStop
        await update.effective_message.reply_text('Подтвердите вход в ваш аккаунт Progressor на отдельном сайте. Нажимайте только если вы сами открыли страницу входа.',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Подтвердить мой вход',callback_data='pg:login:'+challenge)]]))
        raise ApplicationHandlerStop

    async def precheckout(update,context):
        q=update.pre_checkout_query
        if not q.invoice_payload.startswith('progressor:'): return
        try:
            _,owner,order_id=q.invoice_payload.split(':')
            if owner!=source: raise ValueError('wrong_source')
            await client.call(source,'/internal/v1/bundle/precheckout',{'order_id':order_id,'source':source,'payload':q.invoice_payload,'user_id':str(q.from_user.id),'amount':q.total_amount,'currency':q.currency,'checkout_id':q.id})
            await q.answer(ok=True)
        except Exception: await q.answer(ok=False,error_message='Не удалось подтвердить счёт. Средства не списаны; откройте новый счёт.')
        raise ApplicationHandlerStop

    async def payment(update,context):
        message=update.effective_message
        p=message.successful_payment or message.refunded_payment
        if not p or not p.invoice_payload.startswith('progressor:'): return
        _,owner,order_id=p.invoice_payload.split(':')
        if owner!=source: raise ApplicationHandlerStop
        kind='paid' if message.successful_payment else 'refunded'
        body={'kind':kind,'source':source,'order_id':order_id,'payload':p.invoice_payload,'user_id':str(update.effective_user.id),'amount':p.total_amount,'currency':p.currency,'charge_id':p.telegram_payment_charge_id}
        await asyncio.to_thread(client.record_receipt,db_path,body)
        completed=False
        try:
            await client.call(source,'/internal/v1/bundle/receipt',body)
            await client.sync(source,db_path); completed=True
        except Exception: pass
        if kind=='paid': await message.reply_text('Общая подписка Progressor активирована. Она действует в трёх разделах.' if completed else 'Платёж сохранён. Общая подписка будет активирована после восстановления связи; повторно оплачивать не нужно. /paysupport')
        else: await message.reply_text('Возврат сохранён. Доступ по общей подписке пересчитается; отдельные подписки сохраняются.')
        raise ApplicationHandlerStop

    async def support(update,context):
        import os
        owner=os.getenv('PROGRESSOR_SUPPORT_URL','https://t.me/ObotizactiaSuppBot?start')
        await update.effective_message.reply_text('Помощь по платежам Progressor: '+owner+'\nУкажите дату оплаты и бот, через который оплачивали. Не отправляйте данные банковской карты.')
        raise ApplicationHandlerStop

    app.add_handler(CommandHandler('progressor',command),group=-20)
    app.add_handler(CommandHandler('progressor_terms',command),group=-20)
    app.add_handler(CommandHandler('paysupport',support),group=-20)
    app.add_handler(CommandHandler('start',start),group=-20)
    app.add_handler(CallbackQueryHandler(callback,pattern=r'^pg:'),group=-20)
    app.add_handler(PreCheckoutQueryHandler(precheckout),group=-20)
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT|filters.StatusUpdate.REFUNDED_PAYMENT,payment),group=-20)
