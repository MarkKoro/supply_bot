"""Owned check-in cache publication survives purge/reclaim without real providers."""
import asyncio
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import ExitStack, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import db
import progressor_ai_cache as cache


OWN, OTHER = 900000000111, 900000000222
FINANCIAL_FIELDS = ('premium_until_utc', 'premium_plan', 'premium_source', 'premium_updated_at_utc',
                    'starter_trial_used', 'starter_trial_started_at_utc',
                    'starter_trial_until_utc', 'starter_trial_source')


class CheckinCachePublication(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'supply.sqlite3')
        await db.init_db(self.path)
        self.conn = await db.connect(self.path)
        self.ids = {}
        for tg_uid in (OWN, OTHER):
            self.ids[tg_uid] = await db.get_or_create_user(self.conn, tg_uid)
            await db.set_user_tz(self.conn, tg_uid, 'UTC')
        self.assertNotIn(self.ids[OWN], (OWN, OTHER))
        self.assertNotEqual(self.ids[OWN], self.ids[OTHER])
        self.day = db.local_date('UTC')
        self.answers = {'fixture': 'original answers'}
        self.row, self.token = await db.reserve_daily_checkin(
            self.conn, OWN, self.day, self.answers, now=1000, token='original-claim')
        self.request_key = cache.key({'fixture': 'owned publication'})

    async def asyncTearDown(self):
        await self.conn.close()
        self.tmp.cleanup()

    def fence(self, *, tg_uid=OWN, user_id=None, checkin_id=None, token=None):
        return lambda conn: db.daily_checkin_cache_publishable(
            conn, tg_user_id=tg_uid, user_id=self.ids[OWN] if user_id is None else user_id,
            checkin_id=self.row['id'] if checkin_id is None else checkin_id,
            claim_token=self.token if token is None else token)

    def publish(self, predicate=None, *, now=2000):
        return cache.put(self.path, OWN, 'publication-test', self.request_key,
                         {'synthetic': 'result'}, now=now,
                         publish_if=self.fence() if predicate is None else predicate)

    def cache_rows(self):
        with closing(sqlite3.connect(self.path)) as conn:
            return conn.execute('SELECT * FROM progressor_ai_cache ORDER BY user_id,scope,key').fetchall()

    async def test_exact_processing_claim_publishes_without_new_lease_policy(self):
        # The existing finish CAS permits an expired, unreclaimed processing claim.
        self.assertTrue(self.publish(now=2000))
        self.assertEqual(cache.get(self.path, OWN, 'publication-test', self.request_key, now=2001),
                         {'synthetic': 'result'})
        self.assertTrue(await db.finish_daily_checkin(self.conn, OWN, self.row['id'], self.token, []))

    async def test_predicate_only_reads_its_supplied_connection(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute('PRAGMA query_only=ON')
            before = (conn.total_changes, conn.in_transaction)
            self.assertTrue(self.fence()(conn))
            self.assertEqual((conn.total_changes, conn.in_transaction), before)

    async def test_wrong_owners_checkin_and_tokens_leave_all_cache_metadata_unchanged(self):
        self.assertTrue(cache.put(self.path, OTHER, 'sentinel', cache.key({'other': 1}), [], now=1000, ttl=60))
        before = self.cache_rows()
        invalid = (
            self.fence(tg_uid=OTHER), self.fence(user_id=self.ids[OTHER]),
            self.fence(checkin_id=self.row['id'] + 100), self.fence(token=''),
            self.fence(token='different-claim'),
        )
        for index, predicate in enumerate(invalid):
            with self.subTest(predicate=index):
                self.assertFalse(self.publish(predicate))
                self.assertEqual(self.cache_rows(), before)

    async def test_done_and_failed_claims_cannot_publish(self):
        for status in ('done', 'failed'):
            await self.conn.execute('UPDATE daily_checkin_claims SET status=? WHERE checkin_id=?',
                                    (status, self.row['id']))
            await self.conn.commit()
            with self.subTest(status=status):
                self.assertFalse(self.publish())

    async def test_missing_claim_cannot_publish(self):
        await self.conn.execute('DELETE FROM daily_checkin_claims WHERE checkin_id=?', (self.row['id'],))
        await self.conn.commit()
        self.assertFalse(self.publish())

    async def test_missing_checkin_cannot_publish(self):
        await self.conn.execute('DELETE FROM daily_checkins WHERE id=?', (self.row['id'],))
        await self.conn.commit()
        self.assertFalse(self.publish())

    async def test_missing_owner_cannot_publish(self):
        # A supplied read connection cannot recreate an absent identity.
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.executescript('''CREATE TABLE users(id INTEGER PRIMARY KEY,tg_user_id INTEGER);
                CREATE TABLE daily_checkins(id INTEGER PRIMARY KEY,user_id INTEGER);
                CREATE TABLE daily_checkin_claims(checkin_id INTEGER,token TEXT,status TEXT);
                INSERT INTO daily_checkins VALUES(1,10);
                INSERT INTO daily_checkin_claims VALUES(1,'claim','processing');''')
            self.assertFalse(db.daily_checkin_cache_publishable(
                conn, tg_user_id=OWN, user_id=10, checkin_id=1, claim_token='claim'))

    async def test_reclaimed_same_id_requires_the_new_storage_token(self):
        renewed, new_token = await db.reserve_daily_checkin(
            self.conn, OWN, self.day, self.answers, now=1900, token='reclaimed-claim')
        self.assertEqual(renewed['id'], self.row['id'])
        self.assertNotEqual(new_token, self.token)
        self.assertFalse(self.publish())
        self.assertFalse(await db.finish_daily_checkin(self.conn, OWN, self.row['id'], self.token, ['old']))
        self.assertTrue(self.publish(self.fence(token=new_token)))
        self.assertTrue(await db.finish_daily_checkin(self.conn, OWN, self.row['id'], new_token, []))

    async def test_purge_first_prevents_old_publication_and_new_action_still_publishes(self):
        self.assertTrue((await db.purge_user_data(self.conn, OWN))['deleted'])
        self.assertFalse(self.publish())
        renewed, new_token = await db.reserve_daily_checkin(
            self.conn, OWN, self.day, self.answers, token='new-post-purge-claim')
        self.assertTrue(self.publish(self.fence(checkin_id=renewed['id'], token=new_token)))

    async def test_synthetic_recreated_numeric_id_still_requires_new_claim(self):
        old_id = self.row['id']
        self.assertTrue((await db.purge_user_data(self.conn, OWN))['deleted'])
        # This deliberately recreates an id in a fixture; AUTOINCREMENT does not
        # ordinarily reuse the id after purge.
        await self.conn.execute('''INSERT INTO daily_checkins
            (id,user_id,date_local,tz_name,answers_json,created_at_utc) VALUES(?,?,?,?,?,?)''',
            (old_id, self.ids[OWN], self.day, 'UTC', json.dumps(self.answers), '2026-01-01T00:00:00Z'))
        await self.conn.execute('''INSERT INTO daily_checkin_claims(checkin_id,token,status,lease_until)
            VALUES(?,?,'processing',?)''', (old_id, 'synthetic-new-claim', 9999999999))
        await self.conn.commit()
        self.assertFalse(self.publish())
        self.assertTrue(self.publish(self.fence(token='synthetic-new-claim')))

    async def test_publication_writer_first_then_real_purge_removes_its_cache(self):
        entered, release = threading.Event(), threading.Event()
        predicate = self.fence()

        def hold_writer(conn):
            allowed = predicate(conn)
            entered.set()
            if not release.wait(5):
                raise RuntimeError('fixture_writer_not_released')
            return allowed

        other_conn = await db.connect(self.path)
        publication = asyncio.create_task(asyncio.to_thread(self.publish, hold_writer))
        purge = None
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            purge = asyncio.create_task(db.purge_user_data(other_conn, OWN))
            release.set()
            self.assertTrue(await asyncio.wait_for(publication, 5))
            self.assertTrue((await asyncio.wait_for(purge, 5))['deleted'])
            self.assertFalse(any(row[0] == str(OWN) for row in self.cache_rows()))
        finally:
            release.set()
            for operation in (publication, purge):
                if operation is not None and not operation.done():
                    await asyncio.wait_for(operation, 6)
            await other_conn.close()


class ProviderGate:
    """Only the dummy provider waits; database reservation/cache/CAS stay real."""
    def __init__(self, raw):
        self.raw = raw
        self.loop = asyncio.get_running_loop()
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.calls = 0

    async def native(self, *_args, **_kwargs):
        self.calls += 1
        self.entered.set()
        await asyncio.wait_for(self.release.wait(), 8)
        return self.raw

    def web(self, *_args, **_kwargs):
        self.calls += 1
        self.loop.call_soon_threadsafe(self.entered.set)
        waiter = asyncio.run_coroutine_threadsafe(self.release.wait(), self.loop)
        try:
            if not waiter.result(timeout=8):
                raise RuntimeError('fixture_provider_not_released')
        finally:
            waiter.cancel()
        return self.raw


class CheckinCacheCallerPublication(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'supply.sqlite3')
        await db.init_db(self.path)
        self.conn = await db.connect(self.path)
        self.ids = {}
        self.day = db.local_date('UTC')
        previous = (datetime.strptime(self.day, '%Y-%m-%d').date() - timedelta(days=1)).isoformat()
        for tg_uid in (OWN, OTHER):
            self.ids[tg_uid] = await db.get_or_create_user(self.conn, tg_uid)
            self.assertNotIn(self.ids[tg_uid], (OWN, OTHER))
            await db.set_user_tz(self.conn, tg_uid, 'UTC')
            await db.set_premium_until(self.conn, tg_uid, datetime(2070, 1, 1, tzinfo=timezone.utc),
                                       source='fixture-only', plan_id='fixture-only')
            await self.conn.execute('''UPDATE users SET starter_trial_used=1,
                starter_trial_started_at_utc='1999-12-01T00:00:00Z',
                starter_trial_until_utc='2000-01-01T00:00:00Z',starter_trial_source='fixture-only'
                WHERE tg_user_id=?''', (tg_uid,))
            await self.conn.commit()
            self.assertTrue(await db.log_payment_paid(
                self.conn, tg_user_id=tg_uid, provider='fixture', currency='RUB', amount=1,
                payload='fixture-' + str(tg_uid), provider_payment_charge_id='fixture-' + str(tg_uid),
                project_key='fixture-only', payment_kind='subscription', plan_id='fixture-only',
                finalized_at_utc='2026-01-01T00:00:00Z'))
            row, token = await db.reserve_daily_checkin(self.conn, tg_uid, previous, {'prior': tg_uid})
            self.assertTrue(await db.finish_daily_checkin(self.conn, tg_uid, row['id'], token, []))
            self.assertTrue(cache.put(self.path, tg_uid, 'prior-control', cache.key({'prior': tg_uid}), []))
        self.before = self.snapshot()

    async def asyncTearDown(self):
        await self.conn.close()
        self.tmp.cleanup()

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as conn:
            return {
                'financial': conn.execute('SELECT id,tg_user_id,' + ','.join(FINANCIAL_FIELDS) + ' FROM users ORDER BY id').fetchall(),
                'payments': conn.execute('SELECT * FROM payments ORDER BY id').fetchall(),
                'payment_owners': conn.execute('''SELECT p.id,p.user_id,u.id,u.tg_user_id,p.status
                    FROM payments p LEFT JOIN users u ON u.id=p.user_id ORDER BY p.id''').fetchall(),
                'cache': conn.execute('SELECT * FROM progressor_ai_cache ORDER BY user_id,scope,key').fetchall(),
                'checkins': conn.execute('SELECT * FROM daily_checkins ORDER BY id').fetchall(),
                'claims': conn.execute('SELECT * FROM daily_checkin_claims ORDER BY checkin_id').fetchall(),
                'quick_check': conn.execute('PRAGMA quick_check').fetchall(),
            }

    def assert_financial_retained(self):
        actual = self.snapshot()
        for key in ('financial', 'payments', 'payment_owners'):
            self.assertEqual(actual[key], self.before[key])
        self.assertEqual(actual['quick_check'], [('ok',)])
        for row in actual['payment_owners']:
            self.assertEqual(row[1], self.ids[row[3]])
            self.assertEqual(row[1], row[2])
            self.assertEqual(row[4], 'paid')

    async def run_surface(self, surface, scenario):
        import bot
        import webapp_backend as api
        settings = NS(DB_PATH=self.path, OPENAI_API_KEY='offline-fixture',
                      OPENAI_MODEL='offline-fixture', WEBAPP_URL='')
        gate = ProviderGate(json.dumps({'actions': [], 'summary_ru': 'Fixture', 'disclaimer_ru': ''})
                            if surface == 'native' else '[]')
        context = NS(user_data={}, application=NS(bot_data={'settings': settings}))
        update = NS(effective_user=NS(id=OWN))
        render, show = AsyncMock(), AsyncMock()
        pending = None
        try:
            if scenario == 'new-after-purge':
                self.assertTrue((await db.purge_user_data(self.conn, OWN))['deleted'])
                self.assert_financial_retained()
            with ExitStack() as patches:
                patches.enter_context(patch('progressor_client.ai_context', AsyncMock(return_value=None)))
                patches.enter_context(patch('progressor_checkin.changes', AsyncMock(return_value=[])))
                patches.enter_context(patch.object(bot, 'show', show))
                patches.enter_context(patch.object(bot, '_render_ai_checkin_results', render))
                patches.enter_context(patch.object(bot, 'ai_checkin_json', gate.native))
                patches.enter_context(patch.object(api, 's', settings))
                patches.enter_context(patch.object(api, '_auth_user_id', AsyncMock(return_value=OWN)))
                patches.enter_context(patch.object(api, '_sync_call', gate.web))
                # Native guard, Web premium, reserve, finish, cache and purge remain real.
                if surface == 'native':
                    operation = bot._finalize_ai_checkin(
                        update, context, self.conn, tg_uid=OWN, user_id=self.ids[OWN], tz_name='UTC',
                        state={'target_day': 'today', 'answers': {'sleep_range': '7-8', 'energy': 3,
                               'stress': 2, 'training': False, 'goals': [], 'note': 'Fixture only'}})
                else:
                    operation = api.api_checkin(api.CheckinBody(answers={'energy': 'ok', 'sleep': 'ok',
                        'stress': 'medium', 'training': 'none', 'goals': [], 'note': 'Fixture only'}, target='today'), '')
                pending = asyncio.create_task(operation)
                await asyncio.wait_for(gate.entered.wait(), 6)
                row = await db.get_today_checkin(self.conn, OWN, self.day)
                self.assertIsNotNone(row)
                claim = await db.fetchone(self.conn, 'SELECT * FROM daily_checkin_claims WHERE checkin_id=?', (row['id'],))
                self.assertEqual(claim['status'], 'processing')
                self.assertTrue(claim['token'])
                self.assert_financial_retained()
                if scenario in ('own-purge', 'other-purge'):
                    purge_uid = OWN if scenario == 'own-purge' else OTHER
                    purge_conn = await db.connect(self.path)
                    try:
                        self.assertTrue((await db.purge_user_data(purge_conn, purge_uid))['deleted'])
                    finally:
                        await purge_conn.close()
                    self.assertIsNone(await db.get_today_checkin(self.conn, purge_uid, self.day))
                    self.assertFalse(any(r[0] == str(purge_uid) for r in self.snapshot()['cache']))
                    self.assert_financial_retained()
                gate.release.set()
                response = await asyncio.wait_for(pending, 8)
                self.assertEqual(gate.calls, 1)
                own_purge = scenario == 'own-purge'
                scope = 'supply_checkin' if surface == 'native' else 'supply_web_checkin'
                result_rows = [r for r in self.snapshot()['cache'] if r[0] == str(OWN) and r[1] == scope]
                self.assertEqual(len(result_rows), 0 if own_purge else 1)
                if surface == 'native':
                    self.assertEqual(render.await_count, 0 if own_purge else 1)
                else:
                    self.assertEqual(response.status_code, 409 if own_purge else 200)
                    if own_purge:
                        self.assertEqual(json.loads(response.body)['code'], 'CHECKIN_CHANGED')
                if own_purge:
                    self.assertIsNone(await db.get_today_checkin(self.conn, OWN, self.day))
                else:
                    saved = await db.get_today_checkin(self.conn, OWN, self.day)
                    self.assertEqual(json.loads(saved['ai_raw_json']), [])
                    claim = await db.fetchone(self.conn, 'SELECT status FROM daily_checkin_claims WHERE checkin_id=?', (saved['id'],))
                    self.assertEqual(claim['status'], 'done')
                    self.assertEqual(cache.get(self.path, OWN, scope, result_rows[0][2]), gate.raw)
                self.assert_financial_retained()
                # Preserve the untouched owner's prior records, including tokens.
                untouched = OWN if scenario == 'other-purge' else OTHER
                after = self.snapshot()
                old_ids = {r[0] for r in self.before['checkins'] if r[1] == self.ids[untouched]}
                for key, owner_index, owner_value in (('checkins', 1, self.ids[untouched]), ('cache', 0, str(untouched))):
                    for prior in self.before[key]:
                        if prior[owner_index] == owner_value:
                            self.assertIn(prior, after[key])
                for prior in self.before['claims']:
                    if prior[0] in old_ids:
                        self.assertIn(prior, after['claims'])
        finally:
            gate.release.set()
            if pending is not None and not pending.done():
                try:
                    await asyncio.wait_for(pending, 9)
                except (asyncio.CancelledError, Exception):
                    pass

    async def test_native_normal(self):
        await self.run_surface('native', 'normal')

    async def test_native_own_purge_during_provider(self):
        await self.run_surface('native', 'own-purge')

    async def test_native_other_owner_purge(self):
        await self.run_surface('native', 'other-purge')

    async def test_native_intentional_new_action_after_purge(self):
        await self.run_surface('native', 'new-after-purge')

    async def test_web_normal(self):
        await self.run_surface('web', 'normal')

    async def test_web_own_purge_during_provider(self):
        await self.run_surface('web', 'own-purge')

    async def test_web_other_owner_purge(self):
        await self.run_surface('web', 'other-purge')

    async def test_web_intentional_new_action_after_purge(self):
        await self.run_surface('web', 'new-after-purge')
