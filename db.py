from __future__ import annotations
from datetime import datetime
from pathlib import Path
import aiosqlite

def utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def normalize(text: str | None) -> str:
    if not text:
        return ""
    value = text.strip().lower()
    for ch in ("-", "_", ".", " "):
        value = value.replace(ch, "")
    return value

# --- Compat helpers: work on all `aiosqlite` versions ---
async def fetchone(conn: aiosqlite.Connection, sql: str, params: tuple = ()):
    async with conn.execute(sql, params) as cur:
        return await cur.fetchone()

async def fetchall(conn: aiosqlite.Connection, sql: str, params: tuple = ()):
    async with conn.execute(sql, params) as cur:
        return await cur.fetchall()

SCHEMA_SQL = """PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  tg_user_id    INTEGER UNIQUE NOT NULL,
  tz_name       TEXT NOT NULL DEFAULT 'UTC',
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS supplements (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  slug          TEXT UNIQUE NOT NULL,
  name          TEXT NOT NULL,
  name_ru       TEXT,
  name_en       TEXT,
  summary_ru    TEXT,
  purchase_url  TEXT,
  tags_json      TEXT,
  mechanisms     TEXT,
  evidence_grade TEXT,
  safety_notes   TEXT
);

CREATE INDEX IF NOT EXISTS idx_supplements_name ON supplements(name);
CREATE INDEX IF NOT EXISTS idx_supplements_slug ON supplements(slug);

CREATE TABLE IF NOT EXISTS supplement_aliases (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  supplement_id INTEGER NOT NULL REFERENCES supplements(id) ON DELETE CASCADE,
  alias         TEXT NOT NULL,
  alias_norm    TEXT,
  kind          TEXT
);
CREATE INDEX IF NOT EXISTS idx_alias_alias ON supplement_aliases(alias);
CREATE INDEX IF NOT EXISTS idx_alias_norm ON supplement_aliases(alias_norm);
CREATE INDEX IF NOT EXISTS idx_alias_supp ON supplement_aliases(supplement_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_alias_unique ON supplement_aliases(supplement_id, alias);

CREATE TABLE IF NOT EXISTS interactions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  a_id          INTEGER NOT NULL REFERENCES supplements(id) ON DELETE CASCADE,
  b_id          INTEGER NOT NULL REFERENCES supplements(id) ON DELETE CASCADE,
  relation      TEXT NOT NULL,
  severity      INTEGER NOT NULL DEFAULT 0,
  evidence_grade TEXT,
  note          TEXT,
  source_url    TEXT,
  CHECK (a_id < b_id),
  UNIQUE (a_id, b_id)
);
CREATE INDEX IF NOT EXISTS idx_interactions_a ON interactions(a_id);
CREATE INDEX IF NOT EXISTS idx_interactions_b ON interactions(b_id);
CREATE INDEX IF NOT EXISTS idx_interactions_rel ON interactions(relation);

CREATE TABLE IF NOT EXISTS user_supplements (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  supplement_id INTEGER NOT NULL REFERENCES supplements(id) ON DELETE CASCADE,
  custom_dose   TEXT,
  notes         TEXT,
  active        INTEGER NOT NULL DEFAULT 1,
  UNIQUE (user_id, supplement_id)
);
CREATE INDEX IF NOT EXISTS idx_user_supp_user ON user_supplements(user_id);

CREATE TABLE IF NOT EXISTS intake_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  supplement_id INTEGER NOT NULL REFERENCES supplements(id) ON DELETE CASCADE,
  taken_at      TEXT NOT NULL,
  dose          TEXT,
  note          TEXT
);
CREATE INDEX IF NOT EXISTS idx_intake_user_time ON intake_log(user_id, taken_at);

CREATE TABLE IF NOT EXISTS daily_checkins (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  date          TEXT NOT NULL,
  energy        INTEGER,
  sleep         INTEGER,
  mood          INTEGER,
  stress        INTEGER,
  training      INTEGER,
  note          TEXT,
  created_at    TEXT NOT NULL,
  UNIQUE(user_id, date)
);

CREATE TABLE IF NOT EXISTS ai_daily_usage (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  date          TEXT NOT NULL,
  used_count    INTEGER NOT NULL DEFAULT 0,
  UNIQUE(user_id, date)
);
"""

async def connect(db_path: str) -> aiosqlite.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    return conn

async def init_db(db_path: str) -> None:
    conn = await connect(db_path)
    try:
        await conn.executescript(SCHEMA_SQL)
        await _apply_migrations(conn)
        await conn.commit()
    finally:
        await conn.close()

