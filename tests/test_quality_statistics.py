"""Adherence parity, bounded reads and concurrent intake/achievement persistence."""
import asyncio,json,tempfile,unittest
from datetime import date,timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
import db

class QualityStatistics(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=str(Path(self.tmp.name)/'owner.sqlite3')
        await db.init_db(self.path);self.conn=await db.connect(self.path)
        self.uid=await db.get_or_create_user(self.conn,123);await db.set_user_tz(self.conn,123,'UTC')
        self.day=db.local_date('UTC')
        for sid in range(1,5):
            await self.conn.execute('INSERT INTO supplements(id,slug,name,name_ru,name_en) VALUES(?,?,?,?,?)',(sid,'stats-'+str(sid),'Stats '+str(sid),'Статистика '+str(sid),'Stats '+str(sid)))
        await self.conn.commit()
        await db.add_user_supplement(self.conn,123,1)
        await db.add_user_supplement(self.conn,123,2)
        await self.conn.execute('UPDATE user_supplements SET enabled=0 WHERE supplement_id=2')
        await self.conn.commit()
        cx=await db.create_complex(self.conn,123,'Комплекс')
        await db.add_item_to_complex(self.conn,123,cx['id'],3)
        await self.conn.execute("INSERT INTO user_complex_schedules(user_id,complex_id,slot_id,time_local,is_enabled,created_at_utc) VALUES(?,?,'morning','08:00',1,?)",(self.uid,cx['id'],db.utc_now_iso()))
        await self.conn.commit()
        await db.apply_day_plan_all(self.conn,self.uid,self.day,{'actions':[{'type':'add','supplement_id':4,'slug':'stats-4','time_hint':'08:00'}]})
        await db.set_day_status(self.conn,123,self.day,1,'taken')
        yesterday=(date.fromisoformat(self.day)-timedelta(days=1)).isoformat()
        await db.set_day_status(self.conn,123,yesterday,3,'taken')
        await db.get_user_slots(self.conn,self.uid)
        await db.bootstrap_achievements(self.conn);await self.conn.commit()

    async def asyncTearDown(self):
        await self.conn.close();self.tmp.cleanup()

    async def test_batched_window_matches_visible_plan_and_owned_marks(self):
        window=await db._adherence_window(self.conn,123,self.day,30)
        expected_total=taken_total=0
        for i in range(30):
            day=(date.fromisoformat(self.day)-timedelta(days=i)).isoformat()
            expected=set(await db.get_today_supplement_ids(self.conn,123,day))
            taken=await db.get_taken_supplement_ids(self.conn,123,day)
            self.assertEqual(window['expected'][day],expected)
            self.assertEqual(window['taken'][day],taken)
            expected_total+=len(expected);taken_total+=len(expected&taken)
        result=await db.get_range_adherence(self.conn,123,self.day,30)
        self.assertEqual(result['expected_total'],expected_total);self.assertEqual(result['taken_total'],taken_total)
        other=await db._adherence_window(self.conn,124,self.day,30)
        self.assertTrue(all(not value for value in other['expected'].values()))
        self.assertTrue(all(not value for value in other['taken'].values()))

    async def test_stats_use_bounded_queries_and_unchanged_catalog_is_read_only(self):
        statements=[];await self.conn.set_trace_callback(statements.append)
        await db.build_stats_compact(self.conn,123,self.day)
        self.assertLess(len(statements),30)
        statements.clear();before=self.conn.total_changes
        await db.bootstrap_achievements(self.conn)
        self.assertEqual(self.conn.total_changes,before)
        self.assertFalse(any(sql.lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) for sql in statements))

    async def test_slow_achievement_history_does_not_lock_parallel_intake(self):
        import webapp_backend as api
        original_metrics=db.get_user_metrics;entered=asyncio.Event();release=asyncio.Event();calls=0
        async def slow_once(*args):
            nonlocal calls
            calls+=1
            if calls==1:entered.set();await release.wait()
            return await original_metrics(*args)
        first=None
        with patch.object(api,'s',NS(DB_PATH=self.path)),patch.object(api,'_auth_user_id',AsyncMock(return_value=123)), \
             patch.object(api,'_premium_guard',AsyncMock(return_value=True)),patch.object(db,'get_user_metrics',slow_once):
            first=asyncio.create_task(api.api_today_toggle(api.TodayToggleBody(supplement_id=1,status='taken'),''))
            try:
                await asyncio.wait_for(entered.wait(),2)
                second=await asyncio.wait_for(api.api_today_toggle(api.TodayToggleBody(supplement_id=3,status='taken'),''),2)
                self.assertEqual(second.status_code,200)
            finally:
                release.set()
                if first is not None:await first
        row=await db.fetchone(self.conn,'SELECT count(*) AS n FROM supplement_day_state WHERE user_id=? AND day=? AND status=?',(self.uid,self.day,'taken'))
        self.assertEqual(row['n'],2)
        duplicates=await db.fetchall(self.conn,'SELECT achievement_key,count(*) AS n FROM user_achievements WHERE user_id=? GROUP BY achievement_key HAVING count(*)>1',(self.uid,))
        self.assertEqual(duplicates,[])
