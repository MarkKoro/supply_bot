"""One offline Supply holdout: Berlin fold, automatic rest end and next slots.

Real due selection, claim ledger and native menu paths; fake clocks/Telegram only.
"""
import gc
import json
import os
import tempfile
import time
import unittest
from collections import defaultdict
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

import bot
import db
import meal_anchors
import ux
from progressor_links import cache_settings, ensure_schema, notifications_allowed
from telegram.error import RetryAfter


class CalendarRestFold(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.getenv('PROGRESSOR_TEST_TMP_DIR'))
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(ux._last_screen.pop, 123, None)
        self.path = str(Path(self.temp.name) / 'supply.sqlite3')
        self.current = datetime.fromisoformat('2026-10-25T00:30:10+00:00')
        case = self

        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return case.current.astimezone(tz) if tz else case.current.replace(tzinfo=None)

            @classmethod
            def utcnow(cls):
                return case.current.replace(tzinfo=None)

        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(db, 'datetime', Frozen))
        self.patches.enter_context(patch.object(bot, 'datetime', Frozen))
        self.patches.enter_context(patch.object(meal_anchors, 'datetime', Frozen))
        self.patches.enter_context(patch.object(time, 'time', lambda: self.current.timestamp()))
        self.patches.enter_context(patch('progressor_supply_hints.prepare', AsyncMock(return_value=None)))
        await db.init_db(self.path)
        self.conn = await db.connect(self.path)
        self.addCleanup(gc.collect)
        self.addAsyncCleanup(self.conn.close)
        self.uid = await db.get_or_create_user(self.conn, 123)
        await db.set_user_tz(self.conn, 123, 'Europe/Berlin')
        await self.conn.execute("UPDATE users SET premium_until_utc='2026-11-01T00:00:00Z' WHERE tg_user_id=123")
        await self.conn.execute("INSERT INTO supplements(id,slug,name,name_ru,name_en) VALUES(1,'h08-synthetic','Synthetic position','Синтетическая позиция','Synthetic position')")
        await self.conn.commit()
        await db.add_user_supplement(self.conn, 123, 1)
        # Native menu bootstrap fills missing default schedule rows. Establish
        # this normal owner state before the immutable holdout snapshot;
        # reminders must not be blamed for this independent initialization.
        defaults = await db.get_user_slots(self.conn, self.uid)
        self.assertEqual({v['slot_id']: v['time_local'] for v in defaults},
                         {'morning': '08:00', 'noon': '14:00', 'evening': '19:00'})
        await db.set_reminders_for_supplement(self.conn, 123, 1, 'custom', ['02:30', '03:30', '04:30'])
        # An actual prior-day owner mark is a sentinel, never today's intake.
        await db.set_day_status(self.conn, 123, '2026-10-24', 1, 'taken')
        await self.conn.executescript(meal_anchors.SCHEMA)
        ensure_schema(self.path)
        cache_settings(self.path, [{'user_id': '123', 'preferences': {
            'revision': 1, 'paused': False, 'pause_until': None,
            'notifications': True, 'sources': {'food': False, 'gym': False, 'supply': True}}}])
        self.attempts, self.accepted, self.deleted = [], [], []
        self.message_id = 100
        self.reject_first = True
        self.observations = []
        self.new_context()
        ux._last_screen.pop(123, None)

    def new_context(self):
        async def send(**payload):
            entry = {'utc': self.current.isoformat(), 'chat_id': payload['chat_id'],
                     'text': payload['text'], 'callbacks': [[b.callback_data for b in r]
                     for r in payload['reply_markup'].inline_keyboard]}
            self.attempts.append(entry)
            if self.reject_first:
                self.reject_first = False
                entry['outcome'] = 'explicit_retry_after_180'
                raise RetryAfter(180)
            self.message_id += 1
            entry.update(outcome='acknowledged', message_id=self.message_id)
            self.accepted.append(dict(entry))
            return NS(message_id=self.message_id, chat_id=payload['chat_id'])

        async def delete(**payload):
            self.deleted.append({'utc': self.current.isoformat(), **payload})

        telegram = NS(send_message=AsyncMock(side_effect=send),
                      edit_message_text=AsyncMock(side_effect=lambda **kw: NS(message_id=kw['message_id'], chat_id=kw['chat_id'])),
                      delete_message=AsyncMock(side_effect=delete), edit_message_reply_markup=AsyncMock())
        queue = Mock()
        queue.get_jobs_by_name.return_value = []
        app = NS(bot_data={'settings': NS(DB_PATH=self.path)}, user_data=defaultdict(dict),
                 chat_data=defaultdict(dict), bot=telegram, job_queue=queue)
        self.ctx = NS(application=app, bot=telegram, user_data=app.user_data[123], chat_data=app.chat_data[123])

    async def tables(self):
        names = await db.fetchall(self.conn, "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        result = {}
        for row in names:
            name = row['name']
            values = [dict(v) for v in await db.fetchall(self.conn, 'SELECT * FROM "' + name.replace('"', '""') + '"')]
            result[name] = sorted(values, key=lambda value: json.dumps(value, sort_keys=True, default=str))
        return result

    async def snapshot(self, label):
        local = self.current.astimezone(db.parse_utc_tz('Europe/Berlin'))
        value = {'label': label, 'utc': self.current.isoformat(), 'local': local.isoformat(),
                 'fold': local.fold, 'notifications_allowed': notifications_allowed(self.path, 123),
                 'due': [dict(v) for v in await db.list_due_reminders(self.conn)],
                 'tables': await self.tables(), 'attempts': list(self.attempts),
                 'acknowledged': list(self.accepted), 'deleted': list(self.deleted),
                 'native_ui': list(await db.get_ui_state(self.conn, 123))}
        self.observations.append(value)
        return value

    async def tick(self):
        await bot.reminders_tick(self.ctx)
        self.ctx.application.job_queue.run_once.assert_not_called()

    def at(self, value):
        self.current = datetime.fromisoformat(value.replace('Z', '+00:00'))

    async def test_fold_rest_expiry_suppresses_old_slot_but_next_two_work(self):
        initial = await self.snapshot('before_first_fold')
        self.assertEqual((initial['local'], initial['fold']), ('2026-10-25T02:30:10+02:00', 0), 'IANA tzdata is required by the harness')
        self.assertEqual([v['time_local'] for v in initial['due']], ['02:30'])
        await self.tick()
        first = await self.snapshot('fold0_known_retry')
        self.assertEqual(len(self.attempts), 1)
        self.assertEqual(self.accepted, [])
        self.assertEqual(first['tables']['reminder_fires'][0]['delivery_status'], 'pending')
        reserved = first['tables']['reminder_fires'][0]['first_reserved_at']

        self.at('2026-10-25T00:31:00Z')
        cache_settings(self.path, [{'user_id': '123', 'preferences': {
            'revision': 2, 'paused': True,
            'pause_until': datetime.fromisoformat('2026-10-25T01:20:00+00:00').timestamp(),
            'last_pause_started_at': self.current.timestamp(), 'notifications': True,
            'sources': {'food': False, 'gym': False, 'supply': True}}}])
        await self.conn.close()
        self.conn = await db.connect(self.path)
        self.addAsyncCleanup(self.conn.close)
        self.new_context()
        ux._last_screen.pop(123, None)
        reopened = await self.snapshot('rest_and_native_db_reopen')
        self.assertFalse(reopened['notifications_allowed'])
        self.assertEqual(reopened['tables']['reminder_fires'][0]['first_reserved_at'], reserved)

        self.at('2026-10-25T00:35:10Z')
        await self.tick()
        paused = await self.snapshot('rest_after_provider_retry_due')
        self.assertEqual(len(self.attempts), 1)
        self.assertEqual(paused['native_ui'], initial['native_ui'])

        self.at('2026-10-25T01:20:10Z')
        self.assertTrue(notifications_allowed(self.path, 123))
        await self.tick()
        resumed = await self.snapshot('automatic_rest_expiry_no_catchup')
        self.assertEqual(len(self.attempts), 1)
        self.assertEqual(resumed['tables']['reminder_fires'][0]['delivery_status'], 'suppressed')

        self.at('2026-10-25T01:30:10Z')
        fold = await self.snapshot('before_second_fold')
        self.assertEqual((fold['local'], fold['fold']), ('2026-10-25T02:30:10+01:00', 1))
        self.assertEqual([v['time_local'] for v in fold['due']], ['02:30'])
        await self.tick()
        await self.tick()
        folded = await self.snapshot('fold1_not_a_new_occurrence')
        self.assertEqual(len(self.attempts), 1)
        self.assertEqual(len(folded['tables']['reminder_fires']), 1)
        self.assertEqual(folded['tables']['reminder_fires'][0]['delivery_status'], 'suppressed')

        for instant, slot, total in [('2026-10-25T02:30:10Z', '03:30', 1),
                                     ('2026-10-25T03:30:10Z', '04:30', 2)]:
            self.at(instant)
            self.assertEqual([v['time_local'] for v in await db.list_due_reminders(self.conn)], [slot])
            await self.tick()
            await self.tick()
            self.assertEqual(len(self.accepted), total)
            await self.snapshot('next_current_slot_' + slot)

        final = self.observations[-1]
        ledger = final['tables']['reminder_fires']
        self.assertEqual({v['time_local']: v['delivery_status'] for v in ledger},
                         {'02:30': 'suppressed', '03:30': 'delivered', '04:30': 'delivered'})
        self.assertEqual(len(self.attempts), 3)
        self.assertEqual([v['message_id'] for v in self.accepted], [101, 102])
        self.assertIn(101, [v['message_id'] for v in self.deleted])
        self.assertNotIn(102, [v['message_id'] for v in self.deleted])
        self.assertEqual(final['native_ui'][0], 102)
        # Exhaustive row comparison: only delivery/menu/settings tables may change.
        allowed = {'reminder_fires', 'ui_state', 'users', 'progressor_link_cache'}
        for name in set(initial['tables']) | set(final['tables']):
            if name not in allowed:
                self.assertEqual(initial['tables'].get(name), final['tables'].get(name), name)
        before_user, after_user = initial['tables']['users'][0], final['tables']['users'][0]
        for name in before_user:
            if name not in {'last_chat_id', 'last_message_id', 'last_view', 'last_view_payload'}:
                self.assertEqual(before_user[name], after_user[name], name)
        self.assertEqual(len(final['tables']['supplement_day_state']), 1)
        self.assertEqual(final['tables']['supplement_day_state'][0]['day'], '2026-10-24')
        self.assertEqual(final['tables']['supplement_day_state'][0]['status'], 'taken')
        self.assertEqual(await db.get_day_status(self.conn, 123, '2026-10-25', 1), None)
