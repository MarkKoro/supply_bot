"""Actual shared handlers in each native consumer; fake Telegram, zero providers."""
import json,os,tempfile,unittest,sqlite3,time
import asyncio
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,Mock,patch
from collections import defaultdict
from telegram.error import BadRequest,TimedOut
from telegram.ext import ApplicationHandlerStop
import progressor_bot as module
import progressor_client as client

SOURCE='supply'  # Owner contract; deployment/worktree folder names are arbitrary.
class NativeBundleHandlers(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=os.getenv('PROGRESSOR_TEST_TMP_DIR'));self.path=str(Path(self.tmp.name)/'owner.db')
        self.handlers=[];self.render=AsyncMock();self.notify=AsyncMock()
        self.app=NS(user_data=defaultdict(dict),job_queue=Mock(),bot_data={},add_handler=lambda h,group=0:self.handlers.append(h))
        with patch.object(client,'configured',return_value=True):module.install(self.app,SOURCE,self.path,render=self.render,back_callback='owner:menu',notify=self.notify)
        self.ctx=NS(user_data={},application=self.app,args=[],bot=Mock())
        self.q=NS(data='pg:menu',from_user=NS(id=123),answer=AsyncMock(),message=NS(message_id=77))
        self.update=NS(effective_user=NS(id=123),effective_chat=NS(id=123,type='private'),callback_query=self.q,update_id=1)
    async def asyncTearDown(self):self.tmp.cleanup()
    async def invoke(self,name,update=None):
        handler=next(h for h in self.handlers if h.callback.__name__==name)
        try:await handler.callback(update or self.update,self.ctx)
        except ApplicationHandlerStop:pass
    def order(self):return {'id':'actual-reserved-id','amount':277,'pricing_json':json.dumps({'label':'30 дней','credit_amount':173})}
    async def test_catalog_uses_owner_menu_and_shows_credit_align_and_already_covered(self):
        price=lambda n:{'already_covered':n==0,'credit_amount':450-n}
        catalog={'plans':[{'id':'align','label':'До 28.10','XTR':200,'prices':{'XTR':price(200)}},{'id':'m1','label':'30 дней','XTR':277,'prices':{'XTR':price(277)}},{'id':'m3','label':'90 дней','XTR':0,'prices':{'XTR':price(0)}}]}
        with patch.object(client,'call',AsyncMock(return_value=catalog)):await self.invoke('callback')
        args=self.render.call_args.args;self.assertIn('зачтено 173',args[2]);self.assertIn('уже доступны',args[2])
        callbacks=[b.callback_data for row in args[3].inline_keyboard for b in row]
        self.assertIn('pg:buy:align',callbacks);self.assertNotIn('pg:buy:m3',callbacks);self.assertIn('owner:menu',callbacks)
    async def test_rub_and_stars_pairs_show_own_access_and_personal_credit(self):
        catalog={'rub_available':True,'website_url':'https://example.test/progressor/subscribe',
                 'subscriptions':{'products':[{'name':'Food','state':'active','expires_at':'2030-01-01T00:00:00Z'},{'name':'Supply','state':'inactive'}]},
                 'plans':[{'id':'m1','label':'30 дней','RUB':43000,'XTR':277,'prices':{'XTR':{'already_covered':False,'credit_amount':173},'RUB':{'already_covered':False,'credit_amount':26900}}}]}
        with patch.object(client,'call',AsyncMock(return_value=catalog)):await self.invoke('callback')
        text=self.render.call_args.args[2];keyboard=self.render.call_args.args[3]
        self.assertIn('Food — ✓ Доступ активен',text);self.assertIn('Supply — Нет подписки',text)
        self.assertIn('430 ₽ / 277 ⭐',text);self.assertIn('269 ₽ / 173 ⭐',text)
        pair=keyboard.inline_keyboard[0];self.assertEqual(len(pair),2)
        self.assertEqual(pair[0].url,'https://example.test/progressor/subscribe?source='+SOURCE+'&plan=m1')
        self.assertEqual(pair[1].callback_data,'pg:buy:m1')
        self.assertEqual(self.render.await_count,1)
    async def test_invoice_button_uses_actual_reserved_id_and_no_extra_invoice_message(self):
        self.q.data='pg:buy:m1'
        call=AsyncMock(side_effect=[self.order(),{'invoice_url':'https://t.me/$offline'}])
        with patch.object(client,'call',call):await self.invoke('callback')
        self.assertEqual(call.call_args_list[1].args[2]['order_id'],'actual-reserved-id')
        self.assertIn('277',self.render.call_args.args[2]);self.assertEqual(self.render.await_count,1)
    async def test_confirmed_expired_checkout_releases_exact_query_but_timeout_does_not(self):
        self.update.pre_checkout_query=NS(invoice_payload='progressor:'+SOURCE+':order',from_user=NS(id=123),total_amount=277,currency='XTR',id='query',answer=AsyncMock(side_effect=BadRequest('Query is too old and response timeout expired or query ID is invalid')))
        call=AsyncMock(return_value={'ok':True})
        with patch.object(client,'call',call):await self.invoke('precheckout')
        self.assertTrue(call.call_args.args[1].endswith('/precheckout-rejected'))
        self.update.pre_checkout_query.answer=AsyncMock(side_effect=TimedOut())
        call.reset_mock()
        with patch.object(client,'call',call):await self.invoke('precheckout')
        self.assertEqual(call.await_count,1)
    async def test_receipt_is_durable_before_offline_and_never_tracked_as_menu(self):
        payment=NS(invoice_payload='progressor:'+SOURCE+':order',total_amount=277,currency='XTR',telegram_payment_charge_id='receipt-charge')
        self.update.effective_message=NS(successful_payment=payment,refunded_payment=None,message_id=999)
        with patch.object(client,'call',AsyncMock(side_effect=TimeoutError())):await self.invoke('payment')
        with sqlite3.connect(self.path) as db:self.assertEqual(db.execute('SELECT count(*) FROM progressor_payment_outbox').fetchone()[0],1)
        self.assertIn('Платёж сохранён',self.render.call_args.args[2])
    async def test_expiry_deferred_has_one_selected_owner_and_no_unknown_retry(self):
        expiry=self.app.job_queue.run_repeating.call_args.args[0]
        item={'user_id':'123','event_key':'a'*64,'text':'Заканчивается подписка'}
        call=AsyncMock(side_effect=[{'items':[item]}, {'status':'queued'}])
        self.notify.side_effect=module.NotificationDeferred
        with patch.object(client,'call',call):await expiry(self.ctx)
        self.assertEqual(call.call_args.args[2]['status'],'deferred')
        self.notify.side_effect=TimedOut();call=AsyncMock(side_effect=[{'items':[item]},{}])
        with patch.object(client,'call',call):await expiry(self.ctx)
        self.assertEqual(call.call_args.args[2]['status'],'uncertain')
    def test_ongoing_input_and_recent_navigation_keep_the_owner_screen(self):
        for source,data in [('food',{'pending_meal':{'items':[1]}}),('gym',{'awaiting_exercise_note':1}),('supply',{'waiting':'mysupp:set_dose:5'}),(SOURCE,{'progressor_last_interaction':time.monotonic()})]:
            self.app.user_data[123]=data
            with self.assertRaises(module.NotificationDeferred):module.notification_preflight(self.app,123,source)
        self.app.user_data[123]={};module.notification_preflight(self.app,123,SOURCE)
    async def test_no_job_queue_uses_existing_lifecycle_preserves_hooks_and_stops_task(self):
        started=AsyncMock();stopped=AsyncMock();self.app.job_queue=None;self.app.post_init=started;self.app.post_stop=stopped
        with patch.object(client,'configured',return_value=True):module.install(self.app,SOURCE,self.path,render=self.render,back_callback='owner:menu',notify=self.notify)
        original_sleep=asyncio.sleep;ready=asyncio.Event()
        async def clock(seconds):await original_sleep(.001)
        async def call(*args,**kwargs):ready.set();return {'items':[]}
        with patch.object(module.asyncio,'sleep',clock),patch.object(client,'call',call):
            await self.app.post_init(self.app)
            try:await asyncio.wait_for(ready.wait(),1)
            finally:await self.app.post_stop(self.app)
        started.assert_awaited_once();stopped.assert_awaited_once()
        self.assertFalse(any(t.get_name()=='progressor_subscription_expiry_v2' for t in asyncio.all_tasks() if not t.done()))
