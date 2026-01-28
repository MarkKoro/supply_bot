from __future__ import annotations
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import json
import re
import aiosqlite

def utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def normalize(text: str | None) -> str:
    if not text:
        return ""
    value = text.strip().lower().replace("ё", "е")
    value = re.sub(r"[-_.]", " ", value)
    value = re.sub(r"([a-zа-я])(\d)", r"\1 \2", value, flags=re.IGNORECASE)
    value = re.sub(r"(\d)([a-zа-я])", r"\1 \2", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value)
    return value.strip()

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
  tz_name       TEXT DEFAULT '',
  created_at    TEXT NOT NULL,
  last_view     TEXT NOT NULL DEFAULT 'menu',
  last_view_payload TEXT NOT NULL DEFAULT '',
  last_chat_id  INTEGER NOT NULL DEFAULT 0,
  last_message_id INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS supplements (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  slug          TEXT UNIQUE NOT NULL,
  name          TEXT NOT NULL,
  name_ru       TEXT,
  name_en       TEXT,
  aliases       TEXT DEFAULT '',
  alias_norm    TEXT DEFAULT '',
  human_trials_count_est TEXT DEFAULT '',
  participants_total_est TEXT DEFAULT '',
  avg_effect_percent_est TEXT DEFAULT '',
  confidence    TEXT DEFAULT '',
  tags          TEXT DEFAULT '',
  goals         TEXT DEFAULT '',
  rules_tags    TEXT DEFAULT '',
  summary_ru    TEXT,
  summary_verified TEXT,
  summary_theoretical TEXT,
  molecular_mechanisms TEXT,
  evidence_json TEXT,
  dose_ru TEXT,
  timing_ru TEXT,
  safety_ru TEXT,
  purchase_url  TEXT,
  tags_json      TEXT,
  mechanisms     TEXT,
  evidence_grade TEXT,
  safety_notes   TEXT,
  interaction_classes TEXT DEFAULT '[]',
  interaction_classes_json TEXT DEFAULT '[]',
  payload_json TEXT,
  is_active INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_supplements_name ON supplements(name);
CREATE INDEX IF NOT EXISTS idx_supplements_slug ON supplements(slug);
CREATE INDEX IF NOT EXISTS idx_supplements_alias_norm ON supplements(alias_norm);

CREATE TABLE IF NOT EXISTS supplement_aliases (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  supplement_id INTEGER NOT NULL REFERENCES supplements(id) ON DELETE CASCADE,
  alias_raw     TEXT NOT NULL,
  alias_norm    TEXT
);
CREATE INDEX IF NOT EXISTS idx_alias_norm ON supplement_aliases(alias_norm);
CREATE INDEX IF NOT EXISTS idx_alias_supp ON supplement_aliases(supplement_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_alias_unique_norm ON supplement_aliases(supplement_id, alias_norm);

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
  is_active     INTEGER NOT NULL DEFAULT 1,
  dose_text     TEXT,
  note_text     TEXT,
  created_at    TEXT NOT NULL,
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
  day           TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  payload_json  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_plan_overrides (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  day           TEXT NOT NULL,
  override_json TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  UNIQUE(user_id, day)
);

CREATE TABLE IF NOT EXISTS ai_daily_usage (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  date          TEXT NOT NULL,
  used_count    INTEGER NOT NULL DEFAULT 0,
  UNIQUE(user_id, date)
);

CREATE TABLE IF NOT EXISTS user_schedules (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  supplement_id INTEGER NOT NULL REFERENCES supplements(id) ON DELETE CASCADE,
  time_hhmm     TEXT,
  is_enabled    INTEGER NOT NULL DEFAULT 1,
  UNIQUE(user_id, supplement_id)
);

CREATE TABLE IF NOT EXISTS user_intake_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  supplement_id INTEGER NOT NULL REFERENCES supplements(id) ON DELETE CASCADE,
  local_date    TEXT NOT NULL,
  status        TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  UNIQUE(user_id, supplement_id, local_date)
);

CREATE TABLE IF NOT EXISTS user_supp_settings (
  user_id       INTEGER NOT NULL,
  supplement_id INTEGER NOT NULL,
  dose_text     TEXT NOT NULL DEFAULT '',
  reminder_time TEXT NOT NULL DEFAULT '',
  reminder_enabled INTEGER NOT NULL DEFAULT 0,
  taken_date    TEXT NOT NULL DEFAULT '',
  skip_date     TEXT NOT NULL DEFAULT '',
  updated_at    TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(user_id, supplement_id)
);

CREATE TABLE IF NOT EXISTS curated_stacks (
  id INTEGER PRIMARY KEY,
  slug TEXT UNIQUE NOT NULL,
  title_ru TEXT NOT NULL,
  author_ru TEXT,
  description_ru TEXT,
  source_url TEXT,
  tags_json TEXT,
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_curated_stacks_active ON curated_stacks(is_active);

CREATE TABLE IF NOT EXISTS curated_stack_items (
  id INTEGER PRIMARY KEY,
  stack_id INTEGER NOT NULL REFERENCES curated_stacks(id) ON DELETE CASCADE,
  supplement_slug TEXT,
  supplement_name TEXT,
  dose_ru TEXT,
  timing_ru TEXT,
  notes_ru TEXT,
  sort_order INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_curated_stack_items_stack_sort ON curated_stack_items(stack_id, sort_order);
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
    await _ensure_column(conn, "users", "tz_name", "TEXT")
    await _ensure_column(conn, "users", "last_view", "TEXT NOT NULL DEFAULT 'menu'")
    await _ensure_column(conn, "users", "last_view_payload", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "users", "last_chat_id", "INTEGER NOT NULL DEFAULT 0")
    await _ensure_column(conn, "users", "last_message_id", "INTEGER NOT NULL DEFAULT 0")
    await _ensure_column(conn, "supplements", "name_ru", "TEXT")
    await _ensure_column(conn, "supplements", "name_en", "TEXT")
    await _ensure_column(conn, "supplements", "aliases", "TEXT DEFAULT ''")
    await _ensure_column(conn, "supplements", "alias_norm", "TEXT DEFAULT ''")
    await _ensure_column(conn, "supplements", "human_trials_count_est", "TEXT DEFAULT ''")
    await _ensure_column(conn, "supplements", "participants_total_est", "TEXT DEFAULT ''")
    await _ensure_column(conn, "supplements", "avg_effect_percent_est", "TEXT DEFAULT ''")
    await _ensure_column(conn, "supplements", "confidence", "TEXT DEFAULT ''")
    await _ensure_column(conn, "supplements", "tags", "TEXT DEFAULT ''")
    await _ensure_column(conn, "supplements", "goals", "TEXT DEFAULT ''")
    await _ensure_column(conn, "supplements", "rules_tags", "TEXT DEFAULT ''")
    await _ensure_column(conn, "supplements", "summary_ru", "TEXT")
    await _ensure_column(conn, "supplements", "summary_verified", "TEXT")
    await _ensure_column(conn, "supplements", "summary_theoretical", "TEXT")
    await _ensure_column(conn, "supplements", "molecular_mechanisms", "TEXT")
    await _ensure_column(conn, "supplements", "evidence_json", "TEXT")
    await _ensure_column(conn, "supplements", "dose_ru", "TEXT")
    await _ensure_column(conn, "supplements", "timing_ru", "TEXT")
    await _ensure_column(conn, "supplements", "safety_ru", "TEXT")
    await _ensure_column(conn, "supplements", "purchase_url", "TEXT")
    await _ensure_column(conn, "supplements", "interaction_classes", "TEXT DEFAULT '[]'")
    await _ensure_column(conn, "supplements", "interaction_classes_json", "TEXT DEFAULT '[]'")
    await _ensure_column(conn, "supplements", "payload_json", "TEXT")
    await _ensure_column(conn, "supplements", "is_active", "INTEGER NOT NULL DEFAULT 1")
    await _ensure_aliases_table(conn)
    await _ensure_user_supplements(conn)
    await _ensure_user_schedules(conn)
    await _ensure_user_intake_log(conn)
    await _ensure_user_supp_settings(conn)
    await _ensure_daily_checkins(conn)
    await _ensure_daily_plan_overrides(conn)
    await _ensure_curated_stacks(conn)
    await _ensure_curated_stack_items(conn)
    await conn.execute(
        "UPDATE supplements SET interaction_classes='[]' WHERE interaction_classes IS NULL OR interaction_classes=''"
    )
    await conn.execute(
        "UPDATE supplements SET interaction_classes_json='[]' WHERE interaction_classes_json IS NULL OR interaction_classes_json=''"
    )
    await conn.execute(
        "UPDATE supplements SET aliases='' WHERE aliases IS NULL"
    )
    await conn.execute(
        "UPDATE supplements SET alias_norm='' WHERE alias_norm IS NULL"
    )
    await conn.execute(
        "UPDATE supplements SET tags='' WHERE tags IS NULL"
    )
    await conn.execute(
        "UPDATE supplements SET goals='' WHERE goals IS NULL"
    )
    await conn.execute(
        "UPDATE supplements SET rules_tags='' WHERE rules_tags IS NULL"
    )
    await conn.execute(
        "UPDATE users SET last_view='menu' WHERE last_view IS NULL OR last_view=''"
    )
    await conn.execute(
        "UPDATE users SET last_view_payload='' WHERE last_view_payload IS NULL"
    )
    await conn.execute(
        "UPDATE users SET last_chat_id=0 WHERE last_chat_id IS NULL"
    )
    await conn.execute(
        "UPDATE users SET last_message_id=0 WHERE last_message_id IS NULL"
    )
    await _backfill_alias_norm(conn)
    await _backfill_supplement_alias_norm(conn)
    await _backfill_supplement_evidence(conn)

async def _ensure_aliases_table(conn: aiosqlite.Connection) -> None:
    table = await fetchone(
        conn,
        "SELECT name FROM sqlite_master WHERE type='table' AND name='supplement_aliases'",
    )
    if not table:
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS supplement_aliases (
              id            INTEGER PRIMARY KEY AUTOINCREMENT,
              supplement_id INTEGER NOT NULL REFERENCES supplements(id) ON DELETE CASCADE,
              alias_raw     TEXT NOT NULL,
              alias_norm    TEXT
            )"""
        )
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_alias_norm ON supplement_aliases(alias_norm)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_alias_supp ON supplement_aliases(supplement_id)")
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_alias_unique_norm ON supplement_aliases(supplement_id, alias_norm)"
        )
        return

    await _ensure_column(conn, "supplement_aliases", "alias_raw", "TEXT")
    await _ensure_column(conn, "supplement_aliases", "alias_norm", "TEXT")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_alias_norm ON supplement_aliases(alias_norm)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_alias_supp ON supplement_aliases(supplement_id)")
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_alias_unique_norm ON supplement_aliases(supplement_id, alias_norm)"
    )
    await _backfill_alias_raw(conn)
    await conn.execute(
        """DELETE FROM supplement_aliases
           WHERE id NOT IN (
             SELECT MIN(id) FROM supplement_aliases GROUP BY supplement_id, alias_norm
           )"""
    )

async def _ensure_user_supplements(conn: aiosqlite.Connection) -> None:
    await _ensure_column(conn, "user_supplements", "is_active", "INTEGER NOT NULL DEFAULT 1")
    await _ensure_column(conn, "user_supplements", "dose_text", "TEXT")
    await _ensure_column(conn, "user_supplements", "note_text", "TEXT")
    await _ensure_column(conn, "user_supplements", "created_at", "TEXT NOT NULL DEFAULT ''")
    await _backfill_user_supplements(conn)

async def _ensure_user_schedules(conn: aiosqlite.Connection) -> None:
    table = await fetchone(
        conn,
        "SELECT name FROM sqlite_master WHERE type='table' AND name='user_schedules'",
    )
    if not table:
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS user_schedules (
              id            INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              supplement_id INTEGER NOT NULL REFERENCES supplements(id) ON DELETE CASCADE,
              time_hhmm     TEXT,
              is_enabled    INTEGER NOT NULL DEFAULT 1,
              UNIQUE(user_id, supplement_id)
            )"""
        )
        return

    await _ensure_column(conn, "user_schedules", "time_hhmm", "TEXT")
    await _ensure_column(conn, "user_schedules", "is_enabled", "INTEGER NOT NULL DEFAULT 1")
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_schedules_unique ON user_schedules(user_id, supplement_id)"
    )

async def _ensure_user_intake_log(conn: aiosqlite.Connection) -> None:
    table = await fetchone(
        conn,
        "SELECT name FROM sqlite_master WHERE type='table' AND name='user_intake_log'",
    )
    if not table:
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS user_intake_log (
              id            INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              supplement_id INTEGER NOT NULL REFERENCES supplements(id) ON DELETE CASCADE,
              local_date    TEXT NOT NULL,
              status        TEXT NOT NULL,
              updated_at    TEXT NOT NULL,
              UNIQUE(user_id, supplement_id, local_date)
            )"""
        )
        return

    await _ensure_column(conn, "user_intake_log", "local_date", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "user_intake_log", "status", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "user_intake_log", "updated_at", "TEXT NOT NULL DEFAULT ''")
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_intake_unique ON user_intake_log(user_id, supplement_id, local_date)"
    )

async def _ensure_user_supp_settings(conn: aiosqlite.Connection) -> None:
    table = await fetchone(
        conn,
        "SELECT name FROM sqlite_master WHERE type='table' AND name='user_supp_settings'",
    )
    if not table:
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS user_supp_settings (
              user_id       INTEGER NOT NULL,
              supplement_id INTEGER NOT NULL,
              dose_text     TEXT NOT NULL DEFAULT '',
              reminder_time TEXT NOT NULL DEFAULT '',
              reminder_enabled INTEGER NOT NULL DEFAULT 0,
              taken_date    TEXT NOT NULL DEFAULT '',
              skip_date     TEXT NOT NULL DEFAULT '',
              updated_at    TEXT NOT NULL DEFAULT '',
              PRIMARY KEY(user_id, supplement_id)
            )"""
        )
        return
    await _ensure_column(conn, "user_supp_settings", "dose_text", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "user_supp_settings", "reminder_time", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "user_supp_settings", "reminder_enabled", "INTEGER NOT NULL DEFAULT 0")
    await _ensure_column(conn, "user_supp_settings", "taken_date", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "user_supp_settings", "skip_date", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "user_supp_settings", "updated_at", "TEXT NOT NULL DEFAULT ''")

