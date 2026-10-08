"""Actual Supply reminder/UI/intake handlers on owned temporary SQLite."""
import json,os,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace as NS
from collections import defaultdict
from unittest.mock import AsyncMock,Mock,patch
import db,bot,ux
from telegram.ext import ApplicationHandlerStop

class ReminderJourney(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=os.getenv('PROGRESSOR_TEST_TMP_DIR'));self.path=str(Path(self.tmp.name)/'supply.db')
        await db.init_db(self.path);self.conn=await db.connect(self.path)
        await db.get_or_create_user(self.conn,123)
        for sid in range(1,21):
            await self.conn.execute('INSERT INTO supplements(id,slug,name,name_ru,name_en) VALUES(?,?,?,?,?)',(sid,'test-'+str(sid),'Название'+str(sid),'Название'+str(sid),'Name'+str(sid)))
            await db.add_user_supplement(self.conn,123,sid)
        await self.conn.commit();self.msg=80
        async def send(**kw):self.msg+=1;return NS(message_id=self.msg,chat_id=kw['chat_id'])
        async def edit(**kw):return NS(message_id=kw['message_id'],chat_id=kw['chat_id'])
        self.telegram=NS(send_message=AsyncMock(side_effect=send),edit_message_text=AsyncMock(side_effect=edit),delete_message=AsyncMock(),edit_message_reply_markup=AsyncMock())
        self.queue=Mock();self.queue.get_jobs_by_name.return_value=[]
        self.app=NS(bot_data={'settings':NS(DB_PATH=self.path)},user_data=defaultdict(dict),chat_data=defaultdict(dict),bot=self.telegram,job_queue=self.queue)
        self.ctx=NS(application=self.app,bot=self.telegram,user_data=self.app.user_data[123],chat_data=self.app.chat_data[123])
        ux._last_screen.pop(123,None)
    async def asyncTearDown(self):await self.conn.close();ux._last_screen.pop(123,None);self.tmp.cleanup()
    async def payload(self):return json.loads((await db.get_ui_state(self.conn,123))[2])
    def update(self,data,message):
        q=NS(data=data,message=NS(message_id=message,chat_id=123,delete=AsyncMock(),edit_text=AsyncMock()),answer=AsyncMock(),from_user=NS(id=123))
        return NS(callback_query=q,effective_user=NS(id=123),effective_chat=NS(id=123,type='private'),effective_message=q.message)
    async def test_all_positions_page_and_mark_in_one_owned_menu(self):
        with patch('progressor_supply_hints.prepare',AsyncMock(return_value=None)):
            await bot.reminder_fire_batch(self.ctx,123,list(range(1,21)))
            state=await self.payload();self.assertEqual(len(state['reminder_all_ids']),20);self.assertEqual(len(state['reminder_batch_ids']),8)
            message=state['reminder_msg_id'];day=state['reminder_day']
            update=self.update(f'rem:page:1:{day}',message)
            with patch.object(bot,'guard_not_banned',AsyncMock(return_value=True)):
                with self.assertRaises(ApplicationHandlerStop):await bot.on_progressor_supply_action(update,self.ctx)
            self.assertEqual((await self.payload())['reminder_msg_id'],message)
            self.assertFalse(any(c.kwargs['message_id']==message for c in self.telegram.delete_message.call_args_list))
            ids=(await self.payload())['reminder_batch_ids']
            callback='rem:done_all:d'+day.replace('-','')+':'+','.join(map(str,ids))
            marked=self.update(callback,message)
            with patch.object(bot,'guard_not_banned',AsyncMock(return_value=True)):
                await bot.on_reminder_action(marked,self.ctx)
            state=await self.payload();self.assertEqual(len(state['reminder_all_ids']),12)
            for sid in ids:self.assertEqual(await db.get_day_status(self.conn,123,day,sid),'taken')
            self.assertEqual(state['reminder_msg_id'],message);self.telegram.send_message.assert_awaited_once()
            markup=marked.callback_query.message.edit_text.call_args.kwargs['reply_markup']
            self.assertTrue(all(len((b.callback_data or '').encode())<=64 for row in markup.inline_keyboard for b in row))
    async def test_deferred_input_sends_nothing_and_keeps_pending_medication_job(self):
        self.ctx.user_data['waiting']='mysupp:set_dose:1'
        await bot.reminder_fire_batch(self.ctx,123,[1,2])
        self.telegram.send_message.assert_not_awaited();self.telegram.edit_message_text.assert_not_awaited()
        data=self.queue.run_once.call_args.kwargs['data'];self.assertEqual(data['supplement_ids'],[1,2]);self.assertIn('deferred_day',data)
        self.assertEqual(self.ctx.user_data['waiting'],'mysupp:set_dose:1')
    async def test_stale_page_does_not_change_or_mark_owned_positions(self):
        with patch('progressor_supply_hints.prepare',AsyncMock(return_value=None)):await bot.reminder_fire_batch(self.ctx,123,[1,2])
        before=await self.payload();self.telegram.edit_message_text.reset_mock()
        with patch.object(bot,'guard_not_banned',AsyncMock(return_value=True)):
            with self.assertRaises(ApplicationHandlerStop):await bot.on_progressor_supply_action(self.update('rem:page:1:2000-01-01',before['reminder_msg_id']),self.ctx)
        self.assertEqual(await self.payload(),before);self.telegram.edit_message_text.assert_not_awaited()
    async def test_nutrient_action_opens_native_manual_catalog_without_dose_or_ai(self):
        with patch.object(bot,'guard_not_banned',AsyncMock(return_value=True)),patch.object(bot,'render_search_results',AsyncMock()) as catalogue:
            with self.assertRaises(ApplicationHandlerStop):await bot.on_progressor_supply_action(self.update('pgnut:b12',77),self.ctx)
        self.assertEqual(catalogue.await_count,1)
        self.assertIn('B12',str(catalogue.call_args))
