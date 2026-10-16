"""Public native pricing and exported paid-credit inputs use the same tariff."""
import ast
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from native_billing import monthly_tariffs, stars_amount_for_plan
from progressor_native_subscription import prepare_metadata


class NativeTariffTests(unittest.TestCase):
    def settings(self, **overrides):
        return SimpleNamespace(PREMIUM_PRICES_RUB={'m1':20000,'m3':54000,'m6':96000,'m12':168000,**overrides},
                               PREMIUM_PRICES_STARS={'m1':250,'m3':650,'m6':1200,'m12':2000})

    def load_function(self, filename, name, **globals_):
        path = Path(__file__).resolve().parents[1] / filename
        node = next(n for n in ast.parse(path.read_text(encoding='utf-8')).body if isinstance(n,ast.FunctionDef) and n.name==name)
        namespace = dict(globals_)
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),namespace)
        return namespace[name]

    def test_actual_api_and_bot_prices_stay_the_same_for_all_periods(self):
        settings=self.settings()
        api=self.load_function('webapp_backend.py','_stars_amount_for_plan',s=settings,stars_amount_for_plan=stars_amount_for_plan)
        bot=self.load_function('bot.py','_stars_amount_for_plan')
        for plan, expected in [('m1',133),('m3',360),('m6',640),('m12',1120)]:
            with self.subTest(plan=plan):
                self.assertEqual(api(plan),expected)
                self.assertEqual(bot(settings,plan),expected)
        self.assertEqual(monthly_tariffs(settings),{'RUB':20000,'XTR':api('m1')})

    def test_ruble_override_controls_all_prices_and_legacy_stars_map_does_not(self):
        settings=self.settings(m1=30000)
        settings.PREMIUM_PRICES_STARS['m1']=99999
        self.assertEqual(monthly_tariffs(settings),{'RUB':30000,'XTR':200})
        self.assertEqual(stars_amount_for_plan(settings,'m1'),200)
        self.assertEqual(stars_amount_for_plan(settings,'unknown'),0)

    def test_owner_publisher_writes_the_effective_price_to_snapshot_table(self):
        with tempfile.TemporaryDirectory() as temp:
            path=str(Path(temp)/'supply.sqlite3')
            with closing(sqlite3.connect(path)) as db:
                db.executescript('CREATE TABLE users(id INTEGER PRIMARY KEY,tg_user_id INTEGER,premium_until_utc TEXT); CREATE TABLE payments(id INTEGER,user_id INTEGER,status TEXT,amount INTEGER,telegram_payment_charge_id TEXT,provider_payment_charge_id TEXT,created_at_utc TEXT,plan_id TEXT);')
            prepare_metadata(path,'supply',monthly_tariffs(self.settings()))
            with closing(sqlite3.connect(path)) as db:
                row=db.execute('SELECT body,updated_at FROM progressor_native_tariffs WHERE source=?',('supply',)).fetchone()
            import json
            self.assertEqual(json.loads(row[0]),{'RUB':20000,'XTR':133})
            self.assertLess(abs(time.time()-row[1]),10)

    def test_missing_or_disabled_monthly_tariff_fails_publication(self):
        settings=self.settings(m1=0)
        self.assertEqual(stars_amount_for_plan(settings,'m1'),0)
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                prepare_metadata(str(Path(temp)/'supply.sqlite3'),'supply',monthly_tariffs(settings))

    def test_native_menu_has_equal_currency_pairs_and_preserves_extra_period(self):
        from keyboards import premium_plans_kb
        settings=self.settings();settings.PREMIUM_PLANS={'m1':1,'m3':3,'m6':6,'m12':12}
        payload=self.load_function('bot.py','_premium_plans_payload',_stars_amount_for_plan=stars_amount_for_plan)(settings)
        menu=premium_plans_kb(payload,'https://example.test/supply').inline_keyboard
        pairs=[row for row in menu if len(row)==2]
        self.assertEqual(len(pairs),3)
        for row,plan in zip(pairs,('m1','m6','m12')):
            self.assertIn('plan_id='+plan,row[0].web_app.url)
            self.assertEqual(row[1].callback_data,'premium:buy:'+plan+':stars')
        self.assertIn('133',pairs[0][1].text)
        other=premium_plans_kb(payload,'',other_periods=True).inline_keyboard
        pair=next(row for row in other if len(row)==2)
        self.assertEqual([b.callback_data for b in pair],['premium:buy:m3:yookassa','premium:buy:m3:stars'])
        self.assertIn('premium:main_periods',[b.callback_data for row in other for b in row])
