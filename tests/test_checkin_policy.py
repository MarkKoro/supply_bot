"""Check-ins suggest owned schedule changes; models cannot prescribe stored doses."""
import asyncio, json, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch
import db
import engine


class CheckinPolicy(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=str(Path(self.tmp.name)/'supply.sqlite3')
        await db.init_db(self.path)
        self.conn=await db.connect(self.path)
        self.uid=await db.get_or_create_user(self.conn,123)
        await db.set_user_tz(self.conn,123,'UTC')
        self.day=db.local_date('UTC')
        for sid in (1,2):
            await self.conn.execute('INSERT INTO supplements(id,slug,name,name_ru,name_en) VALUES(?,?,?,?,?)',
                                    (sid,'policy-'+str(sid),'Catalog '+str(sid),'Каталог '+str(sid),'Catalog '+str(sid)))
        await db.add_user_supplement(self.conn,123,1)
        await db.upsert_user_supp_settings(self.conn,self.uid,1,{'dose_text':'Моя этикетка'})
        await self.conn.commit()

    async def asyncTearDown(self):
        await self.conn.close()
        self.tmp.cleanup()

    def action(self,sid=1,kind='adjust'):
        return dict(id='a'+str(sid),type=kind,slug='policy-'+str(sid),supplement_id=sid,
                    suggested_time='08:00',dose_text='MODEL MUST NOT SET THIS',reason_short_ru='Time change')

    async def apply(self,actions):
        return await engine.apply_day_plan_actions_with_slots(self.conn,user_id=self.uid,tg_user_id=123,
                    date_local=self.day,target_day='today',ai_actions={'actions':actions})

    async def test_native_and_legacy_apply_take_only_owned_label_dose(self):
        self.assertEqual(await self.apply([self.action(),self.action(2,'add')]),2)
        rows=await db.list_day_plan_items(self.conn,self.uid,self.day)
        self.assertEqual({r['supplement_id']:r['dose_text'] for r in rows},{1:'Моя этикетка',2:None})
        await db.apply_day_plan_all(self.conn,self.uid,self.day,{'actions':[self.action(),self.action(2,'add')]})
        rows=await db.list_day_plan_items(self.conn,self.uid,self.day)
        self.assertEqual({r['supplement_id']:r['dose_text'] for r in rows},{1:'Моя этикетка',2:None})
        self.assertEqual(await db.trusted_label_dose(self.conn,self.uid,1),'Моя этикетка')

    async def test_new_taken_or_complex_membership_rejects_stale_proposal_without_clearing_plan(self):
        await self.apply([self.action()])
        before=await db.list_day_plan_items(self.conn,self.uid,self.day)
        await db.set_day_status(self.conn,123,self.day,1,'taken')
        with self.assertRaisesRegex(ValueError,'already_taken'): await self.apply([self.action(kind='remove')])
        self.assertEqual(await db.list_day_plan_items(self.conn,self.uid,self.day),before)
        await db.set_day_status(self.conn,123,self.day,1,None)
        complex_row=await db.create_complex(self.conn,123,'Owned complex')
        await self.conn.execute('INSERT INTO user_complex_items(complex_id,supplement_id) VALUES(?,1)',(complex_row['id'],))
        await self.conn.commit()
        with self.assertRaisesRegex(ValueError,'complex_plan_changed'): await self.apply([self.action()])
        self.assertEqual(await db.list_day_plan_items(self.conn,self.uid,self.day),before)

    async def test_old_saved_plan_dose_is_not_presented_as_owned_label(self):
        await self.apply([self.action(2,'add')])
        await self.conn.execute('UPDATE day_plan_items SET dose_text=? WHERE user_id=?',('OLD MODEL DOSE',self.uid))
        await self.conn.commit()
        before=await db.list_day_plan_items(self.conn,self.uid,self.day)
        groups=await db.get_today_plan_groups(self.conn,123,self.day)
        projected=[i for g in groups for i in g['items'] if i['supplement_id']==2]
        self.assertEqual(len(projected),1)
        self.assertEqual(projected[0]['dose_text'],'')
        import bot
        with patch.object(bot,'show',AsyncMock()) as show:
            await bot._show_today_plan(NS(),NS(),self.conn,123)
        text=show.await_args.args[2]
        self.assertNotIn('OLD MODEL DOSE',text)
        self.assertIn('Моя этикетка',text)
        self.assertEqual(before,await db.list_day_plan_items(self.conn,self.uid,self.day))

    async def test_parallel_repeat_preserves_first_owned_intake_dose_snapshot(self):
        original=db.fetchall;entered=asyncio.Event();release=asyncio.Event();waited=False
        other=await db.connect(self.path)
        async def pause_old_reader(conn,sql,params=()):
            nonlocal waited
            if conn is other and 'FROM day_plan_items' in sql and not waited:
                waited=True;entered.set();await release.wait()
            return await original(conn,sql,params)
        pending=None
        try:
            with patch.object(db,'fetchall',pause_old_reader):
                pending=asyncio.create_task(db.set_day_status(other,123,self.day,1,'taken'))
                await asyncio.wait_for(entered.wait(),2)
                await db.set_day_status(self.conn,123,self.day,1,'taken')
                await db.upsert_user_supp_settings(self.conn,self.uid,1,{'dose_text':'Новая этикетка'})
                release.set();await pending
            row=await db.fetchone(self.conn,'SELECT dose_snapshot_json FROM supplement_day_state WHERE user_id=? AND day=? AND supplement_id=1',(self.uid,self.day))
            self.assertEqual(json.loads(row['dose_snapshot_json']),['Моя этикетка'])
            await db.set_day_status(self.conn,123,self.day,1,None)
            await db.set_day_status(self.conn,123,self.day,1,'taken')
            row=await db.fetchone(self.conn,'SELECT dose_snapshot_json FROM supplement_day_state WHERE user_id=? AND day=? AND supplement_id=1',(self.uid,self.day))
            self.assertEqual(json.loads(row['dose_snapshot_json']),['Новая этикетка'])
        finally:
            release.set()
            if pending is not None and not pending.done():await pending
            await other.close()

    async def test_malformed_model_items_cannot_override_catalog_identity_or_create_actions(self):
        import webapp_backend as api
        data=[None,'invalid',{'slug':'policy-2','action':'unknown'},
              {'slug':'policy-2','action':'add','name_ru':'Invented name','priority':'invalid','dose_text':'unsafe'}]
        result=await api._normalize_recommendations(self.conn,data,stack_slugs={'policy-1'},candidate_slugs={'policy-2'})
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['name_ru'],'Каталог 2')
        self.assertEqual(result[0]['priority'],2)
        self.assertNotIn('dose_text',result[0])

    async def test_empty_model_result_is_not_replaced_by_forced_supplement(self):
        import webapp_backend as api
        answers=dict(energy='ok',sleep='ok',stress='medium',training='none',goals=['recovery'],note='Хочу больше энергии')
        with patch.object(api,'s',NS(DB_PATH=self.path,OPENAI_API_KEY='offline',OPENAI_MODEL='offline')), \
             patch.object(api,'_auth_user_id',AsyncMock(return_value=123)), \
             patch.object(api,'_premium_guard',AsyncMock(return_value=True)), \
             patch.object(api,'_sync_call',Mock(return_value='[]')), \
             patch('progressor_client.ai_context',AsyncMock(return_value=None)), \
             patch('progressor_checkin.changes',AsyncMock(return_value=[])), \
             patch.object(db,'build_goal_based_fallback_adds',AsyncMock()) as fallback:
            response=await api.api_checkin(api.CheckinBody(answers=answers,target='today'),'')
        self.assertEqual(response.status_code,200)
        self.assertEqual(json.loads(response.body)['recommendations'],[])
        fallback.assert_not_awaited()
