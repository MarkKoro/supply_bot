"""Jar stock is derived from the intake diary: no decrement writers, undo-safe."""
import os,tempfile,unittest
from pathlib import Path
import db,stock

USER=321

class Stock(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=os.getenv('PROGRESSOR_TEST_TMP_DIR'));self.path=str(Path(self.tmp.name)/'supply.db')
        await db.init_db(self.path);self.conn=await db.connect(self.path)
        self.uid=await db.get_or_create_user(self.conn,USER)
        for sid in (1,2):
            await self.conn.execute('INSERT INTO supplements(id,slug,name,name_ru,name_en) VALUES(?,?,?,?,?)',(sid,'t'+str(sid),'N'+str(sid),'N'+str(sid),'N'+str(sid)))
        await self.conn.execute("INSERT INTO user_supplements(user_id,supplement_id,created_at) VALUES(?,1,'2026-01-01')",(self.uid,))
        await self.conn.commit()
    async def asyncTearDown(self):await self.conn.close();self.tmp.cleanup()

    async def test_no_table_and_no_count_mean_an_empty_map(self):
        self.assertEqual(await stock.stock_map(self.conn,USER),{})

    async def test_remainder_follows_taken_days_and_an_undone_mark_returns_the_dose(self):
        self.assertTrue(await stock.set_stock(self.conn,USER,1,20,2,'2026-11-10'))
        self.assertEqual((await stock.stock_map(self.conn,USER))[1],{'units_left':20.0,'per_day':2.0,'days_left':10,'low':False})
        for day in ('2026-11-09','2026-11-10','2026-11-11','2026-11-12'):
            await db.set_day_state(self.conn,USER,day,1,'taken')
        await db.set_day_state(self.conn,USER,'2026-11-13',1,'skipped')
        self.assertEqual((await stock.stock_map(self.conn,USER))[1]['units_left'],14.0)
        await db.set_day_state(self.conn,USER,'2026-11-12',1,'none')
        self.assertEqual((await stock.stock_map(self.conn,USER))[1]['units_left'],16.0)

    async def test_a_dose_already_taken_today_is_not_subtracted_from_a_fresh_count(self):
        await db.set_day_state(self.conn,USER,'2026-11-10',1,'taken')
        await stock.set_stock(self.conn,USER,1,10,1,'2026-11-10')
        self.assertEqual((await stock.stock_map(self.conn,USER))[1]['units_left'],10.0)
        await db.set_day_state(self.conn,USER,'2026-11-11',1,'taken')
        self.assertEqual((await stock.stock_map(self.conn,USER))[1]['units_left'],9.0)

    async def test_low_flag_never_negative_and_forgetting(self):
        await stock.set_stock(self.conn,USER,1,3,1,'2026-11-10')
        for day in ('2026-11-10','2026-11-11','2026-11-12','2026-11-13','2026-11-14'):
            await db.set_day_state(self.conn,USER,day,1,'taken')
        self.assertEqual((await stock.stock_map(self.conn,USER))[1],{'units_left':0.0,'per_day':1.0,'days_left':0,'low':True})
        await stock.set_stock(self.conn,USER,1,None,1,'2026-11-14')
        self.assertEqual(await stock.stock_map(self.conn,USER),{})

    async def test_foreign_supplement_and_bad_numbers_are_refused(self):
        self.assertFalse(await stock.set_stock(self.conn,USER,2,10,1,'2026-11-10'))
        for units,per in ((-1,1),(10,0),(10,101),(100001,1)):
            with self.assertRaises(ValueError):await stock.set_stock(self.conn,USER,1,units,per,'2026-11-10')

    async def test_low_notice_is_claimed_once_and_again_only_after_a_new_count(self):
        self.assertEqual(await stock.claim_low_notices(self.conn),{})
        await stock.set_stock(self.conn,USER,1,30,1,'2026-11-10')
        self.assertEqual(await stock.claim_low_notices(self.conn),{})
        await stock.set_stock(self.conn,USER,1,4,1,'2026-11-10')
        self.assertEqual(await stock.claim_low_notices(self.conn),{USER:[('N1',4)]})
        self.assertEqual(await stock.claim_low_notices(self.conn),{})
        await stock.set_stock(self.conn,USER,1,2,1,'2026-11-10')
        self.assertEqual(await stock.claim_low_notices(self.conn),{USER:[('N1',2)]})

if __name__=='__main__':unittest.main()