async def _apply_migrations(conn: aiosqlite.Connection) -> None:
    await _ensure_column(conn, "supplements", "name_ru", "TEXT")
    await _ensure_column(conn, "supplements", "name_en", "TEXT")
    await _ensure_column(conn, "supplements", "summary_ru", "TEXT")
    await _ensure_column(conn, "supplements", "purchase_url", "TEXT")
    await _ensure_column(conn, "supplement_aliases", "alias_norm", "TEXT")
    await _ensure_column(conn, "supplement_aliases", "kind", "TEXT")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_alias_norm ON supplement_aliases(alias_norm)")
    await conn.execute(
        """DELETE FROM supplement_aliases
           WHERE id NOT IN (
             SELECT MIN(id) FROM supplement_aliases GROUP BY supplement_id, alias
           )"""
    )
    await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_alias_unique ON supplement_aliases(supplement_id, alias)")
    await _backfill_alias_norm(conn)

async def _ensure_column(conn: aiosqlite.Connection, table: str, column: str, column_type: str) -> None:
    cols = await fetchall(conn, f"PRAGMA table_info({table})")
    if any(c["name"] == column for c in cols):
        return
    await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")

async def _backfill_alias_norm(conn: aiosqlite.Connection) -> None:
    rows = await fetchall(conn, "SELECT id, alias FROM supplement_aliases WHERE alias_norm IS NULL OR alias_norm=''")
    for row in rows:
        alias_norm = normalize(row["alias"])
        await conn.execute("UPDATE supplement_aliases SET alias_norm=? WHERE id=?", (alias_norm, int(row["id"])))

# users
async def get_or_create_user(conn: aiosqlite.Connection, tg_user_id: int, tz_name: str = "UTC") -> int:
    row = await fetchone(conn, "SELECT id FROM users WHERE tg_user_id=?", (tg_user_id,))
    if row:
        return int(row["id"])
    await conn.execute(
        "INSERT INTO users(tg_user_id, tz_name, created_at) VALUES(?,?,?)",
        (tg_user_id, tz_name, utc_now_iso()),
    )
    await conn.commit()
    row2 = await fetchone(conn, "SELECT id FROM users WHERE tg_user_id=?", (tg_user_id,))
    return int(row2["id"])

async def get_user_tz(conn: aiosqlite.Connection, user_id: int) -> str:
    row = await fetchone(conn, "SELECT tz_name FROM users WHERE id=?", (user_id,))
    return str(row["tz_name"]) if row else "UTC"

# catalog
async def upsert_supplement(conn: aiosqlite.Connection, s: dict) -> int:
    name_ru = s.get("name_ru")
    name_en = s.get("name_en")
    display_name = s.get("name") or name_ru or name_en or s.get("slug")
    if name_ru and name_en:
        display_name = f"{name_ru} ({name_en})"
    elif name_ru:
        display_name = name_ru
    elif name_en:
        display_name = name_en
    await conn.execute(
        """INSERT INTO supplements(slug,name,name_ru,name_en,summary_ru,purchase_url,tags_json,mechanisms,evidence_grade,safety_notes)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(slug) DO UPDATE SET
          name=excluded.name,
          name_ru=excluded.name_ru,
          name_en=excluded.name_en,
          summary_ru=excluded.summary_ru,
          purchase_url=excluded.purchase_url,
          tags_json=excluded.tags_json,
          mechanisms=excluded.mechanisms,
          evidence_grade=excluded.evidence_grade,
          safety_notes=excluded.safety_notes
        """,
        (
            s["slug"],
            display_name,
            name_ru,
            name_en,
            s.get("summary_ru"),
            s.get("purchase_url"),
            s.get("tags_json"),
            s.get("mechanisms"),
            s.get("evidence_grade"),
            s.get("safety_notes"),
        ),
    )
    await conn.commit()
    row = await fetchone(conn, "SELECT id FROM supplements WHERE slug=?", (s["slug"],))
    return int(row["id"])

async def get_supplement_by_slug(conn: aiosqlite.Connection, slug: str):
    return await fetchone(conn, "SELECT * FROM supplements WHERE slug=?", (slug,))

async def search_supplements(conn: aiosqlite.Connection, q: str, limit: int = 10):
    if not q or not q.strip():
        return []
    q_norm = normalize(q)
    q_like = f"%{q.strip()}%"
    return await fetchall(
        conn,
        """WITH ranked AS (
            SELECT s.id AS sid, 0 AS rank
            FROM supplements s
            JOIN supplement_aliases a ON a.supplement_id=s.id
            WHERE a.alias_norm=?
            UNION ALL
            SELECT s.id AS sid, 1 AS rank
            FROM supplements s
            LEFT JOIN supplement_aliases a ON a.supplement_id=s.id
            WHERE s.name LIKE ? OR s.slug LIKE ? OR s.name_en LIKE ? OR a.alias LIKE ?
        )
        SELECT s.* FROM ranked r
        JOIN supplements s ON s.id=r.sid
        GROUP BY s.id
        ORDER BY MIN(r.rank), s.name
        LIMIT ?""",
        (q_norm, q_like, q_like, q_like, q_like, limit),
    )

async def list_supplements_page(conn: aiosqlite.Connection, offset: int, limit: int = 10):
    return await fetchall(
        conn,
        "SELECT * FROM supplements ORDER BY name LIMIT ? OFFSET ?",
        (limit, offset),
    )

