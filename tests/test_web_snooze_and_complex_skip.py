"""Mini App snooze reaches the single bot consumer once; a complex can be skipped."""
import os,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
import db,bot

USER=123

class WebSnoozeAndComplexSkip(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=os.getenv('PROGRESSOR_TEST_TMP_DIR'));self.path=str(Path(self.tmp.name)/'supply.db')
        await db.init_db(self.path);self.conn=await db.connect(self.path)
        self.uid=await db.get_or_create_user(self.conn,USER)
        for sid in (1,2,3):
            await self.conn.execute('INSERT INTO supplements(id,slug,name,name_ru,name_en) VALUES(?,?,?,?,?)',(sid,'t'+str(sid),'N'+str(sid),'N'+str(sid),'N'+str(sid)))
        await self.conn.commit()
        self.day=db.get_user_local_date(await db.get_user_tz(self.conn,USER) or None)
        self.ctx=NS(application=NS(bot_data={'settings':NS(DB_PATH=self.path)}))
    async def asyncTearDown(self):await self.conn.close();self.tmp.cleanup()

    async def test_claim_is_once_only_and_not_before_its_time(self):
        now=time.time()
        await db.add_web_snooze(self.conn,USER,self.day,'day',[2,1,2],now+3600)
        self.assertEqual(await db.claim_due_web_snoozes(self.conn,now),[])
        claimed=await db.claim_due_web_snoozes(self.conn,now+3601)
        self.assertEqual(claimed,[{'tg_user_id':USER,'day':self.day,'slot_id':'day','supplement_ids':[1,2]}])
        self.assertEqual(await db.claim_due_web_snoozes(self.conn,now+7200),[])

    async def test_repeated_request_for_a_slot_replaces_the_pending_one(self):
        now=time.time()
        await db.add_web_snooze(self.conn,USER,self.day,'day',[1],now+10)
        await db.add_web_snooze(self.conn,USER,self.day,'day',[1,3],now+3600)
        self.assertEqual(await db.claim_due_web_snoozes(self.conn,now+60),[])
        self.assertEqual((await db.claim_due_web_snoozes(self.conn,now+3601))[0]['supplement_ids'],[1,3])

    async def test_tick_reminds_only_about_still_unmarked_intakes_of_the_same_day(self):
        now=time.time()
        await db.add_web_snooze(self.conn,USER,self.day,'day',[1,2],now-1)
        await db.add_web_snooze(self.conn,USER,'1999-01-01','old',[3],now-1)
        await db.set_day_status(self.conn,USER,self.day,1,'taken');await self.conn.commit()
        with patch.object(bot,'reminder_fire_batch',AsyncMock()) as fire,patch('progressor_links.notifications_allowed',return_value=True):
            await bot.web_snoozes_tick(self.ctx)
            await bot.web_snoozes_tick(self.ctx)
        fire.assert_awaited_once()
        self.assertEqual(fire.await_args.args[1:],(USER,[2]));self.assertTrue(fire.await_args.kwargs['is_snooze'])

    async def test_tick_respects_disabled_notifications_and_does_not_replay(self):
        await db.add_web_snooze(self.conn,USER,self.day,'day',[1],time.time()-1)
        with patch.object(bot,'reminder_fire_batch',AsyncMock()) as fire:
            with patch('progressor_links.notifications_allowed',return_value=False):await bot.web_snoozes_tick(self.ctx)
            with patch('progressor_links.notifications_allowed',return_value=True):await bot.web_snoozes_tick(self.ctx)
        fire.assert_not_awaited()

    async def test_complex_can_be_skipped_and_reset(self):
        await self.conn.execute("INSERT INTO user_complexes(id,user_id,name,created_at_utc) VALUES(7,?,?,'2026-01-01T00:00:00')",(self.uid,'Мой'))
        for sid in (1,2):await self.conn.execute('INSERT INTO user_complex_items(complex_id,supplement_id) VALUES(7,?)',(sid,))
        await self.conn.commit()
        await db.toggle_complex_for_day(self.conn,USER,self.day,7,'skipped')
        self.assertEqual({await db.get_day_status(self.conn,USER,self.day,s) for s in (1,2)},{'skipped'})
        await db.toggle_complex_for_day(self.conn,USER,self.day,7,'none')
        for s in (1,2):self.assertIn(await db.get_day_status(self.conn,USER,self.day,s),(None,'none'))
        with self.assertRaises(ValueError):await db.toggle_complex_for_day(self.conn,USER,self.day,7,'partial')

if __name__=='__main__':unittest.main()