async def _ensure_daily_checkins(conn: aiosqlite.Connection) -> None:
    table = await fetchone(
        conn,
        "SELECT name FROM sqlite_master WHERE type='table' AND name='daily_checkins'",
    )
    if not table:
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS daily_checkins (
              id            INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              day           TEXT NOT NULL,
              created_at    TEXT NOT NULL,
              payload_json  TEXT NOT NULL
            )"""
        )
        return
    await _ensure_column(conn, "daily_checkins", "day", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "daily_checkins", "payload_json", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "daily_checkins", "created_at", "TEXT NOT NULL DEFAULT ''")
    cols = await fetchall(conn, "PRAGMA table_info(daily_checkins)")
    col_names = {c["name"] for c in cols}
    if "date" in col_names:
        await conn.execute("UPDATE daily_checkins SET day=date WHERE day IS NULL OR day=''")
    await conn.execute(
        "UPDATE daily_checkins SET created_at=? WHERE created_at IS NULL OR created_at=''",
        (utc_now_iso(),),
    )
    await conn.execute(
        "UPDATE daily_checkins SET payload_json='{}' WHERE payload_json IS NULL OR payload_json=''",
    )
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_checkins_user_day_created ON daily_checkins(user_id, day, created_at)"
    )

async def _ensure_daily_plan_overrides(conn: aiosqlite.Connection) -> None:
    table = await fetchone(
        conn,
        "SELECT name FROM sqlite_master WHERE type='table' AND name='daily_plan_overrides'",
    )
    if not table:
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS daily_plan_overrides (
              id            INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              day           TEXT NOT NULL,
              override_json TEXT NOT NULL,
              created_at    TEXT NOT NULL,
              UNIQUE(user_id, day)
            )"""
        )
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_plan_overrides_user_day ON daily_plan_overrides(user_id, day)"
    )