# interactions
def canon_pair(a_id: int, b_id: int) -> tuple[int, int]:
    return (a_id, b_id) if a_id < b_id else (b_id, a_id)

async def upsert_interaction(conn: aiosqlite.Connection, a_id: int, b_id: int, it: dict) -> None:
    a, b = canon_pair(a_id, b_id)
    await conn.execute(
        """INSERT INTO interactions(a_id,b_id,relation,severity,evidence_grade,note,source_url)
        VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(a_id,b_id) DO UPDATE SET
          relation=excluded.relation,
          severity=excluded.severity,
          evidence_grade=excluded.evidence_grade,
          note=excluded.note,
          source_url=excluded.source_url
        """,
        (a, b, it["relation"], int(it.get("severity", 0)), it.get("evidence_grade"), it.get("note"), it.get("source_url")),
    )
    await conn.commit()

async def list_interactions_for_supplement(conn: aiosqlite.Connection, supp_id: int, relation: str | None = None, limit: int = 20):
    if relation:
        return await fetchall(
            conn,
            """SELECT * FROM interactions
               WHERE (a_id=? OR b_id=?) AND relation=?
               ORDER BY severity DESC LIMIT ?""",
            (supp_id, supp_id, relation, limit),
        )
    return await fetchall(
        conn,
        """SELECT * FROM interactions
           WHERE (a_id=? OR b_id=?)
           ORDER BY severity DESC LIMIT ?""",
        (supp_id, supp_id, limit),
    )

# user library
async def add_user_supplement(conn: aiosqlite.Connection, user_id: int, supplement_id: int):
    await conn.execute(
        "INSERT OR IGNORE INTO user_supplements(user_id, supplement_id) VALUES(?,?)",
        (user_id, supplement_id),
    )
    await conn.commit()

async def remove_user_supplement(conn: aiosqlite.Connection, user_id: int, supplement_id: int):
    await conn.execute("DELETE FROM user_supplements WHERE user_id=? AND supplement_id=?", (user_id, supplement_id))
    await conn.commit()

async def list_user_supplements(conn: aiosqlite.Connection, user_id: int, limit: int = 50):
    return await fetchall(
        conn,
        """SELECT s.* FROM user_supplements us
           JOIN supplements s ON s.id=us.supplement_id
           WHERE us.user_id=? AND us.active=1
           ORDER BY s.name LIMIT ?""",
        (user_id, limit),
    )

async def user_has_supplement(conn: aiosqlite.Connection, user_id: int, supplement_id: int) -> bool:
    row = await fetchone(
        conn,
        "SELECT 1 FROM user_supplements WHERE user_id=? AND supplement_id=? AND active=1",
        (user_id, supplement_id),
    )
    return row is not None

# checkins
async def upsert_checkin(conn: aiosqlite.Connection, user_id: int, date: str, data: dict):
    await conn.execute(
        """INSERT INTO daily_checkins(user_id,date,energy,sleep,mood,stress,training,note,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(user_id,date) DO UPDATE SET
          energy=excluded.energy,
          sleep=excluded.sleep,
          mood=excluded.mood,
          stress=excluded.stress,
          training=excluded.training,
          note=excluded.note
        """,
        (
            user_id, date,
            data.get("energy"),
            data.get("sleep"),
            data.get("mood"),
            data.get("stress"),
            data.get("training"),
            data.get("note"),
            utc_now_iso(),
        ),
    )
    await conn.commit()

async def list_checkins(conn: aiosqlite.Connection, user_id: int, limit: int = 7):
    return await fetchall(
        conn,
        "SELECT * FROM daily_checkins WHERE user_id=? ORDER BY date DESC LIMIT ?",
        (user_id, limit),
    )

# AI limiter
async def ai_can_use_today(conn: aiosqlite.Connection, user_id: int, date: str, max_per_day: int) -> bool:
    row = await fetchone(conn, "SELECT used_count FROM ai_daily_usage WHERE user_id=? AND date=?", (user_id, date))
    used = int(row["used_count"]) if row else 0
    return used < max_per_day

async def ai_inc_today(conn: aiosqlite.Connection, user_id: int, date: str) -> None:
    await conn.execute(
        """INSERT INTO ai_daily_usage(user_id,date,used_count) VALUES(?,?,1)
        ON CONFLICT(user_id,date) DO UPDATE SET used_count=used_count+1""",
        (user_id, date),
    )
    await conn.commit()

async def ai_reset_user_today(conn: aiosqlite.Connection, user_id: int, date: str) -> None:
    await conn.execute("DELETE FROM ai_daily_usage WHERE user_id=? AND date=?", (user_id, date))
    await conn.commit()

async def ai_reset_all_usage(conn: aiosqlite.Connection) -> None:
    await conn.execute("DELETE FROM ai_daily_usage")
    await conn.commit()
