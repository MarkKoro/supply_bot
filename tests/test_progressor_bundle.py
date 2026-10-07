import os
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import aiosqlite
import db
import settings
import progressor_client as client

class BundleAccess(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=str(Path(self.tmp.name)/'supply.db');self.env=patch.dict(os.environ,PROGRESSOR_CLIENT_ENABLED='1');self.env.start()
        self.settings=patch.object(settings,'get_settings',lambda:SimpleNamespace(DB_PATH=self.path));self.settings.start()
        self.conn=await aiosqlite.connect(self.path);self.conn.row_factory=aiosqlite.Row
        await self.conn.execute('CREATE TABLE users(tg_user_id INTEGER PRIMARY KEY,premium_until_utc TEXT)');await self.conn.execute('INSERT INTO users VALUES(123,NULL)');await self.conn.commit();client.ensure_schema(self.path)
    async def asyncTearDown(self):
        await self.conn.close();self.settings.stop();self.env.stop();self.tmp.cleanup()
    async def test_bundle_access_and_refund_are_independent_of_native(self):
        future=(datetime.now(timezone.utc)+timedelta(days=30)).isoformat()
        client.apply_grants(self.path,[{'user_id':'123','expires_at':future,'revision':1}]);self.assertTrue(await db.is_premium_active(self.conn,123));self.assertIsNone(await db.get_premium_until(self.conn,123))
        client.apply_grants(self.path,[{'user_id':'123','expires_at':'1970-01-01T00:00:00Z','revision':2}]);self.assertFalse(await db.is_premium_active(self.conn,123))
        await self.conn.execute('UPDATE users SET premium_until_utc=?',(future,));await self.conn.commit();self.assertTrue(await db.is_premium_active(self.conn,123))
