"""Native Supply decision and atomic private export: synthetic SQLite only."""
import asyncio
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing,contextmanager
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch

import db as native_db
import legal_consent as legal
from progressor_client import publish_source_snapshot

USER,OTHER=2**60+123,2**60+456
OFFER,PRIVACY='fixture-offer-v1','fixture-privacy-v1'

@contextmanager
def read_only(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        yield db

class OwnerPublication(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.live=self.root/'live.db';self.copy=self.root/'published/source.sqlite3'
        with closing(sqlite3.connect(self.live)) as db,db:
            db.execute('CREATE TABLE users(id INTEGER PRIMARY KEY,tg_user_id INTEGER,consent_given INTEGER,consent_offer_version TEXT,consent_privacy_version TEXT,weight REAL)')
            db.execute('INSERT INTO users VALUES(55,?,1,?,?,79)',(USER,OFFER,PRIVACY))

    def test_native_predicate_and_export_decision_agree_for_all_document_combinations(self):
        async def check():
            import aiosqlite
            async with aiosqlite.connect(self.live) as db:
                db.row_factory=aiosqlite.Row
                for flag in (0,1,2,None):
                    for offer in (OFFER,'old',None):
                        for privacy in (PRIVACY,'old',None):
                            await db.execute('UPDATE users SET consent_given=?,consent_offer_version=?,consent_privacy_version=?',(flag,offer,privacy));await db.commit()
                            row=await native_db.fetchone(db,'SELECT * FROM users WHERE id=55')
                            native=await native_db.has_required_consent(db,USER,offer_version=OFFER,privacy_version=PRIVACY)
                            self.assertEqual(native,legal.decision(row,legal.policy(OFFER,PRIVACY))['current'])
                            publish_source_snapshot(self.live,self.copy,legal.metadata_writer(OFFER,PRIVACY))
                            with read_only(self.copy) as exported:
                                value=exported.execute('SELECT legal_current,reason FROM progressor_owner_consent WHERE native_user_id=55').fetchone()
                                self.assertEqual(bool(value['legal_current']),native)
                                self.assertEqual(value['reason'],legal.decision(row,legal.policy(OFFER,PRIVACY))['reason'])
                self.assertFalse(await native_db.has_required_consent(db,OTHER,offer_version=OFFER,privacy_version=PRIVACY))
        asyncio.run(check())

    def test_metadata_written_only_to_copy_and_repeated_generations_have_stable_policy(self):
        before=self.live.read_bytes();writer=legal.metadata_writer(OFFER,PRIVACY)
        publish_source_snapshot(self.live,self.copy,writer)
        with read_only(self.copy) as db:
            header=dict(db.execute('SELECT * FROM progressor_owner_policy').fetchone())
            user=dict(db.execute('SELECT * FROM progressor_owner_consent').fetchone())
            self.assertEqual(user['user_subject'],str(USER));self.assertEqual(user['legal_current'],1)
            self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0],'delete')
            self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0],'ok')
        publish_source_snapshot(self.live,self.copy,writer)
        with read_only(self.copy) as db:
            new=dict(db.execute('SELECT * FROM progressor_owner_policy').fetchone())
            self.assertNotEqual(header['generation'],new['generation']);self.assertEqual(header['policy_fingerprint'],new['policy_fingerprint'])
        self.assertEqual(self.live.read_bytes(),before)
        self.assertFalse(self.copy.with_name('source.sqlite3-wal').exists())
        if os.name!='nt':self.assertEqual(self.copy.stat().st_mode&0o777,0o600)

    def test_concurrent_live_change_cannot_mix_consent_with_new_profile(self):
        writer=legal.metadata_writer(OFFER,PRIVACY)
        def concurrent(copy):
            with closing(sqlite3.connect(self.live)) as live,live:live.execute("UPDATE users SET consent_privacy_version='old',weight=81")
            writer(copy)
        publish_source_snapshot(self.live,self.copy,concurrent)
        with read_only(self.copy) as db:
            self.assertEqual(db.execute('SELECT weight FROM users').fetchone()[0],79)
            self.assertEqual(db.execute('SELECT legal_current FROM progressor_owner_consent').fetchone()[0],1)
        publish_source_snapshot(self.live,self.copy,writer)
        with read_only(self.copy) as db:
            self.assertEqual(db.execute('SELECT weight FROM users').fetchone()[0],81)
            self.assertEqual(db.execute('SELECT legal_current FROM progressor_owner_consent').fetchone()[0],0)

    def test_captured_policy_survives_configuration_change_until_next_cycle(self):
        versions=[OFFER,PRIVACY];writer=legal.metadata_writer(*versions);versions[1]='new-privacy'
        publish_source_snapshot(self.live,self.copy,writer)
        with read_only(self.copy) as db:self.assertEqual(db.execute('SELECT legal_current FROM progressor_owner_consent').fetchone()[0],1)
        publish_source_snapshot(self.live,self.copy,legal.metadata_writer(*versions))
        with read_only(self.copy) as db:
            self.assertEqual(db.execute('SELECT legal_current FROM progressor_owner_consent').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT required_privacy_version FROM progressor_owner_policy').fetchone()[0],versions[1])

    def test_failed_or_overdue_metadata_hook_never_replaces_previous_snapshot(self):
        publish_source_snapshot(self.live,self.copy,legal.metadata_writer(OFFER,PRIVACY));before=self.copy.read_bytes()
        def failed(copy):copy.execute('CREATE TABLE partial(value TEXT)');raise RuntimeError('fixture failure')
        with self.assertRaises(RuntimeError):publish_source_snapshot(self.live,self.copy,failed)
        self.assertEqual(self.copy.read_bytes(),before)
        with patch('progressor_client.time.monotonic',side_effect=[0,0,11]):
            with self.assertRaises(TimeoutError):publish_source_snapshot(self.live,self.copy,legal.metadata_writer(OFFER,PRIVACY))
        self.assertEqual(self.copy.read_bytes(),before)
        self.assertEqual(list(self.copy.parent.glob('.source-*')),[])

    def test_unsafe_destination_is_rejected_before_owner_hook(self):
        with patch('progressor_client.Path.is_symlink',return_value=True):
            with self.assertRaises(ValueError):publish_source_snapshot(self.live,self.copy,lambda db:self.fail('hook ran'))


if __name__=='__main__':unittest.main()
