"""Owner-local attempt control. No network/Core dependency and no prompt storage.

Business quotas remain separate. Failed attempts count here; a provider failure must
not become an unlimited retry budget. Leases are token-owned and survive processes.
"""
import sqlite3
import time
import uuid
from contextlib import contextmanager

SCHEMA = """CREATE TABLE IF NOT EXISTS progressor_ai_attempts(
 token TEXT PRIMARY KEY, subject TEXT NOT NULL, scope TEXT NOT NULL,
 started REAL NOT NULL, expires REAL NOT NULL, finished REAL, successful INTEGER NOT NULL DEFAULT 0);
 CREATE INDEX IF NOT EXISTS progressor_ai_attempt_subject
 ON progressor_ai_attempts(subject,scope,started);"""


class AttemptLimited(Exception):
    def __init__(self, reason, retry_after):
        self.reason = reason
        self.retry_after = max(1, int(retry_after))
        message='Предыдущий расчёт ещё идёт. Дождитесь результата.' if reason=='in_flight' else f'Повторный расчёт доступен через {self.retry_after} с.' if reason=='cooldown' else 'Слишком много попыток за короткое время. Попробуйте позже.'
        super().__init__(message)


def acquire(path, subject, scope, *, hourly=18, daily=100, cooldown=15, success_cooldown=None, lease_seconds=900, now=None):
    stamp = time.time() if now is None else now
    db = sqlite3.connect(str(path), timeout=5)
    try:
        db.executescript(SCHEMA)
        db.execute('BEGIN IMMEDIATE')
        if 'successful' not in {r[1] for r in db.execute('PRAGMA table_info(progressor_ai_attempts)')}:
            db.execute('ALTER TABLE progressor_ai_attempts ADD COLUMN successful INTEGER NOT NULL DEFAULT 0')
        db.execute('DELETE FROM progressor_ai_attempts WHERE started<? AND expires<?', (stamp-86400*2,stamp))
        rows = db.execute('SELECT started,expires,finished,successful FROM progressor_ai_attempts WHERE subject=? AND scope=? AND started>? ORDER BY started DESC',
                          (str(subject),scope,stamp-86400)).fetchall()
        active = next((r for r in rows if r[2] is None and r[1]>stamp), None)
        if active: raise AttemptLimited('in_flight',active[1]-stamp)
        effective_cooldown=success_cooldown if rows and rows[0][3] and success_cooldown is not None else cooldown
        if rows and stamp-rows[0][0]<effective_cooldown:
            raise AttemptLimited('cooldown',effective_cooldown-(stamp-rows[0][0]))
        recent=[r for r in rows if r[0]>stamp-3600]
        if len(recent)>=hourly: raise AttemptLimited('hourly_attempts',recent[-1][0]+3600-stamp)
        if len(rows)>=daily: raise AttemptLimited('daily_attempts',rows[-1][0]+86400-stamp)
        token=uuid.uuid4().hex
        db.execute('INSERT INTO progressor_ai_attempts(token,subject,scope,started,expires,finished) VALUES(?,?,?,?,?,NULL)',(token,str(subject),scope,stamp,stamp+lease_seconds))
        db.commit()
        return token
    finally: db.close()


def finish(path, token, *, successful=False):
    db=sqlite3.connect(str(path),timeout=5)
    try:
        db.execute('UPDATE progressor_ai_attempts SET finished=?,successful=? WHERE token=? AND finished IS NULL',(time.time(),int(successful),token))
        db.commit()
    finally: db.close()


@contextmanager
def attempt(path, subject, scope, **kwargs):
    token=acquire(path,subject,scope,**kwargs)
    try: yield token
    finally: finish(path,token)
