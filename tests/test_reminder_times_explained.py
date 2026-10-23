"""Reminder explanation reuses native split, preserving day marks and history."""
import tempfile
import json
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import bot
import db


class ReminderTimesExplained(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'owner.sqlite3')
        await db.init_db(self.path)
        self.conn = await db.connect(self.path)
        self.uid = await db.get_or_create_user(self.conn, 123)
        await db.set_user_tz(self.conn, 123, 'UTC')
        await db.save_user_consent(self.conn, 123, offer_version='test', privacy_version='test', source='offline')
        await self.conn.execute("INSERT INTO supplements(id,slug,name,name_ru,name_en) VALUES(1,'one','Item','Добавка','Item')")
        await self.conn.commit()
        await db.add_user_supplement(self.conn, 123, 1)
        self.context = NS(user_data={}, application=NS(bot_data={'settings': NS(
            DB_PATH=self.path, OFFER_VERSION='test', PRIVACY_VERSION='test')}))

    async def asyncTearDown(self):
        await self.conn.close()
        self.temp.cleanup()

    def update(self, callback):
        return NS(effective_user=NS(id=123), callback_query=NS(
            data=callback, answer=AsyncMock(), message=NS(message_id=77, chat_id=123)))

    async def call(self, callback):
        with patch.object(bot, 'guard_not_banned', AsyncMock(return_value=True)), \
             patch.object(bot, 'show', AsyncMock()) as show:
            await bot.on_menu(self.update(callback), self.context)
        return show

    async def test_multiple_times_explain_day_mark_and_offer_one_existing_split_action(self):
        for time in ('08:00', '20:00'):
            await db.add_reminder_for_supplement(self.conn, 123, 1, time)
        before = await db.fetchall(self.conn, 'SELECT * FROM supplement_day_state')
        show = await self.call('mysupp:rem:1')
        text, markup = show.call_args.args[2:4]
        self.assertIn('повторные напоминания об одном приёме', text)
        self.assertIn('«Пропуск» закрывает их на сегодня', text)
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertEqual(callbacks.count('mysupp:split:1'), 1)
        self.assertIn('mysupp:open:1', callbacks)
        self.assertEqual(await db.fetchall(self.conn, 'SELECT * FROM supplement_day_state'), before)
        self.assertEqual((await db.fetchone(self.conn, 'SELECT count(*) AS n FROM supplements'))['n'], 1)

    async def test_single_time_does_not_add_a_repeated_reminder_nudge(self):
        await db.add_reminder_for_supplement(self.conn, 123, 1, '08:00')
        show = await self.call('mysupp:rem:1')
        text, markup = show.call_args.args[2:4]
        self.assertNotIn('повторные напоминания', text)
        self.assertNotIn('mysupp:split:1', [b.callback_data for row in markup.inline_keyboard for b in row])

    async def test_explained_action_reuses_pending_split_and_cancel_preserves_original(self):
        for time in ('08:00', '20:00'):
            await db.add_reminder_for_supplement(self.conn, 123, 1, time)
        day = db.get_user_local_date('UTC')
        await db.set_day_status(self.conn, 123, day, 1, 'taken')
        original = dict(await db.fetchone(self.conn, 'SELECT * FROM supplement_day_state'))
        first = await self.call('mysupp:split:1')
        created = int(self.context.user_data['split_flow']['new_id'])
        self.assertNotEqual(created, 1)
        self.assertIn('Создан второй приём', first.call_args.args[2])
        await self.call('mysupp:split:1')
        self.assertEqual(self.context.user_data['split_flow']['new_id'], created)
        self.assertEqual((await db.fetchone(self.conn, 'SELECT count(*) AS n FROM supplements'))['n'], 2)
        await db.set_user_schedule_time(self.conn, 123, created, '20:00')
        self.assertIsNone(await db.get_day_status(self.conn, 123, day, created))
        self.assertEqual(await db.get_day_status(self.conn, 123, day, 1), 'taken')
        await db.set_day_status(self.conn, 123, day, created, 'skipped')
        self.assertEqual(dict(await db.fetchone(self.conn, 'SELECT * FROM supplement_day_state WHERE supplement_id=1')), original)
        with patch.object(bot, 'show_my_supp_hub', AsyncMock()) as returned:
            await self.call(f'mysupp:split:cancel:{created}:1')
        self.assertEqual(returned.call_args.args[3], 1)
        self.assertNotIn('split_flow', self.context.user_data)
        self.assertFalse(await db.user_has_supplement(self.conn, 123, created))
        self.assertEqual([r['time_local'] for r in await db.list_reminders_for_user(self.conn, 123, 1)], ['08:00', '20:00'])
        self.assertEqual(dict(await db.fetchone(self.conn, 'SELECT * FROM supplement_day_state WHERE supplement_id=1')), original)

    async def test_native_cancel_then_split_in_same_second_keeps_old_copy_and_uses_new_id(self):
        day = db.get_user_local_date('UTC')
        await db.set_day_status(self.conn, 123, day, 1, 'taken')
        original = dict(await db.fetchone(self.conn, 'SELECT * FROM supplement_day_state'))
        with patch.object(db.time, 'time', return_value=1800000000):
            await self.call('mysupp:split:1')
            canceled = self.context.user_data['split_flow']['new_id']
            with patch.object(bot, 'show_my_supp_hub', AsyncMock()):
                await self.call(f'mysupp:split:cancel:{canceled}:1')
            await self.call('mysupp:split:1')
            self.assertIn('split_flow', self.context.user_data, 'same-second retry must open a fresh existing split route')
            created = self.context.user_data['split_flow']['new_id']
            self.assertNotEqual(created, canceled)
            await self.call('mysupp:split:1')
            self.assertEqual(self.context.user_data['split_flow']['new_id'], created)
        self.assertEqual((await db.fetchone(self.conn, 'SELECT count(*) AS n FROM supplements'))['n'], 3)
        self.assertIsNotNone(await db.fetchone(self.conn, 'SELECT id FROM supplements WHERE id=?', (canceled,)))
        self.assertFalse(await db.user_has_supplement(self.conn, 123, canceled))
        self.assertTrue(await db.user_has_supplement(self.conn, 123, created))
        self.assertEqual(dict(await db.fetchone(self.conn, 'SELECT * FROM supplement_day_state')), original)

    async def test_web_cancel_then_split_in_same_second_preserves_original_history(self):
        import webapp_backend as api
        day = db.get_user_local_date('UTC')
        await db.set_day_status(self.conn, 123, day, 1, 'skipped')
        original = dict(await db.fetchone(self.conn, 'SELECT * FROM supplement_day_state'))
        with patch.object(api, 's', NS(DB_PATH=self.path)), \
             patch.object(api, '_auth_user_id', AsyncMock(return_value=123)), \
             patch.object(api, '_premium_guard', AsyncMock(return_value=True)), \
             patch.object(db.time, 'time', return_value=1800000000):
            first = await api.api_my_supplements_split(api.MySuppSplitBody(supplement_id=1), '')
            canceled = json.loads(first.body)['created']['supplement_id']
            removed = await api.api_my_supplements_remove(api.MySuppDeleteBody(supplement_id=canceled), '')
            self.assertEqual(removed.status_code, 200)
            again = await api.api_my_supplements_split(api.MySuppSplitBody(supplement_id=1), '')
            self.assertEqual(again.status_code, 200)
            created = json.loads(again.body)['created']['supplement_id']
        self.assertNotEqual(created, canceled)
        self.assertIsNotNone(await db.fetchone(self.conn, 'SELECT id FROM supplements WHERE id=?', (canceled,)))
        self.assertFalse(await db.user_has_supplement(self.conn, 123, canceled))
        self.assertTrue(await db.user_has_supplement(self.conn, 123, created))
        self.assertEqual(dict(await db.fetchone(self.conn, 'SELECT * FROM supplement_day_state')), original)