async def _ensure_curated_stacks(conn: aiosqlite.Connection) -> None:
    table = await fetchone(
        conn,
        "SELECT name FROM sqlite_master WHERE type='table' AND name='curated_stacks'",
    )
    if not table:
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS curated_stacks (
              id INTEGER PRIMARY KEY,
              slug TEXT UNIQUE NOT NULL,
              title_ru TEXT NOT NULL,
              author_ru TEXT,
              description_ru TEXT,
              source_url TEXT,
              tags_json TEXT,
              is_active INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )"""
        )
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_curated_stacks_active ON curated_stacks(is_active)")
        return

    await _ensure_column(conn, "curated_stacks", "slug", "TEXT")
    await _ensure_column(conn, "curated_stacks", "title_ru", "TEXT")
    await _ensure_column(conn, "curated_stacks", "author_ru", "TEXT")
    await _ensure_column(conn, "curated_stacks", "description_ru", "TEXT")
    await _ensure_column(conn, "curated_stacks", "source_url", "TEXT")
    await _ensure_column(conn, "curated_stacks", "tags_json", "TEXT")
    await _ensure_column(conn, "curated_stacks", "is_active", "INTEGER NOT NULL DEFAULT 1")
    await _ensure_column(conn, "curated_stacks", "created_at", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "curated_stacks", "updated_at", "TEXT NOT NULL DEFAULT ''")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_curated_stacks_active ON curated_stacks(is_active)")

async def _ensure_curated_stack_items(conn: aiosqlite.Connection) -> None:
    table = await fetchone(
        conn,
        "SELECT name FROM sqlite_master WHERE type='table' AND name='curated_stack_items'",
    )
    if not table:
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS curated_stack_items (
              id INTEGER PRIMARY KEY,
              stack_id INTEGER NOT NULL REFERENCES curated_stacks(id) ON DELETE CASCADE,
              supplement_slug TEXT,
              supplement_name TEXT,
              dose_ru TEXT,
              timing_ru TEXT,
              notes_ru TEXT,
              sort_order INTEGER NOT NULL DEFAULT 0
            )"""
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_curated_stack_items_stack_sort ON curated_stack_items(stack_id, sort_order)"
        )
        return

    await _ensure_column(conn, "curated_stack_items", "stack_id", "INTEGER")
    await _ensure_column(conn, "curated_stack_items", "supplement_slug", "TEXT")
    await _ensure_column(conn, "curated_stack_items", "supplement_name", "TEXT")
    await _ensure_column(conn, "curated_stack_items", "dose_ru", "TEXT")
    await _ensure_column(conn, "curated_stack_items", "timing_ru", "TEXT")
    await _ensure_column(conn, "curated_stack_items", "notes_ru", "TEXT")
    await _ensure_column(conn, "curated_stack_items", "sort_order", "INTEGER NOT NULL DEFAULT 0")
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_curated_stack_items_stack_sort ON curated_stack_items(stack_id, sort_order)"
    )

async def _backfill_user_supplements(conn: aiosqlite.Connection) -> None:
    cols = await fetchall(conn, "PRAGMA table_info(user_supplements)")
    col_names = {c["name"] for c in cols}
    if "custom_dose" in col_names:
        await conn.execute(
            """UPDATE user_supplements
               SET dose_text=custom_dose
               WHERE (dose_text IS NULL OR dose_text='') AND custom_dose IS NOT NULL AND custom_dose!=''"""
        )
    if "notes" in col_names:
        await conn.execute(
            """UPDATE user_supplements
               SET note_text=notes
               WHERE (note_text IS NULL OR note_text='') AND notes IS NOT NULL AND notes!=''"""
        )
    if "active" in col_names:
        await conn.execute(
            """UPDATE user_supplements
               SET is_active=active
               WHERE (is_active IS NULL OR is_active='') AND active IS NOT NULL"""
        )
    await conn.execute(
        "UPDATE user_supplements SET created_at=? WHERE created_at IS NULL OR created_at=''",
        (utc_now_iso(),),
    )

async def _ensure_column(conn: aiosqlite.Connection, table: str, column: str, column_type: str) -> None:
    cols = await fetchall(conn, f"PRAGMA table_info({table})")
    if any(c["name"] == column for c in cols):
        return
    await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")

async def _backfill_alias_raw(conn: aiosqlite.Connection) -> None:
    cols = await fetchall(conn, "PRAGMA table_info(supplement_aliases)")
    col_names = {c["name"] for c in cols}
    if "alias_raw" not in col_names or "alias" not in col_names:
        return
    rows = await fetchall(
        conn,
        "SELECT id, alias, alias_raw FROM supplement_aliases WHERE alias_raw IS NULL OR alias_raw=''",
    )
    for row in rows:
        alias_raw = row["alias"] or ""
        await conn.execute("UPDATE supplement_aliases SET alias_raw=? WHERE id=?", (alias_raw, int(row["id"])))

async def _backfill_alias_norm(conn: aiosqlite.Connection) -> None:
    cols = await fetchall(conn, "PRAGMA table_info(supplement_aliases)")
    col_names = {c["name"] for c in cols}
    alias_col = "alias_raw" if "alias_raw" in col_names else "alias"
    rows = await fetchall(
        conn,
        f"SELECT id, {alias_col} AS alias_value FROM supplement_aliases WHERE alias_norm IS NULL OR alias_norm=''",
    )
    for row in rows:
        alias_norm = normalize(row["alias_value"])
        await conn.execute("UPDATE supplement_aliases SET alias_norm=? WHERE id=?", (alias_norm, int(row["id"])))

async def _backfill_supplement_alias_norm(conn: aiosqlite.Connection) -> None:
    rows = await fetchall(conn, "SELECT id, slug, name_ru, name_en, aliases FROM supplements")
    for row in rows:
        alias_norm = await _build_supplement_alias_norm(conn, row)
        await conn.execute(
            "UPDATE supplements SET alias_norm=? WHERE id=?",
            (alias_norm, int(row["id"])),
        )

async def _backfill_supplement_evidence(conn: aiosqlite.Connection) -> None:
    rows = await fetchall(
        conn,
        """SELECT id, evidence_json, payload_json, human_trials_count_est,
                  participants_total_est, avg_effect_percent_est, confidence
           FROM supplements""",
    )
    for row in rows:
        if (
            str(row["human_trials_count_est"] or "").strip()
            or str(row["participants_total_est"] or "").strip()
            or str(row["avg_effect_percent_est"] or "").strip()
            or str(row["confidence"] or "").strip()
        ):
            continue
        evidence = _extract_evidence_fields(row["evidence_json"], row["payload_json"])
        await conn.execute(
            """UPDATE supplements
               SET human_trials_count_est=?, participants_total_est=?,
                   avg_effect_percent_est=?, confidence=?
               WHERE id=?""",
            (
                evidence["human_trials_count_est"],
                evidence["participants_total_est"],
                evidence["avg_effect_percent_est"],
                evidence["confidence"],
                int(row["id"]),
            ),
        )

