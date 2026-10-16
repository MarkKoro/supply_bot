"""Expired/stale native actions must pass current server permissions."""
import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
import db


class QualityAccess(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'supply.sqlite3')
        await db.init_db(self.path)
        self.conn = await db.connect(self.path)
        await db.bootstrap_roles_from_settings(self.conn, NS(OWNER_TG_ID=1, ADMIN_TG_IDS={2}))
        for uid in (123, 456):
            await db.get_or_create_user(self.conn, uid)
            await db.set_user_tz(self.conn, uid, 'Asia/Bangkok')
        await db.save_user_consent(self.conn, 123, offer_version='test', privacy_version='test', source='offline')
        self.settings = NS(DB_PATH=self.path, ADMIN_IDS={1}, OFFER_VERSION='test', PRIVACY_VERSION='test')

    async def asyncTearDown(self):
        await self.conn.close()
        self.tmp.cleanup()

    def native(self, uid=123, data='cx:create', state=None):
        user = NS(id=uid)
        message = NS(message_id=77, chat_id=uid, text='New complex', delete=AsyncMock(), reply_text=AsyncMock())
        query = NS(data=data, from_user=user, message=message, answer=AsyncMock())
        update = NS(effective_user=user, effective_chat=NS(id=uid, type='private'),
                    message=message, effective_message=message, callback_query=query)
        context = NS(user_data=state or {}, args=['456', '30'],
                     application=NS(bot_data={'settings': self.settings}))
        return update, context

    async def test_concurrent_trial_activation_is_once_and_expired_trial_never_restarts(self):
        connections = [await db.connect(self.path) for _ in range(5)]
        try:
            dates = await asyncio.gather(*[db.start_starter_trial(c, 123) for c in connections])
        finally:
            for c in connections: await c.close()
        self.assertEqual(len(set(dates)), 1)
        self.assertTrue(dates[0])
        await self.conn.execute("UPDATE users SET starter_trial_until_utc='2000-01-01T00:00:00Z' WHERE tg_user_id=123")
        await self.conn.commit()
        self.assertIsNone(await db.start_starter_trial(self.conn, 123))
        row = await db.fetchone(self.conn, 'SELECT starter_trial_used,starter_trial_until_utc FROM users WHERE tg_user_id=123')
        self.assertEqual(tuple(row), (1, '2000-01-01T00:00:00Z'))

    async def test_bootstrap_and_revoked_owner_cannot_reuse_database_role_actions(self):
        self.assertEqual(await db.get_role(self.conn, 1), db.ROLE_OWNER)
        self.assertEqual(await db.get_role(self.conn, 2), db.ROLE_ADMIN)
        await db.set_role(self.conn, 1, 2, db.ROLE_OWNER)
        await db.remove_role(self.conn, 1, 2)
        before = (await db.fetchone(self.conn, 'SELECT count(*) AS n FROM admin_audit'))['n']
        with self.assertRaises(PermissionError): await db.set_role(self.conn, 2, 456, db.ROLE_OWNER)
        with self.assertRaises(PermissionError): await db.remove_role(self.conn, 2, 1)
        self.assertEqual(await db.get_role(self.conn, 456), db.ROLE_NONE)
        self.assertEqual((await db.fetchone(self.conn, 'SELECT count(*) AS n FROM admin_audit'))['n'], before)

    async def test_stale_native_owner_button_clears_target_without_granting(self):
        import bot
        update, context = self.native(data='admin:roles:set:owner', state={'owner_target_tg_id': 456})
        with patch.object(bot, 'guard_not_banned', AsyncMock(return_value=True)), patch.object(bot, 'show', AsyncMock()) as show:
            await bot.on_menu(update, context)
        self.assertEqual(await db.get_role(self.conn, 456), db.ROLE_NONE)
        self.assertNotIn('owner_target_tg_id', context.user_data)
        self.assertIn('Нет доступа', show.call_args.args[2])

    async def test_editor_and_support_cannot_grant_or_remove_premium_through_native_commands(self):
        import bot
        for role in (db.ROLE_EDITOR, db.ROLE_SUPPORT):
            await db.set_role(self.conn, 1, 123, role)
            update, context = self.native()
            with patch.object(bot, 'guard_not_banned', AsyncMock(return_value=True)), patch.object(bot, '_is_admin', return_value=False):
                await bot.grant_command(update, context)
                await bot.degrant_command(update, context)
            self.assertIsNone(await db.get_premium_until(self.conn, 456))
            self.assertEqual(update.message.reply_text.await_count, 2)

    async def test_manual_access_rechecks_revocation_at_write_and_preserves_other_records(self):
        before = await db.get_premium_until(self.conn, 456)
        await db.remove_role(self.conn, 1, 2)
        with self.assertRaises(PermissionError):
            await db.adjust_manual_premium(self.conn, 2, 456, 30)
        self.assertEqual(await db.get_premium_until(self.conn, 456), before)
        until = await db.adjust_manual_premium(self.conn, 1, 456, 30)
        self.assertTrue(db._parse_utc_iso_dt(until) > datetime.now(timezone.utc) + timedelta(days=29))
        reduced = await db.adjust_manual_premium(self.conn, 1, 456, -10)
        self.assertAlmostEqual((db._parse_utc_iso_dt(until)-db._parse_utc_iso_dt(reduced)).total_seconds(), 864000, delta=2)

    async def test_expired_complex_callback_and_pending_text_do_not_change_owned_records(self):
        import bot
        old = await db.create_complex(self.conn, 123, 'Saved complex')
        update, context = self.native(data=f"cx:rename:{old['id']}")
        with patch.object(bot, 'guard_not_banned', AsyncMock(return_value=True)), patch.object(bot, 'show', AsyncMock()) as show:
            await bot.on_menu(update, context)
            self.assertIn('активным доступом', show.call_args.args[2])
            context.user_data[bot.WAITING_KEY] = 'cx:create'
            await db.save_ui_state(self.conn, 123, 77, 'cx:create', '{}')
            with patch.object(bot, '_handle_lab_text', AsyncMock(return_value=False)):
                await bot.on_text(update, context)
        rows = await db.fetchall(self.conn, 'SELECT name FROM user_complexes')
        self.assertEqual([r['name'] for r in rows], ['Saved complex'])
        self.assertNotIn(bot.WAITING_KEY, context.user_data)

    async def test_active_starter_keeps_complex_creation_available(self):
        import bot
        await db.start_starter_trial(self.conn, 123)
        update, context = self.native()
        with patch.object(bot, 'guard_not_banned', AsyncMock(return_value=True)), patch.object(bot, 'show', AsyncMock()):
            await bot.on_menu(update, context)
        self.assertEqual(context.user_data.get(bot.WAITING_KEY), 'cx:create')

    async def test_explicit_utc_is_configured_but_a_new_default_still_needs_onboarding(self):
        import bot
        await db.get_or_create_user(self.conn, 789)
        self.assertFalse(await db.is_user_timezone_configured(self.conn, 789))
        update, context = self.native(uid=789)
        with patch.object(bot, '_show_timezone_picker', AsyncMock()) as picker:
            self.assertFalse(await bot.ensure_timezone_or_redirect(update, context))
            picker.assert_awaited_once()
            picker.reset_mock()
            await db.set_user_tz(self.conn, 789, 'UTC')
            self.assertTrue(await db.is_user_timezone_configured(self.conn, 789))
            self.assertTrue(await bot.ensure_timezone_or_redirect(update, context))
            picker.assert_not_awaited()
            await db.set_user_tz(self.conn, 789, 'GMT')
            self.assertTrue(await bot.ensure_timezone_or_redirect(update, context))
