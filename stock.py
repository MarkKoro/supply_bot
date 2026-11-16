"""How much of a supplement is left in the jar, derived from the intake diary.

The owner states the count once ("90 capsules, 2 per day"). Nothing is decremented by
hand: the remainder is that count minus the days marked "taken" since then, so marks
made in the bot, in the Mini App or undone later are all reflected without extra writers.
"""
import db

SCHEMA = """CREATE TABLE IF NOT EXISTS supplement_stock (
  user_id       INTEGER NOT NULL,
  supplement_id INTEGER NOT NULL,
  units         REAL NOT NULL,
  per_day       REAL NOT NULL,
  counted_from  TEXT NOT NULL,
  updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
  notified_at   TEXT,
  PRIMARY KEY (user_id, supplement_id)
)"""
LOW_DAYS = 5


async def _ensure(conn):
    await conn.execute(SCHEMA)
    cursor = await conn.execute("PRAGMA table_info(supplement_stock)")
    if "notified_at" not in {row[1] for row in await cursor.fetchall()}:
        await conn.execute("ALTER TABLE supplement_stock ADD COLUMN notified_at TEXT")


async def set_stock(conn, tg_user_id, supplement_id, units, per_day, today):
    """Save the stated count, or forget it when units is None. Returns False for an unknown supplement."""
    await _ensure(conn)
    user_id = await db.get_or_create_user(conn, tg_user_id)
    owned = await db.fetchone(conn, "SELECT 1 FROM user_supplements WHERE user_id=? AND supplement_id=?", (user_id, supplement_id))
    if not owned:
        return False
    if units is None:
        await conn.execute("DELETE FROM supplement_stock WHERE user_id=? AND supplement_id=?", (user_id, supplement_id))
        await conn.commit()
        return True
    if not (0 <= units <= 100000 and 0 < per_day <= 100):
        raise ValueError("invalid stock")
    # A dose already marked today came out of the jar before it was counted.
    taken = await db.fetchone(conn, "SELECT 1 FROM supplement_day_state WHERE user_id=? AND supplement_id=? AND day=? AND status='taken'", (user_id, supplement_id, today))
    counted_from = today + "~" if taken else today
    await conn.execute(
        "INSERT INTO supplement_stock(user_id,supplement_id,units,per_day,counted_from) VALUES(?,?,?,?,?) "
        "ON CONFLICT(user_id,supplement_id) DO UPDATE SET units=excluded.units,per_day=excluded.per_day,counted_from=excluded.counted_from,updated_at=datetime('now'),notified_at=NULL",
        (user_id, supplement_id, float(units), float(per_day), counted_from))
    await conn.commit()
    return True


async def stock_map(conn, tg_user_id):
    """{supplement_id: {units_left, per_day, days_left, low}} for supplements with a stated count."""
    exists = await db.fetchone(conn, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='supplement_stock'")
    if not exists:
        return {}
    user_id = await db.get_or_create_user(conn, tg_user_id)
    cursor = await conn.execute(
        "SELECT k.supplement_id,k.units,k.per_day,"
        "(SELECT COUNT(*) FROM supplement_day_state d WHERE d.user_id=k.user_id AND d.supplement_id=k.supplement_id AND d.status='taken' AND d.day>=k.counted_from) AS taken_days "
        "FROM supplement_stock k WHERE k.user_id=?", (user_id,))
    result = {}
    for row in await cursor.fetchall():
        left = max(0.0, float(row[1]) - float(row[2]) * int(row[3]))
        days = int(left // float(row[2]))
        result[int(row[0])] = {"units_left": round(left, 2), "per_day": float(row[2]), "days_left": days, "low": days <= LOW_DAYS}
    await cursor.close()
    return result


async def claim_low_notices(conn):
    """Jars that just became low, grouped by owner. Claimed before sending: one notice per stated count."""
    exists = await db.fetchone(conn, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='supplement_stock'")
    if not exists:
        return {}
    await _ensure(conn)
    cursor = await conn.execute(
        "SELECT u.tg_user_id,k.user_id,k.supplement_id,COALESCE(NULLIF(p.name_ru,''),p.name,'Добавка'),k.units,k.per_day,"
        "(SELECT COUNT(*) FROM supplement_day_state d WHERE d.user_id=k.user_id AND d.supplement_id=k.supplement_id AND d.status='taken' AND d.day>=k.counted_from) "
        "FROM supplement_stock k JOIN users u ON u.id=k.user_id JOIN supplements p ON p.id=k.supplement_id "
        "JOIN user_supplements us ON us.user_id=k.user_id AND us.supplement_id=k.supplement_id AND us.is_active=1 AND us.enabled=1 "
        "WHERE k.notified_at IS NULL AND COALESCE(u.is_banned,0)=0")
    rows = await cursor.fetchall()
    await cursor.close()
    result = {}
    for tg_user_id, user_id, supplement_id, name, units, per_day, taken in rows:
        days = int(max(0.0, float(units) - float(per_day) * int(taken)) // float(per_day))
        if days > LOW_DAYS:
            continue
        await conn.execute("UPDATE supplement_stock SET notified_at=datetime('now') WHERE user_id=? AND supplement_id=? AND notified_at IS NULL", (user_id, supplement_id))
        result.setdefault(int(tg_user_id), []).append((str(name), days))
    await conn.commit()
    return result