# users
async def get_or_create_user(conn: aiosqlite.Connection, tg_user_id: int, tz_name: str | None = "") -> int:
    row = await fetchone(conn, "SELECT id FROM users WHERE tg_user_id=?", (tg_user_id,))
    if row:
        return int(row["id"])
    tz_value = tz_name or ""
    await conn.execute(
        "INSERT INTO users(tg_user_id, tz_name, created_at) VALUES(?,?,?)",
        (tg_user_id, tz_value, utc_now_iso()),
    )
    await conn.commit()
    row2 = await fetchone(conn, "SELECT id FROM users WHERE tg_user_id=?", (tg_user_id,))
    return int(row2["id"])

async def get_user_tz(conn: aiosqlite.Connection, tg_user_id: int) -> str | None:
    row = await fetchone(conn, "SELECT tz_name FROM users WHERE tg_user_id=?", (tg_user_id,))
    if not row:
        return None
    tz_name = str(row["tz_name"] or "").strip()
    return tz_name or None

async def set_user_tz(conn: aiosqlite.Connection, tg_user_id: int, tz_name: str) -> None:
    await conn.execute(
        "UPDATE users SET tz_name=? WHERE tg_user_id=?",
        (tz_name, tg_user_id),
    )
    await conn.commit()

async def set_user_last_view(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    view: str,
    payload: str = "",
) -> None:
    await get_or_create_user(conn, tg_user_id)
    await conn.execute(
        "UPDATE users SET last_view=?, last_view_payload=? WHERE tg_user_id=?",
        (view, payload, tg_user_id),
    )
    await conn.commit()

async def get_user_last_view(conn: aiosqlite.Connection, tg_user_id: int) -> tuple[str, str]:
    row = await fetchone(conn, "SELECT last_view, last_view_payload FROM users WHERE tg_user_id=?", (tg_user_id,))
    if not row:
        return ("menu", "")
    view = str(row["last_view"] or "menu")
    payload = str(row["last_view_payload"] or "")
    return (view, payload)

async def set_user_last_msg(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    chat_id: int,
    message_id: int,
) -> None:
    await get_or_create_user(conn, tg_user_id)
    await conn.execute(
        "UPDATE users SET last_chat_id=?, last_message_id=? WHERE tg_user_id=?",
        (int(chat_id), int(message_id), tg_user_id),
    )
    await conn.commit()

async def get_user_last_msg(conn: aiosqlite.Connection, tg_user_id: int) -> tuple[int, int]:
    row = await fetchone(conn, "SELECT last_chat_id, last_message_id FROM users WHERE tg_user_id=?", (tg_user_id,))
    if not row:
        return (0, 0)
    return (int(row["last_chat_id"] or 0), int(row["last_message_id"] or 0))

def parse_offset_tz(tz_name: str | None):
    if not tz_name:
        return timezone.utc
    if re.fullmatch(r"[+-](?:0\\d|1\\d|2[0-3]):[0-5]\\d", tz_name):
        sign = 1 if tz_name[0] == "+" else -1
        hours_str, minutes_str = tz_name[1:].split(":")
        offset = timedelta(hours=int(hours_str), minutes=int(minutes_str))
        return timezone(sign * offset)
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return timezone.utc

def local_now(tz_name: str | None) -> datetime:
    return datetime.now(parse_offset_tz(tz_name))

def local_date(tz_name: str | None) -> str:
    return local_now(tz_name).date().isoformat()

def next_run_datetime_for_hhmm(hhmm: str, tz_name: str | None) -> datetime:
    tzinfo = parse_offset_tz(tz_name)
    now_local = datetime.now(tzinfo)
    hour, minute = map(int, hhmm.split(":"))
    candidate = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now_local:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)

def get_user_local_date(tz_name: str | None) -> str:
    return local_date(tz_name)

async def get_user_local_day(conn: aiosqlite.Connection, user_id: int) -> str:
    row = await fetchone(conn, "SELECT tz_name FROM users WHERE id=?", (user_id,))
    tz_name = str(row["tz_name"] or "").strip() if row else ""
    return local_date(tz_name or None)

# catalog
def _extract_evidence_fields(evidence_json: str | None, payload_json: str | None) -> dict[str, str]:
    data: dict = {}
    if evidence_json:
        try:
            parsed = json.loads(evidence_json)
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            data.update(parsed)
    if payload_json:
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict):
            evidence = payload.get("evidence")
            if isinstance(evidence, dict):
                data.update(evidence)
    def _string(value: object) -> str:
        return str(value).strip() if value is not None else ""
    return {
        "human_trials_count_est": _string(data.get("human_trials_count_est")),
        "participants_total_est": _string(data.get("participants_total_est")),
        "avg_effect_percent_est": _string(data.get("avg_effect_percent_est")),
        "confidence": _string(data.get("confidence")),
    }

async def _build_supplement_alias_norm(conn: aiosqlite.Connection, row) -> str:
    parts: list[str] = []
    for field in ("name_ru", "name_en", "slug"):
        value = str(row[field] or "").strip() if field in row.keys() else ""
        if value:
            parts.append(value)
    aliases_raw = str(row["aliases"] or "").strip() if "aliases" in row.keys() else ""
    if aliases_raw:
        try:
            aliases = json.loads(aliases_raw)
        except json.JSONDecodeError:
            aliases = [item.strip() for item in aliases_raw.split(",")]
        if isinstance(aliases, (list, tuple)):
            parts.extend([str(item) for item in aliases if item])
    alias_rows = await fetchall(
        conn,
        "SELECT alias_raw FROM supplement_aliases WHERE supplement_id=?",
        (int(row["id"]),),
    )
    parts.extend([str(item["alias_raw"] or "") for item in alias_rows])
    normalized = [normalize(part) for part in parts if part]
    return " ".join([part for part in normalized if part]).strip()

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
    aliases_raw = s.get("aliases") or s.get("aliases_json") or ""
    if isinstance(aliases_raw, (list, tuple)):
        aliases_raw = json.dumps(list(aliases_raw), ensure_ascii=False)
    tags_raw = s.get("tags") or ""
    if isinstance(tags_raw, (list, tuple)):
        tags_raw = " ".join([str(item) for item in tags_raw if item])
    evidence_fields = _extract_evidence_fields(s.get("evidence_json"), s.get("payload_json"))
    await conn.execute(
        """INSERT INTO supplements(
          slug,name,name_ru,name_en,aliases,alias_norm,human_trials_count_est,participants_total_est,
          avg_effect_percent_est,confidence,tags,goals,rules_tags,summary_ru,summary_verified,
          summary_theoretical,molecular_mechanisms,evidence_json,dose_ru,timing_ru,safety_ru,purchase_url,
          tags_json,mechanisms,evidence_grade,safety_notes,interaction_classes,interaction_classes_json,
          payload_json,is_active
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(slug) DO UPDATE SET
          name=excluded.name,
          name_ru=excluded.name_ru,
          name_en=excluded.name_en,
          aliases=excluded.aliases,
          alias_norm=excluded.alias_norm,
          human_trials_count_est=excluded.human_trials_count_est,
          participants_total_est=excluded.participants_total_est,
          avg_effect_percent_est=excluded.avg_effect_percent_est,
          confidence=excluded.confidence,
          tags=excluded.tags,
          goals=excluded.goals,
          rules_tags=excluded.rules_tags,
          summary_ru=excluded.summary_ru,
          summary_verified=excluded.summary_verified,
          summary_theoretical=excluded.summary_theoretical,
          molecular_mechanisms=excluded.molecular_mechanisms,
          evidence_json=excluded.evidence_json,
          dose_ru=excluded.dose_ru,
          timing_ru=excluded.timing_ru,
          safety_ru=excluded.safety_ru,
          purchase_url=excluded.purchase_url,
          tags_json=excluded.tags_json,
          mechanisms=excluded.mechanisms,
          evidence_grade=excluded.evidence_grade,
          safety_notes=excluded.safety_notes,
          interaction_classes=excluded.interaction_classes,
          interaction_classes_json=excluded.interaction_classes_json,
          payload_json=excluded.payload_json,
          is_active=excluded.is_active
        """,
        (
            s["slug"],
            display_name,
            name_ru,
            name_en,
            aliases_raw,
            s.get("alias_norm") or "",
            s.get("human_trials_count_est") or evidence_fields["human_trials_count_est"],
            s.get("participants_total_est") or evidence_fields["participants_total_est"],
            s.get("avg_effect_percent_est") or evidence_fields["avg_effect_percent_est"],
            s.get("confidence") or evidence_fields["confidence"],
            tags_raw,
            s.get("goals") or "",
            s.get("rules_tags") or "",
            s.get("summary_ru"),
            s.get("summary_verified"),
            s.get("summary_theoretical"),
            s.get("molecular_mechanisms"),
            s.get("evidence_json"),
            s.get("dose_ru"),
            s.get("timing_ru"),
            s.get("safety_ru"),
            s.get("purchase_url"),
            s.get("tags_json"),
            s.get("mechanisms"),
            s.get("evidence_grade"),
            s.get("safety_notes"),
            s.get("interaction_classes", "[]"),
            s.get("interaction_classes_json", s.get("interaction_classes", "[]")),
            s.get("payload_json"),
            int(s.get("is_active", 1)),
        ),
    )
    await conn.commit()
    row = await fetchone(conn, "SELECT * FROM supplements WHERE slug=?", (s["slug"],))
    alias_norm = s.get("alias_norm")
    if not alias_norm:
        alias_norm = await _build_supplement_alias_norm(conn, row)
        await conn.execute("UPDATE supplements SET alias_norm=? WHERE id=?", (alias_norm, int(row["id"])))
        await conn.commit()
    return int(row["id"])

