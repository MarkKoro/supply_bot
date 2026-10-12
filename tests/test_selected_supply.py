import json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import AsyncMock
import db,supplement_overlap as overlap
from progressor_checkin import changes,summary

class SelectedSupply(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=str(Path(self.tmp.name)/'supply.db');await db.init_db(self.path);self.conn=await db.connect(self.path)
        self.uid=await db.get_or_create_user(self.conn,123);await db.get_or_create_user(self.conn,456)
        for sid,name in ((1,'Витамин B12'),(2,'Комплекс витаминов'),(3,'Неизвестный состав'),(4,'Витамин B12')):
            await self.conn.execute('INSERT INTO supplements(id,slug,name,name_ru,name_en) VALUES(?,?,?,?,?)',(sid,'test-'+str(sid),name,name,name))
            await db.add_user_supplement(self.conn,123,sid)
        await self.conn.execute("INSERT INTO user_complexes(id,user_id,name,created_at_utc) VALUES(10,?,'Мой комплекс','2026-10-09T00:00:00Z')",(self.uid,))
        await self.conn.execute('INSERT INTO user_complex_items(complex_id,supplement_id) VALUES(10,2)')
        await self.conn.execute("INSERT INTO user_complex_schedules(user_id,complex_id,slot_id,time_local,is_enabled,created_at_utc) VALUES(?,10,'morning','08:00',1,'2026-10-09T00:00:00Z')",(self.uid,))
        await self.conn.execute('UPDATE user_supplements SET enabled=0 WHERE supplement_id=4');await self.conn.commit()
    async def asyncTearDown(self):await self.conn.close();self.tmp.cleanup()
    async def test_complex_and_separate_same_substance_unknown_forms_not_sum(self):
        await overlap.save(self.conn,123,1,[{'code':'vitamin_b12','form':'метилкобаламин','amount':100,'unit':'ug'}],0)
        await overlap.save(self.conn,123,2,[{'code':'vitamin_b12','form':'цианокобаламин','amount':.1,'unit':'mg'}],0)
        r=await overlap.check(self.conn,123);self.assertEqual(len(r['warnings']),1)
        w=r['warnings'][0];self.assertEqual(w['code'],'vitamin_b12');self.assertEqual({p['unit'] for p in w['products']},{'ug','mg'})
        self.assertTrue(any(any(s.startswith('Комплекс') for s in p['containers']) for p in w['products']))
        self.assertNotIn('total_amount',w);self.assertGreaterEqual(r['unknown_composition_count'],1)
    async def test_same_item_complex_mirror_same_slot_is_not_duplicate(self):
        await self.conn.execute('UPDATE user_supplements SET enabled=0 WHERE supplement_id=1');await self.conn.commit()
        self.assertFalse((await overlap.check(self.conn,123))['warnings'])
    async def test_explicit_native_split_is_not_two_products(self):
        await self.conn.execute('UPDATE user_supplements SET enabled=0 WHERE supplement_id=2')
        await self.conn.execute('UPDATE user_complex_schedules SET is_enabled=0')
        await self.conn.execute('UPDATE user_supplements SET enabled=1 WHERE supplement_id=4')
        await self.conn.execute('INSERT INTO user_supplement_split_groups(user_id,base_supplement_id,supplement_id,intake_order,created_at) VALUES(?,1,4,2,?)',(self.uid,db.utc_now_iso()));await self.conn.commit()
        self.assertFalse((await overlap.check(self.conn,123))['warnings'])
    async def test_other_owner_and_changed_revision_rejected(self):
        with self.assertRaises(ValueError):await overlap.save(self.conn,456,1,[{'code':'iron'}],0)
        await overlap.save(self.conn,123,2,[{'code':'iron'}],0)
        with self.assertRaises(ValueError):await overlap.save(self.conn,123,2,[{'code':'zinc'}],0)
        a=await overlap.save(self.conn,123,2,[{'code':'iron'}],0);self.assertEqual(a['revision'],1)
    async def test_label_values_validate_unknown_units_and_preserve_plan(self):
        before=[dict(r) for r in await db.fetchall(self.conn,'SELECT * FROM user_supplements')]
        for entries in ([{'code':'unknown'}],[{'code':'iron','amount':True,'unit':'mg'}],[{'code':'iron','amount':1,'unit':'drops'}],[{'code':'iron'},{'code':'iron'}]):
            with self.assertRaises(ValueError):await overlap.save(self.conn,123,1,entries,0)
        await overlap.save(self.conn,123,1,[{'code':'iron'}],0)
        self.assertEqual(before,[dict(r) for r in await db.fetchall(self.conn,'SELECT * FROM user_supplements')])
    async def test_poor_checkin_requests_only_bounded_confirmed_changes(self):
        control=AsyncMock(return_value={'items':[{'text':'Новая цель Food'},{'text':'Вес Supply изменён'}]})
        self.assertFalse(await changes(123,{'energy':3,'stress':1},control));control.assert_not_awaited()
        a=await changes(123,{'energy':1},control);self.assertEqual(len(a),2);self.assertIn('не установленная причина',summary(a))
        self.assertEqual(control.call_args.args[2],{'user_id':'123'})
        control.side_effect=RuntimeError('offline');self.assertFalse(await changes(123,{'stress':3},control))

    async def test_meal_anchor_opt_in_native_fallback_owner_and_durable_dedup(self):
        from datetime import datetime,timezone,timedelta
        import meal_anchors
        now=datetime(2026,10,9,12,tzinfo=timezone.utc);slot=(now+timedelta(minutes=45)).strftime('%H:%M')
        await db.set_user_tz(self.conn,123,'UTC')
        await db.add_reminder_for_supplement(self.conn,123,1,slot)
        with self.assertRaises(ValueError):await meal_anchors.save(self.conn,456,1,slot,True)
        await meal_anchors.save(self.conn,123,1,slot,True)
        await self.conn.execute('UPDATE progressor_meal_anchors SET enabled_at=?',(now.timestamp()-300,));await self.conn.commit()
        meal={'entity_id':'meal:private','occurred_at':(now-timedelta(minutes=1)).isoformat()}
        control=AsyncMock(return_value={'items':[{'user_id':'123','meals':[meal]}]})
        values,_=await meal_anchors.due(self.conn,control=control,now=now)
        self.assertEqual(len(values),1);r=values[0];self.assertEqual(r['time_local'],slot)
        first=await db.insert_reminder_fire(self.conn,123,1,r['day'],slot);self.assertTrue(first)
        self.assertIsNone(await db.insert_reminder_fire(self.conn,123,1,r['day'],slot))
        await db.finish_reminder_fire(self.conn,123,1,r['day'],slot,first,delivered=True)
        self.assertIsNone(await db.insert_reminder_fire(self.conn,123,1,r['day'],slot))
        await meal_anchors.save(self.conn,123,1,slot,False)
        self.assertFalse((await meal_anchors.due(self.conn,control=control,now=now))[0])
        self.assertIn(slot,(await db.get_effective_reminders_for_supplement(self.conn,123,1))['times'])

    async def test_meal_anchor_late_old_unavailable_or_changed_time_uses_native_slot(self):
        from datetime import datetime,timezone,timedelta
        import meal_anchors
        now=datetime(2026,10,9,12,tzinfo=timezone.utc);slot=(now+timedelta(minutes=40)).strftime('%H:%M')
        await db.set_user_tz(self.conn,123,'UTC');await db.add_reminder_for_supplement(self.conn,123,1,slot)
        await meal_anchors.save(self.conn,123,1,slot,True)
        for meals in ([],[{'occurred_at':(now-timedelta(hours=2)).isoformat()}],[{'occurred_at':(now+timedelta(minutes=1)).isoformat()}]):
            control=AsyncMock(return_value={'items':[{'user_id':'123','meals':meals}]})
            self.assertFalse((await meal_anchors.due(self.conn,control=control,now=now))[0])
        self.assertFalse((await meal_anchors.due(self.conn,control=AsyncMock(side_effect=TimeoutError),now=now))[0])
        with self.assertRaises(ValueError):await meal_anchors.save(self.conn,123,1,'invalid',True)

    async def test_timezone_preview_is_read_only_cas_and_preserves_intake_dates(self):
        import progressor_timezone as clock
        await db.set_user_tz(self.conn,123,'UTC')
        before=[dict(r) for r in await db.fetchall(self.conn,'SELECT * FROM reminders')]
        value=await clock.change(self.conn,123,{'action':'preview','timezone':'Asia/Bangkok'})
        self.assertFalse(value['applied']);self.assertEqual(await db.get_user_tz(self.conn,123),'UTC')
        await clock.change(self.conn,123,{'action':'apply','timezone':'Asia/Bangkok','expected_timezone':'UTC'})
        with self.assertRaises(ValueError):await clock.change(self.conn,123,{'action':'apply','timezone':'Europe/Moscow','expected_timezone':'UTC'})
        self.assertEqual(before,[dict(r) for r in await db.fetchall(self.conn,'SELECT * FROM reminders')])

    async def test_rest_deferred_reminder_does_not_replace_menu_or_send_catchup(self):
        import bot
        from types import SimpleNamespace
        from unittest.mock import patch,Mock
        from progressor_links import ensure_schema,cache_settings
        ensure_schema(self.path);cache_settings(self.path,[{'user_id':'123','preferences':{'revision':1,'paused':True,'pause_until':None}}])
        ctx=SimpleNamespace(application=SimpleNamespace(bot_data={'settings':SimpleNamespace(DB_PATH=self.path)}),bot=Mock(),job_queue=Mock())
        with patch.object(bot,'show',AsyncMock()) as show:
            await bot.reminder_fire_batch(ctx,123,[1])
        show.assert_not_awaited();ctx.job_queue.run_once.assert_not_called()

    async def test_atlas_returns_to_same_native_card_and_paginated_choice_without_ai(self):
        import bot
        from types import SimpleNamespace as NS
        from unittest.mock import patch
        from telegram.ext import ApplicationHandlerStop
        await db.save_ui_state(self.conn,123,77,'supp:tab:1:dose','{}')
        q=NS(data='foods:supp:1',answer=AsyncMock());update=NS(effective_chat=NS(id=123,type='private'),effective_user=NS(id=123),callback_query=q)
        ctx=NS(user_data={},application=NS(bot_data={'settings':NS(DB_PATH=self.path)}))
        with patch.object(bot,'guard_not_banned',AsyncMock(return_value=True)),patch.object(bot,'show',AsyncMock()) as show:
            for action in ('foods:supp:1','foods:list:1:1','foods:item:1:vitamin_b12:1'):
                q.data=action
                with self.assertRaises(ApplicationHandlerStop):await bot.on_food_sources(update,ctx)
                buttons=[b for row in show.call_args.args[3].inline_keyboard for b in row]
                self.assertIn('supp:tab:1:dose',[b.callback_data for b in buttons])
            self.assertIn('обычные',show.call_args.args[2].lower())

    async def test_paused_lifecycle_rows_do_not_starve_another_user(self):
        from progressor_links import ensure_schema,cache_settings
        other=(await db.fetchone(self.conn,'SELECT id FROM users WHERE tg_user_id=456'))['id']
        for uid in (self.uid,other):
            await self.conn.execute("INSERT INTO nutrient_plan_runs(user_id,nutrient_code,preview_snapshot_json,plan_snapshot_json,plan_mode,plan_phase,confidence_level,created_at_utc,updated_at_utc,course_ends_at_utc,lifecycle_status) VALUES(?,'iron','{}','{}','test','test','unknown',?,?,?,'active')",(uid,db.utc_now_iso(),db.utc_now_iso(),'2000-01-01T00:00:00Z'))
        await self.conn.commit();ensure_schema(self.path);cache_settings(self.path,[{'user_id':'123','preferences':{'revision':1,'paused':True,'pause_until':None}}])
        await db.process_due_nutrient_lifecycle(self.conn,limit=1)
        statuses=await db.fetchall(self.conn,'SELECT user_id,lifecycle_status FROM nutrient_plan_runs ORDER BY id')
        self.assertEqual(statuses[0]['lifecycle_status'],'active');self.assertEqual(statuses[1]['lifecycle_status'],'review_required')
