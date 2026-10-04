from __future__ import annotations
import argparse
import asyncio
import os
from pathlib import Path
import sqlite3
import tempfile
import db


def check_database(path: str) -> None:
    """Inspect an existing database without creating it or running migrations."""
    source = Path(path).resolve(strict=True)
    conn = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
    try:
        if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise RuntimeError('Database integrity check failed')
        if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('Database has foreign key violations')
    finally:
        conn.close()


def backup_database(path: str, destination: str) -> None:
    """Snapshot committed WAL data; publish only a complete, verified new file."""
    source = Path(path).resolve(strict=True)
    target = Path(destination).resolve()
    if target.exists() or source == target:
        raise FileExistsError('Backup destination must be a new file')
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.supply-backup-', suffix='.sqlite3', dir=target.parent)
    os.close(fd)
    try:
        src = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
        dst = sqlite3.connect(temporary)
        try:
            src.backup(dst, pages=256)
        finally:
            dst.close()
            src.close()
        check_database(temporary)
        # Linking fails if the target appeared meanwhile; never overwrite a backup.
        os.link(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


async def self_test() -> None:
    """Offline smoke regressions. Never use DB_PATH, credentials or live services."""
    from types import SimpleNamespace
    from unittest.mock import patch

    def require(condition, message):
        if not condition:
            raise AssertionError(message)

    with tempfile.TemporaryDirectory(prefix='supply-self-test-') as folder:
        path = str(Path(folder) / 'test.sqlite3')
        # Import settings without loading a real .env, then isolate role bootstrap.
        with patch.dict(os.environ, {'PYTHON_DOTENV_DISABLED': '1'}):
            import settings
        with patch.object(settings, 'get_settings', return_value=SimpleNamespace()):
            await db.init_db(path)
            conn = await db.connect(path)
            try:
                await db.get_or_create_user(conn, 101)
                await db.get_or_create_user(conn, 202)
                await db.upsert_user_health_profile(conn, 101, {'height_cm': 180, 'weight_kg': '74,5'})
                await conn.commit()
                await db.init_db(path)
                await db.upsert_user_health_profile(conn, 101, {'city': 'offline-test'})
                await conn.commit()
                profile = await db.get_user_health_profile(conn, 101)
                require(profile['height_cm'] == 180 and profile['weight_kg'] == 74.5, 'Profile fields lost')
                print('OK: repeat migration and partial profile update')
                try:
                    db.validate_lab_result({'value_num': 'NaN'})
                except ValueError:
                    pass
                else:
                    raise AssertionError('Non-finite lab value accepted')
                print('OK: invalid laboratory value rejected')

                try:
                    async with db.transaction(conn) as tx:
                        await db.extend_premium_days(tx, 202, 30, 'self-test', 'rollback')
                        raise RuntimeError('injected interruption')
                except RuntimeError:
                    pass
                require(not await db.get_premium_until(conn, 202), 'Helper committed partial transaction')
                fields = dict(provider='stars', plan_id='m1', currency='XTR', amount=1,
                              payload='offline-test', telegram_payment_charge_id='offline-payment')
                first = await db.activate_subscription_payment(conn, 101, 1, **fields)
                repeat = await db.activate_subscription_payment(conn, 101, 1, **fields)
                require(first[0] and not repeat[0] and first[1] == repeat[1], 'Duplicate payment grant')
                print('OK: rollback and duplicate payment delivery (database only)')

                job_id = await db.create_assistant_job(conn, 101, '{}', 'offline-test')
                require(await db.create_assistant_job(conn, 101, '{}', 'offline-test') == job_id, 'Duplicate active job')
                lease = await db.claim_next_assistant_job(conn, 'offline-worker')
                await db.cancel_assistant_job(conn, 101, job_id)
                require(not await db.mark_assistant_job_done(conn, job_id, 1, lease=lease), 'Cancelled job revived')
                require(await db.assistant_usage_current_month(conn, 101) == 1, 'Started job quota lost')
                print('OK: job deduplication, cancellation and quota')

                copy = str(Path(folder) / 'backup.sqlite3')
                backup_database(path, copy)
                restored = sqlite3.connect(copy)
                try:
                    require(restored.execute('SELECT COUNT(*) FROM payments').fetchone()[0] == 1, 'Backup lost WAL payment')
                finally:
                    restored.close()
                try:
                    backup_database(path, copy)
                except FileExistsError:
                    pass
                else:
                    raise AssertionError('Existing backup was overwritten')
                await db.init_db(copy)
                check_database(copy)
                check_database(path)
                print('OK: WAL backup, no overwrite, restored migration and integrity')
            finally:
                await conn.close()
    print('Self-test passed; no live database or external service used.')


def main() -> None:
    parser = argparse.ArgumentParser(description='Initialize, inspect or back up the GoodyBady database')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--self-test', action='store_true', help='Offline regressions in a temporary database')
    mode.add_argument('--check', action='store_true', help='Read-only SQLite integrity and foreign key checks')
    mode.add_argument('--backup', metavar='NEW_PATH', help='Verified SQLite backup to a new file')
    parser.add_argument('--db', help='Explicit database path; otherwise use DB_PATH from settings')
    args = parser.parse_args()
    if args.self_test:
        if args.db:
            parser.error('--self-test does not accept --db; it always uses a temporary database')
        asyncio.run(self_test())
        return
    path = args.db
    if not path:
        from settings import get_settings
        path = get_settings().DB_PATH
    if args.check:
        check_database(path)
        print('Database integrity and foreign keys: OK')
    elif args.backup:
        backup_database(path, args.backup)
        print(f'Backup verified: {args.backup}')
    else:
        asyncio.run(db.init_db(path))
        print(f'DB initialized: {path}')

if __name__ == "__main__":
    main()