async def get_supplement_by_slug(conn: aiosqlite.Connection, slug: str):
    return await fetchone(conn, "SELECT * FROM supplements WHERE slug=?", (slug,))

async def get_supplement_by_id(conn: aiosqlite.Connection, sid: int):
    return await fetchone(conn, "SELECT * FROM supplements WHERE id=?", (sid,))

async def list_supplements_by_ids(conn: aiosqlite.Connection, ids: list[int]) -> list[aiosqlite.Row]:
    if not ids:
        return []
    cleaned = [int(i) for i in ids]
    placeholders = ",".join("?" for _ in cleaned)
    rows = await fetchall(conn, f"SELECT * FROM supplements WHERE id IN ({placeholders})", tuple(cleaned))
    rows_by_id = {int(r["id"]): r for r in rows}
    return [rows_by_id[i] for i in cleaned if i in rows_by_id]

def _relation_to_kind(relation: str | None) -> str | None:
    rel = (relation or "").strip().lower()
    if rel in {"synergy"}:
        return "synergy"
    if rel in {"avoid", "antagonism"}:
        return "antagonism"
    if rel in {"caution"}:
        return "caution"
    return None

def _severity_to_strength(severity: int | None) -> int | None:
    if severity is None:
        return None
    try:
        value = int(severity)
    except (TypeError, ValueError):
        return None
    if 0 <= value <= 4:
        return min(100, value * 25)
    if 0 <= value <= 100:
        return value
    return None

async def _resolve_supplement_id(conn: aiosqlite.Connection, slug: str | None, name: str | None, raw_id) -> int | None:
    if raw_id is not None:
        try:
            return int(raw_id)
        except (TypeError, ValueError):
            pass
    if slug:
        row = await get_supplement_by_slug(conn, slug)
        if row:
            return int(row["id"])
    if name:
        row = await find_supplement_by_slug_or_name(conn, None, name)
        if row:
            return int(row["id"])
    return None

async def get_interactions_for_supplement(conn: aiosqlite.Connection, sid: int) -> list[dict]:
    interactions: list[dict] = []
    seen: set[tuple[int, str, str]] = set()
    rows = await fetchall(
        conn,
        """SELECT * FROM interactions
           WHERE a_id=? OR b_id=?
           ORDER BY severity DESC, id DESC""",
        (sid, sid),
    )
    for row in rows:
        a_id = int(row["a_id"])
        b_id = int(row["b_id"])
        other_id = b_id if a_id == sid else a_id
        kind = _relation_to_kind(row["relation"])
        if not kind:
            continue
        reason = (row["note"] or "").strip()
        strength = _severity_to_strength(row["severity"])
        key = (other_id, kind, reason)
        if key in seen:
            continue
        seen.add(key)
        interactions.append(
            {
                "other_supplement_id": other_id,
                "kind": kind,
                "reason_ru": reason,
                "strength": strength,
            }
        )

    supp_row = await get_supplement_by_id(conn, sid)
    payload_raw = (supp_row["payload_json"] if supp_row and "payload_json" in supp_row.keys() else None) or ""
    if payload_raw:
        try:
            payload = json.loads(payload_raw)
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict):
            for item in payload.get("explicit_interactions") or []:
                if not isinstance(item, dict):
                    continue
                kind = _relation_to_kind(item.get("relation"))
                if not kind:
                    continue
                other_id = await _resolve_supplement_id(
                    conn,
                    item.get("other_slug"),
                    item.get("other_name") or item.get("other_title"),
                    item.get("other_id"),
                )
                if not other_id:
                    continue
                reason = (item.get("note_ru") or item.get("note") or "").strip()
                strength = _severity_to_strength(item.get("severity"))
                key = (other_id, kind, reason)
                if key in seen:
                    continue
                seen.add(key)
                interactions.append(
                    {
                        "other_supplement_id": int(other_id),
                        "kind": kind,
                        "reason_ru": reason,
                        "strength": strength,
                    }
                )

    return interactions

async def search_supplements(
    conn: aiosqlite.Connection,
    q: str,
    limit: int = 10,
    offset: int = 0,
):
    if not q or not q.strip():
        return []
    q_norm = normalize(q)
    q_like = f"%{q_norm}%"
    return await fetchall(
        conn,
        """SELECT *
           FROM supplements
           WHERE is_active=1 AND alias_norm LIKE ?
           ORDER BY human_trials_count_est DESC, participants_total_est DESC, name_ru COLLATE NOCASE
           LIMIT ? OFFSET ?""",
        (q_like, limit, offset),
    )

async def count_search_supplements(conn: aiosqlite.Connection, q: str) -> int:
    if not q or not q.strip():
        return 0
    q_norm = normalize(q)
    q_like = f"%{q_norm}%"
    row = await fetchone(
        conn,
        "SELECT COUNT(*) AS total FROM supplements WHERE is_active=1 AND alias_norm LIKE ?",
        (q_like,),
    )
    return int(row["total"] or 0)

async def list_supplements_sorted(
    conn: aiosqlite.Connection,
    sort_key: str,
    limit: int,
    offset: int = 0,
):
    if sort_key == "alpha":
        order = "name_ru COLLATE NOCASE"
    elif sort_key == "effect":
        order = "avg_effect_percent_est DESC, name_ru COLLATE NOCASE"
    else:
        order = "human_trials_count_est DESC, participants_total_est DESC"
    return await fetchall(
        conn,
        f"SELECT * FROM supplements WHERE is_active=1 ORDER BY {order} LIMIT ? OFFSET ?",
        (limit, offset),
    )

async def list_supplements_by_class(
    conn: aiosqlite.Connection,
    class_key: str,
    limit: int,
    offset: int = 0,
):
    token = class_key.strip().lower()
    if not token:
        return []
    like_json = f'%"{token}"%'
    like_plain = f"%{token}%"
    return await fetchall(
        conn,
        """SELECT * FROM supplements
           WHERE is_active=1 AND (tags_json LIKE ? OR tags LIKE ? OR goals LIKE ? OR rules_tags LIKE ?)
           ORDER BY name_ru COLLATE NOCASE
           LIMIT ? OFFSET ?""",
        (like_json, like_plain, like_plain, like_plain, limit, offset),
    )

