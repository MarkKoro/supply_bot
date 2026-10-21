"""Native scheduler + renderer + SQLite fault injection; never a Telegram socket."""
import asyncio
import gc
import json
import sqlite3
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

import bot
import db
import ux
from telegram.error import BadRequest, Forbidden, RetryAfter, TimedOut
import test_reminder_journey


class ReminderDelivery(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = test_reminder_journey.ReminderJourney()
        await self.fixture.asyncSetUp()
        self.conn, self.ctx = self.fixture.conn, self.fixture.ctx
        self.telegram, self.queue = self.fixture.telegram, self.fixture.queue
        user = await db.fetchone(self.conn, 'SELECT tz_name FROM users WHERE tg_user_id=123')
        self.day = db.get_user_local_date(user['tz_name'])
        self.due = {'user_id': 123, 'supplement_id': 1, 'time_local': '12:00',
                    'day': self.day, 'tz_name': user['tz_name']}

    async def asyncTearDown(self):
        gc.collect()
        await self.fixture.asyncTearDown()

    async def tick(self, due=None):
        with patch.object(db, 'list_due_reminders', AsyncMock(return_value=[due or self.due])), \
             patch('meal_anchors.due', AsyncMock(return_value=([], 0))), \
             patch('progressor_supply_hints.prepare', AsyncMock(return_value=None)):
            await bot.reminders_tick(self.ctx)

    async def row(self, slot='12:00'):
        return dict(await db.fetchone(self.conn,
            'SELECT * FROM reminder_fires WHERE user_id=123 AND supplement_id=1 AND time_local=?', (slot,)))

    async def test_accepted_then_lost_response_is_not_sent_twice(self):
        accepted = []
        async def provider(**payload):
            accepted.append(payload)
            if len(accepted) == 1:
                raise TimedOut('Lost response after simulated provider acceptance')
            return NS(message_id=900 + len(accepted), chat_id=123)
        self.telegram.send_message.side_effect = provider
        await self.tick()
        # Reopen the actual temporary DB: this outcome is durable, not a memory
        # flag that disappears with a scheduler or application restart.
        await self.conn.close()
        self.conn = self.fixture.conn = await db.connect(self.fixture.path)
        await self.conn.execute("UPDATE reminder_fires SET retry_at_utc='' WHERE user_id=123")
        await self.conn.commit()
        await self.tick()
        self.assertEqual(len(accepted), 1, 'Unknown acceptance must not cause an automatic duplicate')
        row = await self.row()
        self.assertEqual(row['delivery_status'], 'uncertain')
        self.assertEqual(row['fired_at'], '')
        self.assertIsNone(await db.get_day_status(self.conn, 123, self.day, 1))

    async def test_expired_sending_lease_is_uncertain_after_crash(self):
        token = await db.insert_reminder_fire(self.conn, 123, 1, self.day, '12:00')
        self.assertTrue(token)
        await self.conn.execute("UPDATE reminder_fires SET lease_until_utc='2000-01-01T00:00:00Z'")
        await self.conn.commit()
        await self.tick()
        self.telegram.send_message.assert_not_awaited()
        row = await self.row()
        self.assertEqual(row['delivery_status'], 'uncertain')
        self.assertEqual(row['attempts'], 1)
        self.assertIsNone(await db.insert_reminder_fire(self.conn, 123, 1, self.day, '12:00'))
        await db.finish_reminder_fire(self.conn, 123, 1, self.day, '12:00', token, delivered=True)
        self.assertEqual((await self.row())['delivery_status'], 'uncertain', 'Expired token cannot invent a late receipt')

    async def test_retry_after_is_safe_and_honors_provider_minimum(self):
        before = datetime.now(timezone.utc)
        self.telegram.send_message.side_effect = [RetryAfter(180), NS(message_id=901, chat_id=123)]
        await self.tick()
        row = await self.row()
        self.assertEqual(row['delivery_status'], 'pending')
        retry = datetime.fromisoformat(row['retry_at_utc'].replace('Z', '+00:00'))
        self.assertGreaterEqual((retry - before).total_seconds(), 180)
        await self.tick()
        self.assertEqual(self.telegram.send_message.await_count, 1)
        await self.conn.execute("UPDATE reminder_fires SET retry_at_utc=''")
        await self.conn.commit()
        await self.tick()
        self.assertEqual(self.telegram.send_message.await_count, 2)
        self.assertEqual((await self.row())['delivery_status'], 'delivered')

    async def test_forbidden_and_bad_request_are_permanent(self):
        for error in (Forbidden('blocked'), BadRequest('chat not found')):
            with self.subTest(error=type(error).__name__):
                await self.conn.execute('DELETE FROM reminder_fires')
                await self.conn.commit()
                self.telegram.send_message.reset_mock()
                self.telegram.send_message.side_effect = error
                await self.tick()
                await self.tick()
                self.telegram.send_message.assert_awaited_once()
                self.assertEqual((await self.row())['delivery_status'], 'failed')

    async def test_generic_pre_send_error_retries_but_send_error_is_uncertain(self):
        with patch('progressor_supply_hints.prepare', AsyncMock(side_effect=RuntimeError('preparation failed'))):
            with patch.object(db, 'list_due_reminders', AsyncMock(return_value=[self.due])), \
                 patch('meal_anchors.due', AsyncMock(return_value=([], 0))):
                await bot.reminders_tick(self.ctx)
        self.telegram.send_message.assert_not_awaited()
        row = await self.row()
        self.assertEqual(row['delivery_status'], 'pending')
        self.assertEqual(row['attempts'], 0)
        await self.conn.execute("UPDATE reminder_fires SET retry_at_utc=''")
        await self.conn.commit()
        self.telegram.send_message.side_effect = RuntimeError('transport outcome unknown')
        await self.tick()
        await self.tick()
        self.telegram.send_message.assert_awaited_once()
        self.assertEqual((await self.row())['delivery_status'], 'uncertain')

    async def test_acknowledged_send_then_persistence_retry_after_does_not_resend(self):
        with patch('ux._persist_last_state', AsyncMock(side_effect=RetryAfter(120))):
            await self.tick()
        await self.tick()
        self.telegram.send_message.assert_awaited_once()
        self.assertEqual((await self.row())['delivery_status'], 'delivered')

    async def test_acknowledged_new_menu_retires_owned_previous_even_if_sqlite_persist_fails(self):
        ux._last_screen[123] = 77
        await db.set_user_last_msg(self.conn, 123, 123, 77)
        with patch('ux._persist_last_state', AsyncMock(side_effect=sqlite3.OperationalError('disk I/O error'))):
            await self.tick()
        await self.tick()
        self.telegram.send_message.assert_awaited_once()
        self.telegram.delete_message.assert_any_await(chat_id=123, message_id=77)
        self.assertNotEqual(ux._last_screen[123], 77)
        self.assertEqual((await self.row())['delivery_status'], 'delivered')
        self.assertIsNone(await db.get_day_status(self.conn, 123, self.day, 1))

    async def test_cancelled_inflight_request_is_durable_uncertain_and_propagates(self):
        self.telegram.send_message.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.tick()
        self.assertEqual((await self.row())['delivery_status'], 'uncertain')
        await self.tick()
        self.telegram.send_message.assert_awaited_once()

    async def test_next_different_slot_stays_eligible_after_uncertain_send(self):
        self.telegram.send_message.side_effect = [TimedOut('unknown'), NS(message_id=902, chat_id=123)]
        await self.tick()
        await self.tick({**self.due, 'time_local': '18:00'})
        self.assertEqual(self.telegram.send_message.await_count, 2)
        self.assertEqual((await self.row())['delivery_status'], 'uncertain')
        self.assertEqual((await self.row('18:00'))['delivery_status'], 'delivered')

    async def test_meal_and_scheduled_share_the_same_uncertain_slot(self):
        self.telegram.send_message.side_effect = TimedOut('unknown')
        with patch.object(db, 'list_due_reminders', AsyncMock(return_value=[self.due])), \
             patch('meal_anchors.due', AsyncMock(return_value=([self.due], 1))), \
             patch('progressor_supply_hints.prepare', AsyncMock(return_value=None)):
            await bot.reminders_tick(self.ctx)
            await bot.reminders_tick(self.ctx)
        self.telegram.send_message.assert_awaited_once()
        rows = await db.fetchall(self.conn, 'SELECT * FROM reminder_fires')
        self.assertEqual(len(rows), 1)

    async def test_busy_defer_settles_original_ledger_in_one_native_menu(self):
        self.ctx.user_data['waiting'] = 'mysupp:set_dose:1'
        await self.tick()
        self.telegram.send_message.assert_not_awaited()
        self.assertEqual((await self.row())['delivery_status'], 'deferred')
        self.assertEqual((await self.row())['attempts'], 0)
        data = self.queue.run_once.call_args.kwargs['data']
        self.assertEqual(data['delivery_refs'], [{'supplement_id': 1, 'day': self.day, 'time_local': '12:00'}])
        await self.tick()
        self.telegram.send_message.assert_not_awaited()
        self.ctx.user_data.pop('waiting')
        self.ctx.job = NS(data=data)
        with patch('progressor_supply_hints.prepare', AsyncMock(return_value=None)):
            await bot.reminder_fire_job(self.ctx)
            await bot.reminder_fire_job(self.ctx)
        self.telegram.send_message.assert_awaited_once()
        row = await self.row()
        self.assertEqual(row['delivery_status'], 'delivered')
        self.assertEqual(row['attempts'], 1)
        self.assertIsNone(await db.get_day_status(self.conn, 123, self.day, 1))

    async def test_deferred_unknown_send_stays_uncertain_on_job_replay(self):
        self.ctx.user_data['waiting'] = 'mysupp:set_dose:1'
        await self.tick()
        data = self.queue.run_once.call_args.kwargs['data']
        self.ctx.user_data.pop('waiting')
        self.ctx.job = NS(data=data)
        self.telegram.send_message.side_effect = TimedOut('unknown')
        with patch('progressor_supply_hints.prepare', AsyncMock(return_value=None)):
            await bot.reminder_fire_job(self.ctx)
            await bot.reminder_fire_job(self.ctx)
        self.telegram.send_message.assert_awaited_once()
        self.assertEqual((await self.row())['delivery_status'], 'uncertain')

    async def test_mixed_busy_meal_and_scheduled_jobs_keep_original_refs_and_provenance(self):
        from progressor_links import cache_settings, ensure_schema
        ensure_schema(self.fixture.path)
        cache_settings(self.fixture.path, [{'user_id': '123', 'preferences':
            {'revision': 1, 'sources': {'food': True}}}])
        self.ctx.user_data['waiting'] = 'mysupp:set_dose:1'
        with patch.object(db, 'list_due_reminders', AsyncMock(return_value=[])), \
             patch('meal_anchors.due', AsyncMock(return_value=([self.due], 1))):
            await bot.reminders_tick(self.ctx)
        first = self.queue.run_once.call_args.kwargs['data']
        previous = NS(data=first, schedule_removal=Mock())
        self.queue.get_jobs_by_name.return_value = [previous]
        second_due = {**self.due, 'supplement_id': 2, 'time_local': '18:00'}
        await self.tick(second_due)
        merged = self.queue.run_once.call_args.kwargs['data']
        self.assertTrue(merged['meal_trigger'])
        self.assertCountEqual(merged['delivery_refs'], [
            {'supplement_id': 1, 'day': self.day, 'time_local': '12:00'},
            {'supplement_id': 2, 'day': self.day, 'time_local': '18:00'}])
        previous.schedule_removal.assert_called_once()
        self.telegram.send_message.assert_not_awaited()
        self.ctx.user_data.pop('waiting')
        self.ctx.job = NS(data=merged)
        with patch('progressor_supply_hints.prepare', AsyncMock(return_value=None)):
            await bot.reminder_fire_job(self.ctx)
            await bot.reminder_fire_job(self.ctx)
        self.telegram.send_message.assert_awaited_once()
        text = self.telegram.send_message.call_args.kwargs['text']
        self.assertIn('напоминание связано с ним по вашей настройке', text)
        self.assertNotIn('раньше выбранного времени', text)
        self.assertEqual((await self.row())['delivery_status'], 'delivered')
        second = await db.fetchone(self.conn, 'SELECT * FROM reminder_fires WHERE supplement_id=2')
        self.assertEqual(second['time_local'], '18:00')
        self.assertEqual(second['delivery_status'], 'delivered')
        self.assertCountEqual((await self.fixture.payload())['reminder_all_ids'], [1,2])

    async def test_mixed_deferred_job_does_not_resend_a_reference_already_uncertain(self):
        self.ctx.user_data['waiting'] = 'mysupp:set_dose:1'
        await self.tick()
        data = self.queue.run_once.call_args.kwargs['data']
        token = await db.insert_reminder_fire(self.conn, 123, 2, self.day, '18:00')
        await db.finish_reminder_fire(self.conn, 123, 2, self.day, '18:00', token, status='uncertain')
        data['supplement_ids'].append(2)
        data['delivery_refs'].append({'supplement_id': 2, 'day': self.day, 'time_local': '18:00'})
        self.ctx.user_data.pop('waiting')
        self.ctx.job = NS(data=data)
        with patch('progressor_supply_hints.prepare', AsyncMock(return_value=None)):
            await bot.reminder_fire_job(self.ctx)
        self.telegram.send_message.assert_awaited_once()
        self.assertEqual((await self.fixture.payload())['reminder_all_ids'], [1])
        second = await db.fetchone(self.conn, 'SELECT * FROM reminder_fires WHERE supplement_id=2')
        self.assertEqual(second['delivery_status'], 'uncertain')
        self.assertEqual(second['attempts'], 1)

    async def test_deferred_old_day_and_rest_do_not_catch_up(self):
        self.ctx.user_data['waiting'] = 'mysupp:set_dose:1'
        await self.tick()
        data = self.queue.run_once.call_args.kwargs['data']
        self.ctx.user_data.pop('waiting')
        self.ctx.job = NS(data={**data, 'deferred_day': '2000-01-01'})
        await bot.reminder_fire_job(self.ctx)
        self.telegram.send_message.assert_not_awaited()
        self.ctx.job = NS(data=data)
        # The same owner-local settings hook used by normal rest.
        with patch('progressor_links.notifications_allowed', return_value=False):
            await bot.reminder_fire_job(self.ctx)
        self.telegram.send_message.assert_not_awaited()
        self.assertEqual((await self.row())['delivery_status'], 'suppressed')

    async def test_pending_reserved_before_rest_does_not_catch_up_after_same_day_resume(self):
        from progressor_links import cache_settings, ensure_schema
        ensure_schema(self.fixture.path)
        self.telegram.send_message.side_effect = [RetryAfter(180), NS(message_id=905,chat_id=123)]
        await self.tick()
        paused_at = time.time()
        cache_settings(self.fixture.path, [{'user_id':'123','preferences':{'revision':1,
            'paused':True,'pause_until':None,'last_pause_started_at':paused_at}}])
        await self.conn.execute("UPDATE reminder_fires SET retry_at_utc=''")
        await self.conn.commit()
        await self.tick()
        cache_settings(self.fixture.path, [{'user_id':'123','preferences':{'revision':2,
            'paused':False,'pause_until':None,'last_pause_started_at':paused_at}}])
        # There is no fresh due input: only the persisted pending retry exists.
        with patch.object(db,'list_due_reminders',AsyncMock(return_value=[])), \
             patch('meal_anchors.due',AsyncMock(return_value=([],0))):
            await bot.reminders_tick(self.ctx)
        self.telegram.send_message.assert_awaited_once()
        self.assertEqual((await self.row())['delivery_status'], 'suppressed')
        await self.tick({**self.due,'time_local':'18:00'})
        self.assertEqual(self.telegram.send_message.await_count,2)
        self.assertEqual((await self.row('18:00'))['delivery_status'], 'delivered')

    async def test_known_retry_survives_startup_with_missing_or_older_cached_pause(self):
        from progressor_links import cache_settings, ensure_schema
        ensure_schema(self.fixture.path)
        for cutoff in (None, 1.0):
            with self.subTest(cutoff=cutoff):
                await self.conn.execute('DELETE FROM reminder_fires')
                await self.conn.commit()
                self.telegram.send_message.reset_mock()
                self.telegram.send_message.side_effect = [RetryAfter(90), NS(message_id=906,chat_id=123)]
                prefs={'revision':1 if cutoff is None else 2,'paused':False}
                if cutoff is not None:prefs['last_pause_started_at']=cutoff
                cache_settings(self.fixture.path,[{'user_id':'123','preferences':prefs}])
                await self.tick()
                before=await self.row()
                self.assertEqual(before['delivery_version'],1)
                self.assertGreater(before['first_reserved_at'],1)
                await db.init_db(self.fixture.path)
                after=await self.row()
                self.assertEqual(after['delivery_status'],'pending')
                self.assertEqual(after['first_reserved_at'],before['first_reserved_at'])
                await self.conn.execute("UPDATE reminder_fires SET retry_at_utc=''")
                await self.conn.commit()
                await self.tick()
                self.assertEqual(self.telegram.send_message.await_count,2)
                self.assertEqual((await self.row())['delivery_status'],'delivered')

    async def test_legacy_unknown_pending_migrates_once_without_changing_historical_delivery(self):
        # These legacy columns/defaults also model an old runtime inserting a
        # row on code rollback. New columns do not break its INSERT/SELECT.
        for slot,status in (('12:00','pending'),('18:00','delivered')):
            await self.conn.execute('''INSERT INTO reminder_fires
                (user_id,supplement_id,day,time_local,fired_at,delivery_status,attempts)
                VALUES(123,1,?,?,?, ?,1)''',(self.day,slot,'legacy-receipt' if status=='delivered' else '',status))
        await self.conn.commit()
        await db.init_db(self.fixture.path)
        await db.init_db(self.fixture.path)
        self.assertEqual((await self.row())['delivery_status'],'uncertain')
        historic=await self.row('18:00')
        self.assertEqual(historic['delivery_status'],'delivered')
        self.assertEqual(historic['fired_at'],'legacy-receipt')
        self.assertFalse(await db.list_retryable_reminder_fires(self.conn))
        # The old claim predicate also cannot reopen either terminal state.
        cur=await self.conn.execute("""UPDATE reminder_fires SET delivery_status='sending'
            WHERE user_id=123 AND delivery_status IN ('pending','sending')
            AND retry_at_utc<=? AND lease_until_utc<=?""",(db.utc_now_iso(),db.utc_now_iso()))
        self.assertEqual(cur.rowcount,0)
        await self.conn.commit()
        self.telegram.send_message.assert_not_awaited()

    async def test_deferred_job_created_before_rest_is_not_replayed_if_pause_job_never_ran(self):
        from progressor_links import cache_settings, ensure_schema
        self.ctx.user_data['waiting']='mysupp:set_dose:1'
        await self.tick()
        data=self.queue.run_once.call_args.kwargs['data']
        ensure_schema(self.fixture.path)
        cutoff=time.time()
        # The callback was not executed during the pause. The cached cutoff
        # alone must still suppress its original reservation after return.
        cache_settings(self.fixture.path,[{'user_id':'123','preferences':{'revision':2,
            'paused':False,'last_pause_started_at':cutoff}}])
        self.ctx.user_data.pop('waiting')
        self.ctx.job=NS(data=data)
        await bot.reminder_fire_job(self.ctx)
        self.telegram.send_message.assert_not_awaited()
        self.assertEqual((await self.row())['delivery_status'],'suppressed')

    async def test_explicit_snooze_job_remains_one_native_send(self):
        self.ctx.job = NS(data={'tg_user_id': 123, 'supplement_ids': [1], 'deferred_day': self.day})
        with patch('progressor_supply_hints.prepare', AsyncMock(return_value=None)):
            await bot.reminder_fire_job(self.ctx)
        self.telegram.send_message.assert_awaited_once()
        payload = await self.fixture.payload()
        self.assertEqual(payload['reminder_day'], self.day)
        self.assertEqual(payload['reminder_batch_ids'], [1])
