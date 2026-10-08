import os,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,AsyncMock,Mock
import db


class NativeControls(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=os.environ.get('PROGRESSOR_TEST_TMP_DIR'));self.path=str(Path(self.tmp.name)/'supply.db')
        await db.init_db(self.path);self.conn=await db.connect(self.path)
    async def asyncTearDown(self):await self.conn.close();self.tmp.cleanup()
    async def test_active_job_dedup_does_not_spend_second_attempt(self):
        a=await db.create_assistant_job(self.conn,123,'{}','a',kind='lab_explain')
        b=await db.create_assistant_job(self.conn,123,'{}','b',kind='lab_explain')
        self.assertEqual(a,b)
        row=await db.fetchone(self.conn,'SELECT count(*) AS n FROM progressor_ai_attempts')
        self.assertEqual(row['n'],1)
    async def test_failed_or_purged_job_does_not_reset_attempt_counter(self):
        await db.create_assistant_job(self.conn,123,'{}','a',kind='lab_explain')
        await self.conn.execute('DELETE FROM assistant_job_events');await self.conn.execute('DELETE FROM assistant_jobs');await self.conn.commit()
        with self.assertRaises(db.AssistantQuotaExceeded) as exc:await db.create_assistant_job(self.conn,123,'{}','b',kind='lab_explain')
        self.assertEqual(str(exc.exception),'ASSISTANT_ATTEMPT_LIMIT')
    async def test_other_account_is_not_blocked(self):
        a=await db.create_assistant_job(self.conn,123,'{}','a',kind='lab_explain')
        b=await db.create_assistant_job(self.conn,456,'{}','b',kind='lab_explain')
        self.assertNotEqual(a,b)

    async def test_registered_voice_and_checkin_entrypoints_stop_before_provider(self):
        import bot
        from progressor_ai_guard import acquire,finish
        token=acquire(self.path,123,'supply_guidance')
        update=SimpleNamespace(effective_user=SimpleNamespace(id=123))
        context=SimpleNamespace(application=SimpleNamespace(bot_data={'settings':SimpleNamespace(DB_PATH=self.path)}),user_data={})
        with patch.object(bot,'show',AsyncMock()) as show,patch.object(bot,'guard_not_banned',AsyncMock()) as guard,patch.object(bot,'download_telegram_voice',AsyncMock()) as provider,patch.object(db,'ensure_daily_rollover',AsyncMock()) as rollover:
            await bot.on_voice(update,context)
            await bot._finalize_ai_checkin(update,context,self.conn,tg_uid=123,user_id=55,tz_name='UTC',state={})
        self.assertEqual(show.await_count,2);guard.assert_not_awaited();provider.assert_not_awaited();rollover.assert_not_awaited()
        finish(self.path,token)

    async def test_sdk_does_not_add_hidden_retries(self):
        from openai_client import _build_openai_client,OpenAITranscribeConfig
        with patch('openai.OpenAI',Mock()) as client:
            _build_openai_client(OpenAITranscribeConfig('offline','offline'))
        self.assertEqual(client.call_args.kwargs['max_retries'],0)
        self.assertEqual(client.call_args.kwargs['timeout'],60.0)