async def count_supplements(conn: aiosqlite.Connection) -> int:
    row = await fetchone(conn, "SELECT COUNT(*) AS total FROM supplements WHERE is_active=1")
    return int(row["total"] or 0)

async def count_supplements_by_class(conn: aiosqlite.Connection, class_key: str) -> int:
    token = class_key.strip().lower()
    if not token:
        return 0
    like_json = f'%"{token}"%'
    like_plain = f"%{token}%"
    row = await fetchone(
        conn,
        """SELECT COUNT(*) AS total
           FROM supplements
           WHERE is_active=1 AND (tags_json LIKE ? OR tags LIKE ? OR goals LIKE ? OR rules_tags LIKE ?)""",
        (like_json, like_plain, like_plain, like_plain),
    )
    return int(row["total"] or 0)

async def upsert_aliases(conn: aiosqlite.Connection, supplement_id: int, aliases: list[str]) -> None:
    if not aliases:
        return
    cols = await fetchall(conn, "PRAGMA table_info(supplement_aliases)")
    col_names = {c["name"] for c in cols}
    has_alias = "alias" in col_names
    has_alias_raw = "alias_raw" in col_names
    has_kind = "kind" in col_names
    for alias in aliases:
        alias_raw = str(alias).strip()
        if not alias_raw:
            continue
        alias_norm = normalize(alias_raw)
        if has_alias and has_alias_raw and has_kind:
            await conn.execute(
                """INSERT OR IGNORE INTO supplement_aliases(supplement_id, alias, alias_raw, alias_norm, kind)
                   VALUES(?,?,?,?,?)""",
                (supplement_id, alias_raw, alias_raw, alias_norm, None),
            )
        elif has_alias and has_alias_raw:
            await conn.execute(
                """INSERT OR IGNORE INTO supplement_aliases(supplement_id, alias, alias_raw, alias_norm)
                   VALUES(?,?,?,?)""",
                (supplement_id, alias_raw, alias_raw, alias_norm),
            )
        elif has_alias:
            await conn.execute(
                """INSERT OR IGNORE INTO supplement_aliases(supplement_id, alias, alias_norm)
                   VALUES(?,?,?)""",
                (supplement_id, alias_raw, alias_norm),
            )
        else:
            await conn.execute(
                """INSERT OR IGNORE INTO supplement_aliases(supplement_id, alias_raw, alias_norm)
                   VALUES(?,?,?)""",
                (supplement_id, alias_raw, alias_norm),
            )
    await conn.execute(
        "UPDATE supplements SET aliases=? WHERE id=?",
        (json.dumps(list({str(a).strip() for a in aliases if str(a).strip()}), ensure_ascii=False), supplement_id),
    )
    await conn.commit()
    row = await fetchone(conn, "SELECT * FROM supplements WHERE id=?", (supplement_id,))
    if row:
        alias_norm = await _build_supplement_alias_norm(conn, row)
        await conn.execute("UPDATE supplements SET alias_norm=? WHERE id=?", (alias_norm, supplement_id))
        await conn.commit()

async def list_supplements_page(conn: aiosqlite.Connection, offset: int, limit: int = 10):
    return await fetchall(
        conn,
        "SELECT * FROM supplements ORDER BY name LIMIT ? OFFSET ?",
        (limit, offset),
    )

# curated stacks
async def list_curated_stacks(conn: aiosqlite.Connection, limit: int, offset: int = 0):
    return await fetchall(
        conn,
        """SELECT * FROM curated_stacks
           WHERE is_active=1
           ORDER BY created_at DESC, title_ru
           LIMIT ? OFFSET ?""",
        (limit, offset),
    )

async def get_curated_stack(conn: aiosqlite.Connection, slug: str):
    return await fetchone(
        conn,
        "SELECT * FROM curated_stacks WHERE slug=? AND is_active=1",
        (slug,),
    )

async def list_curated_stack_items(conn: aiosqlite.Connection, stack_id: int):
    return await fetchall(
        conn,
        """SELECT * FROM curated_stack_items
           WHERE stack_id=?
           ORDER BY sort_order, id""",
        (stack_id,),
    )

async def find_supplement_by_slug_or_name(
    conn: aiosqlite.Connection,
    slug: str | None,
    name: str | None,
):
    if slug:
        row = await get_supplement_by_slug(conn, slug)
        if row:
            return row
    if not name:
        return None
    name = str(name).strip()
    if not name:
        return None
    norm = normalize(name)
    row = await fetchone(
        conn,
        """SELECT s.* FROM supplements s
           JOIN supplement_aliases a ON a.supplement_id=s.id
           WHERE a.alias_norm=?
           LIMIT 1""",
        (norm,),
    )
    if row:
        return row
    row = await fetchone(
        conn,
        """SELECT * FROM supplements
           WHERE lower(name)=lower(?) OR lower(name_ru)=lower(?) OR lower(name_en)=lower(?)
           LIMIT 1""",
        (name, name, name),
    )
    if row:
        return row
    rows = await search_supplements(conn, name, limit=1)
    return rows[0] if rows else None

async def add_stack_items_to_user(conn: aiosqlite.Connection, user_id: int, items: list[dict]) -> dict:
    added = 0
    updated = 0
    for item in items:
        supplement_id = int(item["supplement_id"])
        existing = await fetchone(
            conn,
            "SELECT note_text FROM user_supplements WHERE user_id=? AND supplement_id=?",
            (user_id, supplement_id),
        )
        if existing:
            updated += 1
        else:
            added += 1

        await conn.execute(
            """INSERT INTO user_supplements(user_id, supplement_id, is_active, created_at)
               VALUES(?,?,1,?)
               ON CONFLICT(user_id, supplement_id) DO UPDATE SET
                 is_active=1""",
            (user_id, supplement_id, utc_now_iso()),
        )

        dose_text = str(item.get("dose_ru") or "").strip()
        timing_text = str(item.get("timing_ru") or "").strip()
        notes_text = str(item.get("notes_ru") or "").strip()
        if dose_text:
            await conn.execute(
                """UPDATE user_supplements
                   SET dose_text=?
                   WHERE user_id=? AND supplement_id=? AND (dose_text IS NULL OR dose_text='')""",
                (dose_text, user_id, supplement_id),
            )
        note_bits = []
        if dose_text:
            note_bits.append(f"Доза: {dose_text}")
        if timing_text:
            note_bits.append(f"Время: {timing_text}")
        if notes_text:
            note_bits.append(f"Примечание: {notes_text}")
        if note_bits:
            addition = "\n".join(note_bits)
            if existing and existing["note_text"]:
                combined = f"{str(existing['note_text']).rstrip()}\n\n{addition}"
            else:
                combined = addition
            await conn.execute(
                "UPDATE user_supplements SET note_text=? WHERE user_id=? AND supplement_id=?",
                (combined, user_id, supplement_id),
            )

    await conn.commit()
    return {"added": added, "updated": updated, "total": added + updated}

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

async def get_interaction_between(conn: aiosqlite.Connection, a_id: int, b_id: int):
    a, b = canon_pair(a_id, b_id)
    return await fetchone(conn, "SELECT * FROM interactions WHERE a_id=? AND b_id=?", (a, b))

