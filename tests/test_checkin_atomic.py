"""Concurrent native/Web check-ins against an isolated real database, no providers."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch
import db


class AtomicCheckin(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'supply.sqlite3')
        await db.init_db(self.path)
        self.conn = await db.connect(self.path)
        await db.get_or_create_user(self.conn, 123)
        await db.set_user_tz(self.conn, 123, 'UTC')
        self.answers = {'energy': 'ok', 'sleep': 'ok', 'stress': 'medium',
                        'training': 'none', 'goals': [], 'note': ''}
        self.day = db.local_now('UTC').date().isoformat()

    async def asyncTearDown(self):
        await self.conn.close()
        self.tmp.cleanup()

    async def test_six_concurrent_surfaces_keep_one_owner_and_original_answers(self):
        connections = [await db.connect(self.path) for _ in range(6)]
        try:
            results = await asyncio.gather(*[
                db.reserve_daily_checkin(c, 123, self.day, {**self.answers, 'note': str(i)})
                for i, c in enumerate(connections)])
        finally:
            for c in connections: await c.close()
        winners = [(row, token) for row, token in results if token]
        self.assertEqual(len(winners), 1)
        row, token = winners[0]
        self.assertEqual(len({r['id'] for r, _ in results}), 1)
        self.assertTrue(all(r['answers_json'] == row['answers_json'] for r, _ in results))
        self.assertFalse(await db.finish_daily_checkin(self.conn, 456, row['id'], token, ['foreign']))
        self.assertTrue(await db.finish_daily_checkin(self.conn, 123, row['id'], token, []))
        self.assertFalse(await db.finish_daily_checkin(self.conn, 123, row['id'], token, ['late']))
        saved = await db.get_today_checkin(self.conn, 123, self.day)
        self.assertEqual(json.loads(saved['ai_raw_json']), [])

    async def test_failed_attempt_can_retry_same_answers_without_rewriting_observation(self):
        row, token = await db.reserve_daily_checkin(self.conn, 123, self.day, self.answers, now=1000)
        await db.finish_daily_checkin(self.conn, 123, row['id'], token, {'error': 'AI_UNAVAILABLE'}, failed=True)
        unchanged, denied = await db.reserve_daily_checkin(self.conn, 123, self.day, {**self.answers, 'note': 'replace'}, now=1001)
        self.assertIsNone(denied)
        retry, new_token = await db.reserve_daily_checkin(self.conn, 123, self.day, self.answers, now=1002)
        self.assertTrue(new_token)
        self.assertNotEqual(new_token, token)
        self.assertEqual(retry['created_at_utc'], row['created_at_utc'])
        self.assertFalse(await db.finish_daily_checkin(self.conn, 123, row['id'], token, ['late']))
        self.assertTrue(await db.finish_daily_checkin(self.conn, 123, row['id'], new_token, []))
        _, denied = await db.reserve_daily_checkin(self.conn, 123, self.day, self.answers, now=10000)
        self.assertIsNone(denied)

    async def test_expired_worker_cannot_replace_reclaimed_or_applied_result(self):
        row, old = await db.reserve_daily_checkin(self.conn, 123, self.day, self.answers, now=1000)
        _, blocked = await db.reserve_daily_checkin(self.conn, 123, self.day, self.answers, now=1899)
        self.assertIsNone(blocked)
        _, fresh = await db.reserve_daily_checkin(self.conn, 123, self.day, self.answers, now=1900)
        self.assertTrue(fresh)
        self.assertFalse(await db.finish_daily_checkin(self.conn, 123, row['id'], old, ['late']))
        self.assertTrue(await db.finish_daily_checkin(self.conn, 123, row['id'], fresh, []))
        await db.set_daily_checkin_applied(self.conn, row['id'], 123, {'applied': True})
        _, blocked = await db.reserve_daily_checkin(self.conn, 123, self.day, self.answers, now=10000)
        self.assertIsNone(blocked)
        self.assertEqual(json.loads((await db.get_today_checkin(self.conn, 123, self.day))['applied_json']), {'applied': True})

    async def test_historical_error_recovers_but_valid_empty_result_is_terminal(self):
        row, token = await db.reserve_daily_checkin(self.conn, 123, self.day, self.answers, now=1000)
        await db.finish_daily_checkin(self.conn, 123, row['id'], token, {'error': 'AI_UNAVAILABLE'}, failed=True)
        await self.conn.execute('DELETE FROM daily_checkin_claims')
        await self.conn.commit()
        _, recovered = await db.reserve_daily_checkin(self.conn, 123, self.day, self.answers, now=2000)
        self.assertTrue(recovered)
        await db.finish_daily_checkin(self.conn, 123, row['id'], recovered, [])
        await self.conn.execute('DELETE FROM daily_checkin_claims')
        await self.conn.commit()
        _, denied = await db.reserve_daily_checkin(self.conn, 123, self.day, self.answers, now=10000)
        self.assertIsNone(denied)

    def api_settings(self):
        return NS(DB_PATH=self.path, OPENAI_API_KEY='offline', OPENAI_MODEL='offline')

    async def test_two_web_requests_make_one_provider_call_and_keep_first_result(self):
        import webapp_backend as api
        provider = Mock(return_value='[]')
        with patch.object(api, 's', self.api_settings()), \
             patch.object(api, '_auth_user_id', AsyncMock(return_value=123)), \
             patch.object(api, '_premium_guard', AsyncMock(return_value=True)), \
             patch.object(api, '_sync_call', provider), \
             patch('progressor_client.ai_context', AsyncMock(return_value=None)), \
             patch('progressor_checkin.changes', AsyncMock(return_value=[])):
            responses = await asyncio.gather(*[api.api_checkin(api.CheckinBody(answers=self.answers, target='today'), '') for _ in range(2)])
        self.assertEqual(sorted(r.status_code for r in responses), [200, 409])
        provider.assert_called_once()
        rows = await db.fetchall(self.conn, 'SELECT * FROM daily_checkins')
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]['ai_raw_json']), [])

    async def test_web_obeys_existing_native_attempt_lease_and_releases_its_claim(self):
        import webapp_backend as api
        from progressor_ai_guard import acquire, finish
        token = acquire(self.path, 123, 'supply_guidance')
        provider = Mock(return_value='[]')
        try:
            with patch.object(api, 's', self.api_settings()), \
                 patch.object(api, '_auth_user_id', AsyncMock(return_value=123)), \
                 patch.object(api, '_premium_guard', AsyncMock(return_value=True)), \
                 patch.object(api, '_sync_call', provider), \
                 patch('progressor_client.ai_context', AsyncMock(return_value=None)):
                response = await api.api_checkin(api.CheckinBody(answers=self.answers, target='today'), '')
            self.assertEqual(response.status_code, 429)
            self.assertIn('Retry-After'.lower(), response.headers)
            provider.assert_not_called()
            row = await db.fetchone(self.conn, 'SELECT status FROM daily_checkin_claims')
            self.assertEqual(row['status'], 'failed')
        finally: finish(self.path, token)

    async def test_native_reopens_web_result_without_replacing_it_or_calling_provider(self):
        import bot
        row, token = await db.reserve_daily_checkin(self.conn, 123, self.day, self.answers)
        await db.finish_daily_checkin(self.conn, 123, row['id'], token, [])
        context = NS(user_data={}, application=NS(bot_data={'settings': NS(DB_PATH=self.path, WEBAPP_URL='')}))
        update = NS(effective_user=NS(id=123))
        uid = (await db.fetchone(self.conn, 'SELECT id FROM users WHERE tg_user_id=123'))['id']
        with patch.object(db, 'ensure_daily_rollover', AsyncMock(return_value=self.day)), \
             patch.object(bot, 'show', AsyncMock()) as show, \
             patch.object(bot, 'ai_checkin_json', AsyncMock()) as provider:
            await bot._finalize_ai_checkin(update, context, self.conn, tg_uid=123, user_id=uid,
                                          tz_name='UTC', state={'answers': {**self.answers, 'note': 'replace'}})
        provider.assert_not_awaited()
        self.assertIn('уже сохранён', show.call_args.args[2])
        saved = await db.get_today_checkin(self.conn, 123, self.day)
        self.assertEqual(saved['answers_json'], row['answers_json'])
        self.assertEqual(json.loads(saved['ai_raw_json']), [])

    async def test_status_read_distinguishes_pending_expired_failure_and_empty_success(self):
        import webapp_backend as api
        row, token = await db.reserve_daily_checkin(self.conn, 123, self.day, self.answers, prompt_hash='bot:saved')
        settings = self.api_settings()
        settings.BOT_USERNAME = 'example_bot'
        with patch.object(api, 's', settings), \
             patch.object(api, '_auth_user_id', AsyncMock(return_value=123)), \
             patch.object(api, '_premium_guard', AsyncMock(return_value=True)), \
             patch.object(api, '_sync_call', Mock()) as provider:
            async def current():
                return json.loads((await api.api_checkin_today('today', '')).body)['checkin']
            pending = await current()
            self.assertEqual(pending['processing_status'], 'processing')
            self.assertEqual(pending['answer_source'], 'bot')
            self.assertEqual(pending['retry_in_bot_url'], 'https://t.me/example_bot')
            await self.conn.execute('UPDATE daily_checkin_claims SET lease_until=0')
            await self.conn.commit()
            self.assertEqual((await current())['processing_status'], 'failed')
            self.assertTrue(await db.finish_daily_checkin(self.conn, 123, row['id'], token, []))
            result = await current()
            self.assertEqual(result['processing_status'], 'done')
            self.assertEqual(result['recommendations'], [])
            provider.assert_not_called()
        saved = await db.get_today_checkin(self.conn, 123, self.day)
        self.assertEqual(saved['created_at_utc'], row['created_at_utc'])
        self.assertEqual(saved['answers_json'], row['answers_json'])

    async def test_native_explicit_retry_uses_owned_original_answers_and_rejects_foreign_id(self):
        import bot
        native = {'sleep_range': '7-8', 'energy': 3, 'stress': 2, 'training': False, 'goals': ['sleep'], 'note': 'original'}
        row, token = await db.reserve_daily_checkin(self.conn, 123, self.day, native, prompt_hash='bot:saved')
        await db.finish_daily_checkin(self.conn, 123, row['id'], token, {'error': 'offline'}, failed=True)
        context = NS(user_data={}, application=NS(bot_data={'settings': NS(DB_PATH=self.path, WEBAPP_URL='')}))
        update = NS(effective_user=NS(id=123))
        with patch.object(bot, '_finalize_ai_checkin', AsyncMock()) as retry, \
             patch.object(bot, '_render_ai_checkin_entry', AsyncMock()) as entry:
            await bot._retry_saved_ai_checkin(update, context, row['id'])
            retry.assert_awaited_once()
            self.assertEqual(retry.call_args.kwargs['state']['answers'], native)
            retry.reset_mock()
            update.effective_user.id = 456
            await bot._retry_saved_ai_checkin(update, context, row['id'])
            retry.assert_not_awaited()
            entry.assert_awaited_once()