# user library
async def add_user_supplement(conn: aiosqlite.Connection, tg_user_id: int, supplement_id: int) -> None:
    user_id = await get_or_create_user(conn, tg_user_id)
    await conn.execute(
        """INSERT INTO user_supplements(user_id, supplement_id, is_active, created_at)
           VALUES(?,?,1,?)
           ON CONFLICT(user_id, supplement_id) DO UPDATE SET
             is_active=1""",
        (user_id, supplement_id, utc_now_iso()),
    )
    await conn.execute(
        """INSERT OR IGNORE INTO user_supp_settings(
             user_id, supplement_id, dose_text, reminder_time, reminder_enabled,
             taken_date, skip_date, updated_at
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (user_id, supplement_id, "", "", 0, "", "", utc_now_iso()),
    )
    await conn.commit()

async def remove_user_supplement(conn: aiosqlite.Connection, tg_user_id: int, supplement_id: int) -> None:
    user_id = await get_or_create_user(conn, tg_user_id)
    await conn.execute("DELETE FROM user_supplements WHERE user_id=? AND supplement_id=?", (user_id, supplement_id))
    await conn.execute(
        "DELETE FROM user_supp_settings WHERE user_id=? AND supplement_id=?",
        (user_id, supplement_id),
    )
    await conn.commit()

async def get_user_supplement(conn: aiosqlite.Connection, tg_user_id: int, supplement_id: int):
    user_id = await get_or_create_user(conn, tg_user_id)
    return await fetchone(
        conn,
        """SELECT us.*, s.slug, s.name, s.name_ru, s.name_en,
                  s.summary_ru, s.summary_verified, s.summary_theoretical,
                  s.molecular_mechanisms, s.mechanisms
           FROM user_supplements us
           JOIN supplements s ON s.id=us.supplement_id
           WHERE us.user_id=? AND us.supplement_id=?""",
        (user_id, supplement_id),
    )

async def get_user_supplement_with_status(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    supplement_id: int,
    local_date: str | None,
):
    user_id = await get_or_create_user(conn, tg_user_id)
    params: list = [local_date or ""]
    status_join = (
        "LEFT JOIN user_intake_log uil ON uil.user_id=us.user_id "
        "AND uil.supplement_id=us.supplement_id AND uil.local_date=?"
    )
    params.extend([user_id, supplement_id])
    return await fetchone(
        conn,
        f"""SELECT us.*, s.slug, s.name, s.name_ru, s.name_en,
                   s.summary_ru, s.summary_verified, s.summary_theoretical,
                   s.molecular_mechanisms, s.mechanisms,
                   sch.time_hhmm, sch.is_enabled,
                   uil.status AS today_status
            FROM user_supplements us
            JOIN supplements s ON s.id=us.supplement_id
            LEFT JOIN user_schedules sch ON sch.user_id=us.user_id AND sch.supplement_id=us.supplement_id
            {status_join}
            WHERE us.user_id=? AND us.supplement_id=?""",
        tuple(params),
    )

async def list_user_supplements_page(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    q: str | None = None,
    page: int = 0,
    page_size: int = 10,
    local_date: str | None = None,
):
    user_id = await get_or_create_user(conn, tg_user_id)
    page = max(page, 0)
    offset = page * page_size
    q = (q or "").strip()
    count_params = [user_id]
    params: list = [local_date or "", user_id]
    filters = ""
    if q:
        filters = " AND (s.name LIKE ? OR s.slug LIKE ? OR s.name_ru LIKE ? OR s.name_en LIKE ?)"
        q_like = f"%{q}%"
        count_params.extend([q_like, q_like, q_like, q_like])
        params.extend([q_like, q_like, q_like, q_like])

    count_row = await fetchone(
        conn,
        f"""SELECT COUNT(*) AS total
            FROM user_supplements us
            JOIN supplements s ON s.id=us.supplement_id
            WHERE us.user_id=?{filters}""",
        tuple(count_params),
    )
    total = int(count_row["total"]) if count_row else 0

    params.extend([page_size, offset])
    status_join = (
        "LEFT JOIN user_intake_log uil ON uil.user_id=us.user_id "
        "AND uil.supplement_id=us.supplement_id AND uil.local_date=?"
    )
    rows = await fetchall(
        conn,
        f"""SELECT us.*, s.id AS supplement_id, s.slug, s.name, s.name_ru, s.name_en,
                  s.summary_ru, s.summary_verified, s.summary_theoretical,
                  s.molecular_mechanisms, s.mechanisms,
                  uil.status AS today_status
           FROM user_supplements us
           JOIN supplements s ON s.id=us.supplement_id
           {status_join}
           WHERE us.user_id=?{filters}
           ORDER BY s.name
           LIMIT ? OFFSET ?""",
        tuple(params),
    )
    return rows, total

async def set_user_supplement_dose(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    supplement_id: int,
    dose_text: str | None,
) -> None:
    user_id = await get_or_create_user(conn, tg_user_id)
    await conn.execute(
        "UPDATE user_supplements SET dose_text=? WHERE user_id=? AND supplement_id=?",
        (dose_text, user_id, supplement_id),
    )
    await conn.commit()

async def set_user_supplement_note(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    supplement_id: int,
    note_text: str | None,
) -> None:
    user_id = await get_or_create_user(conn, tg_user_id)
    await conn.execute(
        "UPDATE user_supplements SET note_text=? WHERE user_id=? AND supplement_id=?",
        (note_text, user_id, supplement_id),
    )
    await conn.commit()

async def toggle_user_supplement_active(conn: aiosqlite.Connection, tg_user_id: int, supplement_id: int) -> None:
    user_id = await get_or_create_user(conn, tg_user_id)
    await conn.execute(
        """UPDATE user_supplements
           SET is_active=CASE WHEN is_active=1 THEN 0 ELSE 1 END
           WHERE user_id=? AND supplement_id=?""",
        (user_id, supplement_id),
    )
    await conn.commit()

async def list_user_supplements(conn: aiosqlite.Connection, tg_user_id: int, limit: int = 50):
    user_id = await get_or_create_user(conn, tg_user_id)
    return await fetchall(
        conn,
        """SELECT s.* FROM user_supplements us
           JOIN supplements s ON s.id=us.supplement_id
           WHERE us.user_id=? AND us.is_active=1
           ORDER BY s.name LIMIT ?""",
        (user_id, limit),
    )

async def list_user_stack_details(conn: aiosqlite.Connection, tg_user_id: int, local_date: str | None = None):
    user_id = await get_or_create_user(conn, tg_user_id)
    params: list = [local_date or "", user_id]
    status_join = (
        "LEFT JOIN user_intake_log uil ON uil.user_id=us.user_id "
        "AND uil.supplement_id=us.supplement_id AND uil.local_date=?"
    )
    return await fetchall(
        conn,
        f"""SELECT us.supplement_id, us.dose_text, us.note_text, us.is_active,
                   s.name, s.slug,
                   sch.time_hhmm,
                   uil.status AS today_status
            FROM user_supplements us
            JOIN supplements s ON s.id=us.supplement_id
            LEFT JOIN user_schedules sch ON sch.user_id=us.user_id AND sch.supplement_id=us.supplement_id
            {status_join}
            WHERE us.user_id=? AND us.is_active=1
            ORDER BY s.name""",
        tuple(params),
    )

async def user_has_supplement(conn: aiosqlite.Connection, tg_user_id: int, supplement_id: int) -> bool:
    user_id = await get_or_create_user(conn, tg_user_id)
    row = await fetchone(
        conn,
        "SELECT 1 FROM user_supplements WHERE user_id=? AND supplement_id=?",
        (user_id, supplement_id),
    )
    return row is not None

async def get_supplement_name(conn: aiosqlite.Connection, supplement_id: int) -> str | None:
    row = await fetchone(conn, "SELECT name FROM supplements WHERE id=?", (supplement_id,))
    return str(row["name"]) if row else None

async def get_user_schedule(conn: aiosqlite.Connection, tg_user_id: int, supplement_id: int):
    user_id = await get_or_create_user(conn, tg_user_id)
    return await fetchone(
        conn,
        "SELECT * FROM user_schedules WHERE user_id=? AND supplement_id=?",
        (user_id, supplement_id),
    )

async def list_user_schedules(conn: aiosqlite.Connection, tg_user_id: int):
    user_id = await get_or_create_user(conn, tg_user_id)
    return await fetchall(
        conn,
        "SELECT * FROM user_schedules WHERE user_id=?",
        (user_id,),
    )

async def list_enabled_schedules(conn: aiosqlite.Connection):
    return await fetchall(
        conn,
        """SELECT u.tg_user_id, u.tz_name, sch.supplement_id, sch.time_hhmm
           FROM user_schedules sch
           JOIN users u ON u.id=sch.user_id
           WHERE sch.is_enabled=1 AND sch.time_hhmm IS NOT NULL AND sch.time_hhmm!=''
             AND u.tz_name IS NOT NULL AND u.tz_name!=''""",
    )

async def list_enabled_reminders(conn: aiosqlite.Connection):
    return await fetchall(
        conn,
        """SELECT u.tg_user_id, u.tz_name, uss.supplement_id, uss.reminder_time
           FROM user_supp_settings uss
           JOIN users u ON u.id=uss.user_id
           WHERE uss.reminder_enabled=1 AND uss.reminder_time IS NOT NULL AND uss.reminder_time!=''
             AND u.tz_name IS NOT NULL AND u.tz_name!=''""",
    )

async def set_user_schedule_time(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    supplement_id: int,
    time_hhmm: str | None,
) -> None:
    user_id = await get_or_create_user(conn, tg_user_id)
    await conn.execute(
        """INSERT INTO user_schedules(user_id, supplement_id, time_hhmm, is_enabled)
           VALUES(?,?,?,?)
           ON CONFLICT(user_id, supplement_id) DO UPDATE SET
             time_hhmm=excluded.time_hhmm,
             is_enabled=excluded.is_enabled""",
        (user_id, supplement_id, time_hhmm, 1 if time_hhmm else 0),
    )
    await conn.commit()

async def get_today_intake_status(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    supplement_id: int,
    local_date: str,
) -> str | None:
    user_id = await get_or_create_user(conn, tg_user_id)
    row = await fetchone(
        conn,
        """SELECT status FROM user_intake_log
           WHERE user_id=? AND supplement_id=? AND local_date=?""",
        (user_id, supplement_id, local_date),
    )
    return str(row["status"]) if row else None

async def set_today_intake_status(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    supplement_id: int,
    local_date: str,
    status: str | None,
) -> None:
    user_id = await get_or_create_user(conn, tg_user_id)
    if status is None:
        await conn.execute(
            "DELETE FROM user_intake_log WHERE user_id=? AND supplement_id=? AND local_date=?",
            (user_id, supplement_id, local_date),
        )
        await conn.commit()
        return
    await conn.execute(
        """INSERT INTO user_intake_log(user_id, supplement_id, local_date, status, updated_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(user_id, supplement_id, local_date) DO UPDATE SET
             status=excluded.status,
             updated_at=excluded.updated_at""",
        (user_id, supplement_id, local_date, status, utc_now_iso()),
    )
    await conn.commit()

async def get_user_supp_settings(conn: aiosqlite.Connection, user_id: int, supplement_id: int) -> dict:
    row = await fetchone(
        conn,
        """SELECT dose_text, reminder_time, reminder_enabled, taken_date, skip_date
           FROM user_supp_settings
           WHERE user_id=? AND supplement_id=?""",
        (user_id, supplement_id),
    )
    defaults = {
        "dose_text": "",
        "reminder_time": "",
        "reminder_enabled": 0,
        "taken_date": "",
        "skip_date": "",
    }
    if not row:
        return defaults
    return {
        "dose_text": str(row["dose_text"] or ""),
        "reminder_time": str(row["reminder_time"] or ""),
        "reminder_enabled": int(row["reminder_enabled"] or 0),
        "taken_date": str(row["taken_date"] or ""),
        "skip_date": str(row["skip_date"] or ""),
    }

async def upsert_user_supp_settings(
    conn: aiosqlite.Connection,
    user_id: int,
    supplement_id: int,
    patch: dict,
) -> None:
    text_fields = {"dose_text", "reminder_time", "taken_date", "skip_date"}
    int_fields = {"reminder_enabled"}
    updates: dict[str, object] = {}
    for key, value in (patch or {}).items():
        if key in text_fields:
            updates[key] = "" if value is None else str(value)
        elif key in int_fields:
            updates[key] = 1 if bool(value) else 0

    await conn.execute(
        """INSERT OR IGNORE INTO user_supp_settings(user_id, supplement_id, updated_at)
           VALUES(?,?,?)""",
        (user_id, supplement_id, utc_now_iso()),
    )
    update_cols = ["updated_at=?"] + [f"{key}=?" for key in updates.keys()]
    params = [utc_now_iso(), *updates.values(), user_id, supplement_id]
    await conn.execute(
        f"""UPDATE user_supp_settings
            SET {", ".join(update_cols)}
            WHERE user_id=? AND supplement_id=?""",
        tuple(params),
    )
    await conn.commit()

async def set_user_supp_dose(
    conn: aiosqlite.Connection,
    user_id: int,
    supplement_id: int,
    dose_text: str | None,
) -> None:
    await upsert_user_supp_settings(
        conn,
        user_id,
        supplement_id,
        {"dose_text": dose_text},
    )

async def set_user_supp_reminder_time(
    conn: aiosqlite.Connection,
    user_id: int,
    supplement_id: int,
    reminder_time: str | None,
) -> None:
    await upsert_user_supp_settings(
        conn,
        user_id,
        supplement_id,
        {"reminder_time": reminder_time},
    )

async def set_user_supp_reminder_enabled(
    conn: aiosqlite.Connection,
    user_id: int,
    supplement_id: int,
    reminder_enabled: bool | int,
) -> None:
    await upsert_user_supp_settings(
        conn,
        user_id,
        supplement_id,
        {"reminder_enabled": reminder_enabled},
    )

async def set_user_supp_taken_date(
    conn: aiosqlite.Connection,
    user_id: int,
    supplement_id: int,
    taken_date: str | None,
) -> None:
    await upsert_user_supp_settings(
        conn,
        user_id,
        supplement_id,
        {"taken_date": taken_date},
    )

async def set_user_supp_skip_date(
    conn: aiosqlite.Connection,
    user_id: int,
    supplement_id: int,
    skip_date: str | None,
) -> None:
    await upsert_user_supp_settings(
        conn,
        user_id,
        supplement_id,
        {"skip_date": skip_date},
    )

async def delete_user_supp_settings(
    conn: aiosqlite.Connection,
    user_id: int,
    supplement_id: int,
) -> None:
    await conn.execute(
        "DELETE FROM user_supp_settings WHERE user_id=? AND supplement_id=?",
        (user_id, supplement_id),
    )
    await conn.commit()

# checkins
async def save_daily_checkin(conn: aiosqlite.Connection, user_id: int, day: str, payload: dict) -> None:
    await conn.execute(
        """INSERT INTO daily_checkins(user_id, day, created_at, payload_json)
           VALUES(?,?,?,?)""",
        (user_id, day, utc_now_iso(), json.dumps(payload, ensure_ascii=False)),
    )
    await conn.commit()

async def list_recent_checkins(conn: aiosqlite.Connection, user_id: int, limit: int = 3):
    return await fetchall(
        conn,
        "SELECT * FROM daily_checkins WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    )

async def upsert_daily_plan_override(conn: aiosqlite.Connection, user_id: int, day: str, override: dict) -> None:
    await conn.execute(
        """INSERT INTO daily_plan_overrides(user_id, day, override_json, created_at)
           VALUES(?,?,?,?)
           ON CONFLICT(user_id, day) DO UPDATE SET
             override_json=excluded.override_json,
             created_at=excluded.created_at""",
        (user_id, day, json.dumps(override, ensure_ascii=False), utc_now_iso()),
    )
    await conn.commit()

async def get_daily_plan_override(conn: aiosqlite.Connection, user_id: int, day: str) -> dict | None:
    row = await fetchone(
        conn,
        "SELECT override_json FROM daily_plan_overrides WHERE user_id=? AND day=?",
        (user_id, day),
    )
    if not row:
        return None
    raw = row["override_json"]
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None

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
