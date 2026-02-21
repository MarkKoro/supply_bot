from __future__ import annotations
from datetime import date, datetime, timedelta, timezone
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

def normalize_text(text: str | None) -> str:
    if not text:
        return ""
    value = str(text).strip().lower().replace("ё", "е")
    value = re.sub(r"[\s\-_. ,]+", "", value)
    return value

# --- Compat helpers: work on all `aiosqlite` versions ---
async def fetchone(conn: aiosqlite.Connection, sql: str, params: tuple = ()):
    async with conn.execute(sql, params) as cur:
        return await cur.fetchone()

async def fetchall(conn: aiosqlite.Connection, sql: str, params: tuple = ()): 
    async with conn.execute(sql, params) as cur:
        return await cur.fetchall()


async def _table_exists(conn: aiosqlite.Connection, name: str) -> bool:
    row = await fetchone(
        conn,
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    )
    return row is not None


async def _table_columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    try:
        rows = await fetchall(conn, f"PRAGMA table_info({table})")
    except Exception:
        return set()
    return {str(row["name"]).strip().lower() for row in rows if row and row["name"]}


async def _get_date_col(conn: aiosqlite.Connection, table: str) -> str | None:
    cols = await _table_columns(conn, table)
    for candidate in ("date_local", "local_date", "date"):
        if candidate in cols:
            return candidate
    return None

SCHEMA_SQL = """PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  tg_user_id    INTEGER UNIQUE NOT NULL,
  tz_name       TEXT NOT NULL DEFAULT 'UTC',
  created_at    TEXT NOT NULL,
  last_local_date TEXT NOT NULL DEFAULT '',
  last_view     TEXT NOT NULL DEFAULT 'menu',
  last_view_payload TEXT NOT NULL DEFAULT '',
  last_chat_id  INTEGER NOT NULL DEFAULT 0,
  last_message_id INTEGER NOT NULL DEFAULT 0,
  initial_stack_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS supplements (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  slug          TEXT UNIQUE NOT NULL,
  name          TEXT NOT NULL,
  name_ru       TEXT,
  name_en       TEXT,
  aliases       TEXT DEFAULT '',
  aliases_json  TEXT NOT NULL DEFAULT '[]',
  alias_norm    TEXT DEFAULT '',
  search_blob   TEXT NOT NULL DEFAULT '',
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
  supplement_type TEXT DEFAULT '',
  functional_groups TEXT DEFAULT '',
  functional_groups_json TEXT NOT NULL DEFAULT '[]',
  evidence_grade TEXT,
  body_effects_json TEXT NOT NULL DEFAULT '[]',
  body_targets TEXT DEFAULT '',
  body_targets_json TEXT NOT NULL DEFAULT '[]',
  body_burdens TEXT DEFAULT '',
  body_burdens_json TEXT NOT NULL DEFAULT '[]',
  safety_notes   TEXT,
  interaction_classes TEXT DEFAULT '[]',
  interaction_classes_json TEXT DEFAULT '[]',
  payload_json TEXT,
  is_active INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_supplements_name ON supplements(name);
CREATE INDEX IF NOT EXISTS idx_supplements_slug ON supplements(slug);
CREATE TABLE IF NOT EXISTS supplement_aliases (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  supplement_id INTEGER NOT NULL REFERENCES supplements(id) ON DELETE CASCADE,
  alias_raw     TEXT NOT NULL,
  alias_norm    TEXT
);
CREATE INDEX IF NOT EXISTS idx_alias_supp ON supplement_aliases(supplement_id);

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
  enabled       INTEGER NOT NULL DEFAULT 1,
  dose_text     TEXT,
  note_text     TEXT,
  notes         TEXT,
  created_at    TEXT NOT NULL,
  UNIQUE (user_id, supplement_id)
);
CREATE INDEX IF NOT EXISTS idx_user_supp_user ON user_supplements(user_id);

CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  supplement_id INTEGER NOT NULL,
  time_local TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  snooze_min INTEGER NOT NULL DEFAULT 15,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(user_id, supplement_id, time_local)
);
CREATE INDEX IF NOT EXISTS idx_rem_user_time ON reminders(user_id, time_local);

CREATE TABLE IF NOT EXISTS ui_state (
  user_id INTEGER PRIMARY KEY,
  last_message_id INTEGER NULL,
  last_screen TEXT NOT NULL DEFAULT '',
  last_payload TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reminder_fires (
  user_id INTEGER NOT NULL,
  supplement_id INTEGER NOT NULL,
  day TEXT NOT NULL,
  time_local TEXT NOT NULL,
  fired_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (user_id, supplement_id, day, time_local)
);

CREATE TABLE IF NOT EXISTS intake_log (
  -- LEGACY: kept for backward compatibility, do not use for status source of truth.
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  date_local    TEXT NOT NULL,
  slot_id       TEXT NOT NULL,
  taken_slugs_json TEXT NOT NULL DEFAULT '[]',
  skipped_slugs_json TEXT NOT NULL DEFAULT '[]',
  note          TEXT DEFAULT '',
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  UNIQUE(user_id, date_local, slot_id)
);

CREATE TABLE IF NOT EXISTS user_schedule_slots (
  user_id INTEGER PRIMARY KEY,
  slots_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS daily_checkins (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  date_local    TEXT NOT NULL,
  tz_name       TEXT NOT NULL,
  answers_json  TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  UNIQUE(user_id, date_local)
);

CREATE TABLE IF NOT EXISTS day_plan_items (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  date_local    TEXT NOT NULL,
  supplement_id INTEGER NOT NULL REFERENCES supplements(id) ON DELETE CASCADE,
  action        TEXT NOT NULL,
  dose_text     TEXT,
  time_hint     TEXT,
  reason        TEXT,
  source        TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  UNIQUE(user_id, date_local, supplement_id, action)
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
  -- LEGACY: kept for backward compatibility, do not use for status source of truth.
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

CREATE TABLE IF NOT EXISTS supplement_day_state (
  user_id       INTEGER NOT NULL,
  supplement_id INTEGER NOT NULL,
  day           TEXT NOT NULL,
  status        TEXT NOT NULL,
  dose_override TEXT NULL,
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (user_id, supplement_id, day)
);
CREATE INDEX IF NOT EXISTS idx_sds_user_day ON supplement_day_state(user_id, day);
CREATE INDEX IF NOT EXISTS idx_sds_user_sup_day ON supplement_day_state(user_id, supplement_id, day);

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
    await _ensure_users_tz_name(conn)
    await conn.execute("UPDATE users SET tz_name='UTC' WHERE tz_name IS NULL OR tz_name=''")
    await _ensure_column(conn, "users", "last_local_date", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "users", "last_view", "TEXT NOT NULL DEFAULT 'menu'")
    await _ensure_column(conn, "users", "last_view_payload", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "users", "last_chat_id", "INTEGER NOT NULL DEFAULT 0")
    await _ensure_column(conn, "users", "last_message_id", "INTEGER NOT NULL DEFAULT 0")
    await _ensure_column(conn, "users", "initial_stack_json", "TEXT NOT NULL DEFAULT '[]'")
    await _ensure_column(conn, "supplements", "name_ru", "TEXT")
    await _ensure_column(conn, "supplements", "name_en", "TEXT")
    await _ensure_column(conn, "supplements", "aliases", "TEXT DEFAULT ''")
    await _ensure_column(conn, "supplements", "aliases_json", "TEXT NOT NULL DEFAULT '[]'")
    await _ensure_column(conn, "supplements", "alias_norm", "TEXT DEFAULT ''")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_supplements_alias_norm ON supplements(alias_norm)")
    await _ensure_column(conn, "supplements", "search_blob", "TEXT NOT NULL DEFAULT ''")
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
    await _ensure_column(conn, "supplements", "tags_json", "TEXT")
    await _ensure_column(conn, "supplements", "mechanisms", "TEXT")
    await _ensure_column(conn, "supplements", "supplement_type", "TEXT DEFAULT ''")
    await _ensure_column(conn, "supplements", "functional_groups", "TEXT DEFAULT ''")
    await _ensure_column(conn, "supplements", "functional_groups_json", "TEXT NOT NULL DEFAULT '[]'")
    await _ensure_column(conn, "supplements", "evidence_grade", "TEXT")
    await _ensure_column(conn, "supplements", "body_effects_json", "TEXT NOT NULL DEFAULT '[]'")
    await _ensure_column(conn, "supplements", "body_targets", "TEXT DEFAULT ''")
    await _ensure_column(conn, "supplements", "body_targets_json", "TEXT NOT NULL DEFAULT '[]'")
    await _ensure_column(conn, "supplements", "body_burdens", "TEXT DEFAULT ''")
    await _ensure_column(conn, "supplements", "body_burdens_json", "TEXT NOT NULL DEFAULT '[]'")
    await _ensure_column(conn, "supplements", "safety_notes", "TEXT")
    await _ensure_column(conn, "supplements", "interaction_classes", "TEXT DEFAULT '[]'")
    await _ensure_column(conn, "supplements", "interaction_classes_json", "TEXT DEFAULT '[]'")
    await _ensure_column(conn, "supplements", "payload_json", "TEXT")
    await _ensure_column(conn, "supplements", "is_active", "INTEGER NOT NULL DEFAULT 1")
    await _ensure_aliases_table(conn)
    await _ensure_user_supplements(conn)
    await _ensure_user_schedules(conn)
    await _ensure_user_intake_log(conn)
    await _ensure_user_supp_settings(conn)
    await _ensure_supplement_day_state(conn)
    await _ensure_reminders(conn)
    await _ensure_ui_state(conn)
    await _ensure_reminder_fires(conn)
    await _ensure_daily_checkins(conn)
    await _ensure_day_plan_items(conn)
    await _ensure_intake_log_slots(conn)
    await _ensure_user_schedule_slots(conn)
    await _ensure_curated_stacks(conn)
    await _ensure_curated_stack_items(conn)
    await conn.execute(
        "UPDATE supplements SET interaction_classes='[]' WHERE interaction_classes IS NULL OR interaction_classes=''"
    )
    await conn.execute(
        "UPDATE supplements SET interaction_classes_json='[]' WHERE interaction_classes_json IS NULL OR interaction_classes_json=''"
    )
    await conn.execute(
        "UPDATE supplements SET functional_groups_json='[]' WHERE functional_groups_json IS NULL OR functional_groups_json=''"
    )
    await conn.execute(
        "UPDATE supplements SET body_effects_json='[]' WHERE body_effects_json IS NULL OR body_effects_json=''"
    )
    await conn.execute(
        "UPDATE supplements SET body_targets_json='[]' WHERE body_targets_json IS NULL OR body_targets_json=''"
    )
    await conn.execute(
        "UPDATE supplements SET body_burdens_json='[]' WHERE body_burdens_json IS NULL OR body_burdens_json=''"
    )
    await conn.execute(
        "UPDATE supplements SET aliases='' WHERE aliases IS NULL"
    )
    await conn.execute(
        "UPDATE supplements SET aliases_json='[]' WHERE aliases_json IS NULL OR aliases_json=''"
    )
    await conn.execute(
        "UPDATE supplements SET alias_norm='' WHERE alias_norm IS NULL"
    )
    await conn.execute(
        "UPDATE supplements SET search_blob='' WHERE search_blob IS NULL"
    )
    await conn.execute(
        "UPDATE supplements SET tags='' WHERE tags IS NULL"
    )
    await conn.execute(
        "UPDATE supplements SET tags_json='[]' WHERE tags_json IS NULL OR tags_json=''"
    )
    await conn.execute(
        "UPDATE supplements SET goals='' WHERE goals IS NULL"
    )
    await conn.execute(
        "UPDATE supplements SET rules_tags='' WHERE rules_tags IS NULL"
    )
    await conn.execute(
        "UPDATE supplements SET supplement_type='' WHERE supplement_type IS NULL"
    )
    await conn.execute(
        "UPDATE supplements SET functional_groups='' WHERE functional_groups IS NULL"
    )
    await conn.execute(
        "UPDATE supplements SET body_targets='' WHERE body_targets IS NULL"
    )
    await conn.execute(
        "UPDATE supplements SET body_burdens='' WHERE body_burdens IS NULL"
    )
    await conn.execute(
        "UPDATE users SET last_view='menu' WHERE last_view IS NULL OR last_view=''"
    )
    await conn.execute(
        "UPDATE users SET last_local_date='' WHERE last_local_date IS NULL"
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
    await _backfill_aliases_json(conn)
    await _backfill_search_blob(conn)
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
    await _ensure_column(conn, "user_supplements", "enabled", "INTEGER NOT NULL DEFAULT 1")
    await _ensure_column(conn, "user_supplements", "dose_text", "TEXT")
    await _ensure_column(conn, "user_supplements", "note_text", "TEXT")
    await _ensure_column(conn, "user_supplements", "notes", "TEXT")
    await _ensure_column(conn, "user_supplements", "created_at", "TEXT NOT NULL DEFAULT ''")
    await _backfill_user_supplements(conn)

async def _ensure_reminders(conn: aiosqlite.Connection) -> None:
    table = await fetchone(
        conn,
        "SELECT name FROM sqlite_master WHERE type='table' AND name='reminders'",
    )
    if not table:
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS reminders (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER NOT NULL,
              supplement_id INTEGER NOT NULL,
              time_local TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1,
              snooze_min INTEGER NOT NULL DEFAULT 15,
              created_at TEXT NOT NULL DEFAULT (datetime('now')),
              UNIQUE(user_id, supplement_id, time_local)
            )"""
        )
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_rem_user_time ON reminders(user_id, time_local)")
        return

    await _ensure_column(conn, "reminders", "user_id", "INTEGER NOT NULL")
    await _ensure_column(conn, "reminders", "supplement_id", "INTEGER NOT NULL")
    await _ensure_column(conn, "reminders", "time_local", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "reminders", "enabled", "INTEGER NOT NULL DEFAULT 1")
    await _ensure_column(conn, "reminders", "snooze_min", "INTEGER NOT NULL DEFAULT 15")
    await _ensure_column(conn, "reminders", "created_at", "TEXT NOT NULL DEFAULT ''")
    await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_rem_unique ON reminders(user_id, supplement_id, time_local)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_rem_user_time ON reminders(user_id, time_local)")

async def _ensure_ui_state(conn: aiosqlite.Connection) -> None:
    table = await fetchone(
        conn,
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ui_state'",
    )
    if not table:
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS ui_state (
              user_id INTEGER PRIMARY KEY,
              last_message_id INTEGER NULL,
              last_screen TEXT NOT NULL DEFAULT '',
              last_payload TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )"""
        )
        return
    await _ensure_column(conn, "ui_state", "last_message_id", "INTEGER")
    await _ensure_column(conn, "ui_state", "last_screen", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "ui_state", "last_payload", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "ui_state", "updated_at", "TEXT NOT NULL DEFAULT ''")

async def _ensure_reminder_fires(conn: aiosqlite.Connection) -> None:
    table = await fetchone(
        conn,
        "SELECT name FROM sqlite_master WHERE type='table' AND name='reminder_fires'",
    )
    if not table:
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS reminder_fires (
              user_id INTEGER NOT NULL,
              supplement_id INTEGER NOT NULL,
              day TEXT NOT NULL,
              time_local TEXT NOT NULL,
              fired_at TEXT NOT NULL DEFAULT (datetime('now')),
              PRIMARY KEY (user_id, supplement_id, day, time_local)
            )"""
        )
        return
    await _ensure_column(conn, "reminder_fires", "user_id", "INTEGER NOT NULL")
    await _ensure_column(conn, "reminder_fires", "supplement_id", "INTEGER NOT NULL")
    await _ensure_column(conn, "reminder_fires", "day", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "reminder_fires", "time_local", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "reminder_fires", "fired_at", "TEXT NOT NULL DEFAULT ''")

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

async def _ensure_intake_log_slots(conn: aiosqlite.Connection) -> None:
    table = await fetchone(
        conn,
        "SELECT name FROM sqlite_master WHERE type='table' AND name='intake_log'",
    )
    if not table:
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS intake_log (
              id            INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              date_local    TEXT NOT NULL,
              slot_id       TEXT NOT NULL,
              taken_slugs_json TEXT NOT NULL DEFAULT '[]',
              skipped_slugs_json TEXT NOT NULL DEFAULT '[]',
              note          TEXT DEFAULT '',
              created_at    TEXT NOT NULL,
              updated_at    TEXT NOT NULL,
              UNIQUE(user_id, date_local, slot_id)
            )"""
        )
    await _ensure_column(conn, "intake_log", "date_local", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "intake_log", "slot_id", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "intake_log", "taken_slugs_json", "TEXT NOT NULL DEFAULT '[]'")
    await _ensure_column(conn, "intake_log", "skipped_slugs_json", "TEXT NOT NULL DEFAULT '[]'")
    await _ensure_column(conn, "intake_log", "note", "TEXT DEFAULT ''")
    await _ensure_column(conn, "intake_log", "created_at", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "intake_log", "updated_at", "TEXT NOT NULL DEFAULT ''")

    cols = await fetchall(conn, "PRAGMA table_info(intake_log)")
    col_names = {c["name"] for c in cols}
    if "day" in col_names:
        await conn.execute(
            "UPDATE intake_log SET date_local=day WHERE (date_local IS NULL OR date_local='') AND day IS NOT NULL AND day!=''"
        )
    if "date" in col_names:
        await conn.execute(
            "UPDATE intake_log SET date_local=date WHERE (date_local IS NULL OR date_local='') AND date IS NOT NULL AND date!=''"
        )

    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_intake_unique_slot ON intake_log(user_id, date_local, slot_id)"
    )


async def _ensure_user_schedule_slots(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        """CREATE TABLE IF NOT EXISTS user_schedule_slots (
          user_id INTEGER PRIMARY KEY,
          slots_json TEXT NOT NULL DEFAULT '[]'
        )"""
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

async def _ensure_supplement_day_state(conn: aiosqlite.Connection) -> None:
    table = await fetchone(
        conn,
        "SELECT name FROM sqlite_master WHERE type='table' AND name='supplement_day_state'",
    )
    if not table:
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS supplement_day_state (
              user_id       INTEGER NOT NULL,
              supplement_id INTEGER NOT NULL,
              day           TEXT NOT NULL,
              status        TEXT NOT NULL,
              dose_override TEXT NULL,
              created_at    TEXT NOT NULL DEFAULT (datetime('now')),
              PRIMARY KEY (user_id, supplement_id, day)
            )"""
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sds_user_day ON supplement_day_state(user_id, day)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sds_user_sup_day ON supplement_day_state(user_id, supplement_id, day)"
        )
        return

    await _ensure_column(conn, "supplement_day_state", "user_id", "INTEGER NOT NULL")
    await _ensure_column(conn, "supplement_day_state", "supplement_id", "INTEGER NOT NULL")
    await _ensure_column(conn, "supplement_day_state", "day", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "supplement_day_state", "status", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "supplement_day_state", "dose_override", "TEXT")
    await _ensure_column(conn, "supplement_day_state", "created_at", "TEXT NOT NULL DEFAULT ''")
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sds_user_day ON supplement_day_state(user_id, day)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sds_user_sup_day ON supplement_day_state(user_id, supplement_id, day)"
    )

async def _rebuild_daily_checkins_table(conn: aiosqlite.Connection) -> None:
    cols = await fetchall(conn, "PRAGMA table_info(daily_checkins)")
    col_names = {c["name"] for c in cols}

    def _pick_expr(primary: str, fallbacks: list[str], default_sql: str) -> str:
        options = []
        for key in [primary, *fallbacks]:
            if key in col_names:
                options.append(f"NULLIF({key}, '')")
        options.append(default_sql)
        return f"COALESCE({', '.join(options)})"

    date_expr = _pick_expr("date_local", ["day", "date"], "'1970-01-01'")
    tz_expr = _pick_expr("tz_name", [], "'UTC'")
    answers_expr = _pick_expr("answers_json", ["payload_json"], "'{}'")
    created_expr = _pick_expr("created_at_utc", ["created_at"], "datetime('now')")

    await conn.execute("DROP TABLE IF EXISTS daily_checkins__new")
    await conn.execute(
        """CREATE TABLE daily_checkins__new (
          id            INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          date_local    TEXT NOT NULL,
          tz_name       TEXT NOT NULL,
          answers_json  TEXT NOT NULL,
          created_at_utc TEXT NOT NULL,
          UNIQUE(user_id, date_local)
        )"""
    )
    await conn.execute(
        f"""INSERT INTO daily_checkins__new(user_id, date_local, tz_name, answers_json, created_at_utc)
           SELECT user_id,
                  {date_expr} AS date_local,
                  {tz_expr} AS tz_name,
                  {answers_expr} AS answers_json,
                  {created_expr} AS created_at_utc
           FROM daily_checkins"""
    )
    await conn.execute("DROP TABLE daily_checkins")
    await conn.execute("ALTER TABLE daily_checkins__new RENAME TO daily_checkins")

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
              date_local    TEXT NOT NULL,
              tz_name       TEXT NOT NULL,
              answers_json  TEXT NOT NULL,
              created_at_utc TEXT NOT NULL,
              UNIQUE(user_id, date_local)
            )"""
        )
        return
    await _ensure_column(conn, "daily_checkins", "date_local", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "daily_checkins", "tz_name", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "daily_checkins", "answers_json", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "daily_checkins", "created_at_utc", "TEXT NOT NULL DEFAULT ''")
    cols = await fetchall(conn, "PRAGMA table_info(daily_checkins)")
    col_names = {c["name"] for c in cols}
    needs_rebuild = False
    for legacy_col in ("date", "day", "payload_json", "created_at"):
        if legacy_col in col_names:
            needs_rebuild = True
            break
    if needs_rebuild:
        await _rebuild_daily_checkins_table(conn)
        cols = await fetchall(conn, "PRAGMA table_info(daily_checkins)")
        col_names = {c["name"] for c in cols}
    if "day" in col_names:
        await conn.execute(
            "UPDATE daily_checkins SET date_local=day WHERE date_local IS NULL OR date_local=''"
        )
    if "date" in col_names:
        await conn.execute(
            "UPDATE daily_checkins SET date_local=date WHERE date_local IS NULL OR date_local=''"
        )
    if "created_at" in col_names:
        await conn.execute(
            "UPDATE daily_checkins SET created_at_utc=created_at WHERE created_at_utc IS NULL OR created_at_utc=''"
        )
    if "payload_json" in col_names:
        await conn.execute(
            "UPDATE daily_checkins SET answers_json=payload_json WHERE answers_json IS NULL OR answers_json=''"
        )
    await conn.execute(
        "UPDATE daily_checkins SET created_at_utc=? WHERE created_at_utc IS NULL OR created_at_utc=''",
        (utc_now_iso(),),
    )
    await conn.execute(
        "UPDATE daily_checkins SET answers_json='{}' WHERE answers_json IS NULL OR answers_json=''",
    )
    await conn.execute(
        "UPDATE daily_checkins SET tz_name='UTC' WHERE tz_name IS NULL OR tz_name=''"
    )
    await conn.execute(
        """DELETE FROM daily_checkins
           WHERE id NOT IN (
             SELECT MAX(id) FROM daily_checkins GROUP BY user_id, date_local
           )"""
    )
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_checkins_user_date ON daily_checkins(user_id, date_local)"
    )

async def _ensure_day_plan_items(conn: aiosqlite.Connection) -> None:
    table = await fetchone(
        conn,
        "SELECT name FROM sqlite_master WHERE type='table' AND name='day_plan_items'",
    )
    if not table:
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS day_plan_items (
              id            INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              date_local    TEXT NOT NULL,
              supplement_id INTEGER NOT NULL REFERENCES supplements(id) ON DELETE CASCADE,
              action        TEXT NOT NULL,
              dose_text     TEXT,
              time_hint     TEXT,
              reason        TEXT,
              source        TEXT NOT NULL,
              created_at_utc TEXT NOT NULL,
              UNIQUE(user_id, date_local, supplement_id, action)
            )"""
        )
        return
    await _ensure_column(conn, "day_plan_items", "date_local", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "day_plan_items", "supplement_id", "INTEGER NOT NULL DEFAULT 0")
    await _ensure_column(conn, "day_plan_items", "action", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "day_plan_items", "dose_text", "TEXT")
    await _ensure_column(conn, "day_plan_items", "time_hint", "TEXT")
    await _ensure_column(conn, "day_plan_items", "reason", "TEXT")
    await _ensure_column(conn, "day_plan_items", "source", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "day_plan_items", "created_at_utc", "TEXT NOT NULL DEFAULT ''")
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_day_plan_items_unique ON day_plan_items(user_id, date_local, supplement_id, action)"
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
    if "enabled" in col_names:
        await conn.execute(
            """UPDATE user_supplements
               SET enabled=is_active
               WHERE (enabled IS NULL OR enabled='') AND is_active IS NOT NULL"""
        )
    if "notes" in col_names:
        await conn.execute(
            """UPDATE user_supplements
               SET notes=note_text
               WHERE (notes IS NULL OR notes='') AND note_text IS NOT NULL AND note_text!=''"""
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

async def _ensure_users_tz_name(conn: aiosqlite.Connection) -> None:
    cols = await fetchall(conn, "PRAGMA table_info(users)")
    if any(c["name"] == "tz_name" for c in cols):
        return
    await conn.execute("ALTER TABLE users ADD COLUMN tz_name TEXT NOT NULL DEFAULT 'UTC'")

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

def _parse_aliases_list(raw: str | list | tuple | None) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    raw_text = str(raw).strip()
    if not raw_text:
        return []
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [item.strip() for item in raw_text.split(",") if item.strip()]

async def _backfill_aliases_json(conn: aiosqlite.Connection) -> None:
    rows = await fetchall(
        conn,
        "SELECT id, aliases, aliases_json FROM supplements WHERE aliases_json IS NULL OR aliases_json=''",
    )
    for row in rows:
        aliases = _parse_aliases_list(row["aliases"])
        await conn.execute(
            "UPDATE supplements SET aliases_json=? WHERE id=?",
            (json.dumps(aliases, ensure_ascii=False), int(row["id"])),
        )

def build_search_blob(name_ru: str | None, name_en: str | None, aliases: list[str]) -> str:
    parts: list[str] = []
    for value in (name_ru, name_en):
        norm = normalize_text(value)
        if norm:
            parts.append(norm)
    for alias in aliases:
        norm = normalize_text(alias)
        if norm:
            parts.append(norm)
    return "|".join(parts)

async def _backfill_search_blob(conn: aiosqlite.Connection) -> None:
    rows = await fetchall(conn, "SELECT id, name_ru, name_en, aliases, aliases_json FROM supplements")
    for row in rows:
        aliases = []
        aliases.extend(_parse_aliases_list(row["aliases"]))
        aliases.extend(_parse_aliases_list(row["aliases_json"]))
        alias_rows = await fetchall(
            conn,
            "SELECT alias_raw FROM supplement_aliases WHERE supplement_id=?",
            (int(row["id"]),),
        )
        aliases.extend([str(item["alias_raw"] or "") for item in alias_rows])
        search_blob = build_search_blob(row["name_ru"], row["name_en"], aliases)
        await conn.execute(
            "UPDATE supplements SET search_blob=? WHERE id=?",
            (search_blob, int(row["id"])),
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
    tz_value = tz_name.strip() if isinstance(tz_name, str) and tz_name.strip() else "UTC"
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

def _clean_initial_stack_ids(items: list[int]) -> list[int]:
    cleaned: list[int] = []
    for item in items:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value not in cleaned:
            cleaned.append(value)
    return cleaned

async def get_user_initial_stack_ids(conn: aiosqlite.Connection, tg_user_id: int) -> list[int]:
    user_id = await get_or_create_user(conn, tg_user_id)
    row = await fetchone(conn, "SELECT initial_stack_json FROM users WHERE id=?", (user_id,))
    raw = row["initial_stack_json"] if row else "[]"
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        data = []
    if not isinstance(data, list):
        data = []
    return _clean_initial_stack_ids(data)

async def set_user_initial_stack_ids(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    supplement_ids: list[int],
) -> None:
    user_id = await get_or_create_user(conn, tg_user_id)
    cleaned = _clean_initial_stack_ids(supplement_ids)
    payload = json.dumps(cleaned, ensure_ascii=False)
    await conn.execute(
        "UPDATE users SET initial_stack_json=? WHERE id=?",
        (payload, user_id),
    )
    await conn.commit()

async def add_user_initial_stack_id(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    supplement_id: int,
) -> None:
    items = await get_user_initial_stack_ids(conn, tg_user_id)
    if supplement_id not in items:
        items.append(int(supplement_id))
        await set_user_initial_stack_ids(conn, tg_user_id, items)

def parse_utc_tz(tz_name: str | None):
    if not tz_name:
        return timezone.utc
    value = str(tz_name).strip()
    if value.upper() in {"UTC", "GMT"}:
        return timezone.utc
    match = re.fullmatch(r"(?:UTC|GMT)?([+-](?:0\d|1\d|2[0-3])):([0-5]\d)", value)
    if match:
        sign_hours, minutes_str = match.groups()
        sign = 1 if sign_hours[0] == "+" else -1
        hours = int(sign_hours[1:])
        minutes = int(minutes_str)
        offset = timedelta(hours=hours, minutes=minutes)
        return timezone(sign * offset)
    try:
        return ZoneInfo(value)
    except Exception:
        return timezone.utc

def parse_offset_tz(tz_name: str | None):
    return parse_utc_tz(tz_name)

def local_now(tz_name: str | None) -> datetime:
    return datetime.now(parse_utc_tz(tz_name))

def local_date(tz_name: str | None) -> str:
    return local_now(tz_name).date().isoformat()

def get_local_date_str(tz_name: str | None) -> str:
    return datetime.now(parse_utc_tz(tz_name)).date().isoformat()

DEFAULT_USER_SLOTS = [
    {"slot_id": "morning", "title_ru": "Утро", "time_local": "08:30", "is_enabled": True},
    {"slot_id": "noon", "title_ru": "День", "time_local": "13:30", "is_enabled": True},
    {"slot_id": "evening", "title_ru": "Вечер", "time_local": "19:30", "is_enabled": True},
    {"slot_id": "night", "title_ru": "Ночь", "time_local": "22:30", "is_enabled": True},
]


def default_user_slots() -> list[dict]:
    return [dict(item) for item in DEFAULT_USER_SLOTS]


def _normalize_slots(slots_list: list[dict] | None) -> list[dict]:
    if not isinstance(slots_list, list):
        return default_user_slots()
    out: list[dict] = []
    seen: set[str] = set()
    for raw in slots_list:
        if not isinstance(raw, dict):
            continue
        slot_id = str(raw.get("slot_id") or "").strip()
        if not slot_id or slot_id in seen:
            continue
        time_local = str(raw.get("time_local") or "").strip()
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", time_local):
            continue
        title_ru = str(raw.get("title_ru") or "").strip() or "Слот"
        out.append(
            {
                "slot_id": slot_id,
                "title_ru": title_ru,
                "time_local": time_local,
                "is_enabled": bool(raw.get("is_enabled", True)),
            }
        )
        seen.add(slot_id)
    return out or default_user_slots()

def next_run_datetime_for_hhmm(hhmm: str, tz_name: str | None) -> datetime:
    tzinfo = parse_utc_tz(tz_name)
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

async def ensure_daily_rollover(conn: aiosqlite.Connection, tg_user_id: int) -> str:
    row = await fetchone(
        conn,
        "SELECT tz_name, last_local_date FROM users WHERE tg_user_id=?",
        (tg_user_id,),
    )
    if not row:
        await get_or_create_user(conn, tg_user_id, tz_name="")
        row = await fetchone(
            conn,
            "SELECT tz_name, last_local_date FROM users WHERE tg_user_id=?",
            (tg_user_id,),
        )
    tz_name = str(row["tz_name"] or "").strip() if row else ""
    last_local_date = str(row["last_local_date"] or "") if row else ""
    today_local = get_local_date_str(tz_name or None)
    if last_local_date != today_local:
        await conn.execute(
            "UPDATE users SET last_local_date=? WHERE tg_user_id=?",
            (today_local, tg_user_id),
        )
        await conn.commit()
    return today_local

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
    aliases_list = _parse_aliases_list(s.get("aliases_json") or s.get("aliases") or "")
    aliases_raw = json.dumps(aliases_list, ensure_ascii=False)
    tags_raw = s.get("tags") or ""
    if isinstance(tags_raw, (list, tuple)):
        tags_raw = " ".join([str(item) for item in tags_raw if item])
    evidence_fields = _extract_evidence_fields(s.get("evidence_json"), s.get("payload_json"))
    search_blob = s.get("search_blob") or build_search_blob(name_ru, name_en, aliases_list)
    await conn.execute(
        """INSERT INTO supplements(
          slug,name,name_ru,name_en,aliases,aliases_json,alias_norm,search_blob,
          human_trials_count_est,participants_total_est,avg_effect_percent_est,confidence,
          tags,goals,rules_tags,summary_ru,summary_verified,summary_theoretical,molecular_mechanisms,
          evidence_json,dose_ru,timing_ru,safety_ru,purchase_url,tags_json,mechanisms,supplement_type,
          functional_groups,functional_groups_json,evidence_grade,body_effects_json,body_targets,
          body_targets_json,body_burdens,body_burdens_json,
          safety_notes,interaction_classes,interaction_classes_json,payload_json,is_active
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(slug) DO UPDATE SET
          name=excluded.name,
          name_ru=excluded.name_ru,
          name_en=excluded.name_en,
          aliases=excluded.aliases,
          aliases_json=excluded.aliases_json,
          alias_norm=excluded.alias_norm,
          search_blob=excluded.search_blob,
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
          supplement_type=excluded.supplement_type,
          functional_groups=excluded.functional_groups,
          functional_groups_json=excluded.functional_groups_json,
          evidence_grade=excluded.evidence_grade,
          body_effects_json=excluded.body_effects_json,
          body_targets=excluded.body_targets,
          body_targets_json=excluded.body_targets_json,
          body_burdens=excluded.body_burdens,
          body_burdens_json=excluded.body_burdens_json,
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
            aliases_raw,
            s.get("alias_norm") or "",
            search_blob,
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
            s.get("supplement_type") or "",
            s.get("functional_groups") or "",
            s.get("functional_groups_json") or "[]",
            s.get("evidence_grade") or "",
            s.get("body_effects_json") or "[]",
            s.get("body_targets") or "",
            s.get("body_targets_json") or "[]",
            s.get("body_burdens") or "",
            s.get("body_burdens_json") or "[]",
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

async def get_supplement_by_id(conn: aiosqlite.Connection, supplement_id: int) -> dict | None:
    row = await fetchone(conn, "SELECT * FROM supplements WHERE id=?", (supplement_id,))
    if not row:
        return None
    return dict(row)

async def list_supplements(
    conn: aiosqlite.Connection,
    page: int,
    page_size: int = 10,
) -> tuple[list[dict], int]:
    page = max(int(page), 0)
    page_size = max(int(page_size), 1)
    offset = page * page_size
    total_row = await fetchone(
        conn,
        "SELECT COUNT(*) AS total FROM supplements WHERE is_active=1",
    )
    total_count = int(total_row["total"]) if total_row else 0
    rows = await fetchall(
        conn,
        """SELECT id, name_ru, name_en, slug,
                  human_trials_count_est, participants_total_est, avg_effect_percent_est,
                  confidence, evidence_grade
           FROM supplements
           WHERE is_active=1
           ORDER BY COALESCE(NULLIF(name_ru, ''), NULLIF(name_en, ''), name) COLLATE NOCASE, id
           LIMIT ? OFFSET ?""",
        (page_size, offset),
    )
    return ([dict(row) for row in rows], total_count)

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
    seen: set[tuple[object, str, str, str]] = set()
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
        key = (other_id, kind, reason, "")
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
                other_name_ru = (item.get("other_name_ru") or "").strip()
                other_slug = (item.get("other_slug") or "").strip()
                other_label = other_name_ru or other_slug
                reason = (item.get("note_ru") or item.get("note") or "").strip()
                strength = _severity_to_strength(item.get("severity"))
                key = (other_id, kind, reason, other_label)
                if key in seen:
                    continue
                seen.add(key)
                if other_id:
                    interactions.append(
                        {
                            "other_supplement_id": int(other_id),
                            "kind": kind,
                            "reason_ru": reason,
                            "strength": strength,
                        }
                    )
                elif other_label:
                    interactions.append(
                        {
                            "other_supplement_id": None,
                            "other_label_ru": other_label,
                            "kind": kind,
                            "reason_ru": reason,
                            "strength": strength,
                        }
                    )

    return interactions

async def search_supplements(
    conn: aiosqlite.Connection,
    query: str,
    page: int,
    page_size: int = 10,
) -> tuple[list[dict], int]:
    qn = normalize_text(query)
    if not qn:
        return ([], 0)
    page = max(int(page), 0)
    page_size = max(int(page_size), 1)
    offset = page * page_size
    q_like = f"%{qn}%"
    total_row = await fetchone(
        conn,
        "SELECT COUNT(*) AS total FROM supplements WHERE is_active=1 AND search_blob LIKE ?",
        (q_like,),
    )
    total_count = int(total_row["total"]) if total_row else 0
    rows = await fetchall(
        conn,
        """SELECT id, name, name_ru, name_en, slug
           FROM supplements
           WHERE is_active=1 AND search_blob LIKE ?
           ORDER BY CASE WHEN search_blob LIKE ? THEN 0 ELSE 1 END,
                    name_ru COLLATE NOCASE
           LIMIT ? OFFSET ?""",
        (q_like, f"{qn}%", page_size, offset),
    )
    return ([dict(row) for row in rows], total_count)

async def count_search_supplements(conn: aiosqlite.Connection, q: str) -> int:
    qn = normalize_text(q)
    if not qn:
        return 0
    q_like = f"%{qn}%"
    row = await fetchone(
        conn,
        "SELECT COUNT(*) AS total FROM supplements WHERE is_active=1 AND search_blob LIKE ?",
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
        order = "name_ru COLLATE NOCASE ASC, slug COLLATE NOCASE ASC"
    elif sort_key == "effect":
        order = "avg_effect_percent_est DESC, name_ru COLLATE NOCASE"
    else:
        order = "human_trials_count_est DESC, participants_total_est DESC"
    return await fetchall(
        conn,
        f"""SELECT id, slug, name, name_ru, name_en, evidence_grade
            FROM supplements
            WHERE is_active=1
            ORDER BY {order}
            LIMIT ? OFFSET ?""",
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


async def list_supplements_by_type(
    conn: aiosqlite.Connection,
    sup_type: str,
    limit: int = 50,
    offset: int = 0,
):
    token = sup_type.strip().lower()
    if not token:
        return []
    like_token = f"% {token} %"
    return await fetchall(
        conn,
        """SELECT * FROM supplements
           WHERE is_active=1 AND (" " || lower(supplement_type) || " ") LIKE ?
           ORDER BY name_ru COLLATE NOCASE ASC
           LIMIT ? OFFSET ?""",
        (like_token, limit, offset),
    )


async def list_supplements_by_body_target(
    conn: aiosqlite.Connection,
    system_token: str,
    limit: int = 50,
    offset: int = 0,
):
    token = system_token.strip().lower()
    if not token:
        return []
    like_token = f"% {token} %"
    return await fetchall(
        conn,
        """SELECT * FROM supplements
           WHERE is_active=1 AND (" " || lower(body_targets) || " ") LIKE ?
           ORDER BY name_ru COLLATE NOCASE ASC
           LIMIT ? OFFSET ?""",
        (like_token, limit, offset),
    )


async def list_supplements_by_body_burden(
    conn: aiosqlite.Connection,
    system_token: str,
    limit: int = 50,
    offset: int = 0,
):
    token = system_token.strip().lower()
    if not token:
        return []
    like_token = f"% {token} %"
    return await fetchall(
        conn,
        """SELECT * FROM supplements
           WHERE is_active=1 AND (" " || lower(body_burdens) || " ") LIKE ?
           ORDER BY name_ru COLLATE NOCASE ASC
           LIMIT ? OFFSET ?""",
        (like_token, limit, offset),
    )


async def list_supplements_by_functional_group(
    conn: aiosqlite.Connection,
    group_token: str,
    limit: int = 50,
    offset: int = 0,
):
    token = group_token.strip().lower()
    if not token:
        return []
    like_token = f"% {token} %"
    return await fetchall(
        conn,
        """SELECT * FROM supplements
           WHERE is_active=1 AND (" " || lower(functional_groups) || " ") LIKE ?
           ORDER BY name_ru COLLATE NOCASE ASC
           LIMIT ? OFFSET ?""",
        (like_token, limit, offset),
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
        "UPDATE supplements SET aliases=?, aliases_json=? WHERE id=?",
        (
            json.dumps(list({str(a).strip() for a in aliases if str(a).strip()}), ensure_ascii=False),
            json.dumps(list({str(a).strip() for a in aliases if str(a).strip()}), ensure_ascii=False),
            supplement_id,
        ),
    )
    await conn.commit()
    row = await fetchone(conn, "SELECT * FROM supplements WHERE id=?", (supplement_id,))
    if row:
        alias_norm = await _build_supplement_alias_norm(conn, row)
        search_blob = build_search_blob(
            row["name_ru"] if "name_ru" in row.keys() else None,
            row["name_en"] if "name_en" in row.keys() else None,
            _parse_aliases_list(
                row["aliases_json"] if "aliases_json" in row.keys() else row["aliases"] if "aliases" in row.keys() else ""
            ),
        )
        await conn.execute(
            "UPDATE supplements SET alias_norm=?, search_blob=? WHERE id=?",
            (alias_norm, search_blob, supplement_id),
        )
        await conn.commit()

async def list_supplements_page(conn: aiosqlite.Connection, offset: int, limit: int = 10):
    return await fetchall(
        conn,
        "SELECT * FROM supplements ORDER BY name LIMIT ? OFFSET ?",
        (limit, offset),
    )


async def list_supplements_alpha(conn: aiosqlite.Connection, limit: int = 30, offset: int = 0) -> list[aiosqlite.Row]:
    return await fetchall(
        conn,
        """SELECT slug, name_ru, evidence_grade
           FROM supplements
           WHERE is_active=1
           ORDER BY name_ru COLLATE NOCASE ASC
           LIMIT ? OFFSET ?""",
        (limit, offset),
    )


async def admin_get_global_stats(conn: aiosqlite.Connection) -> dict:
    today = date.today()
    threshold_1d = (today - timedelta(days=1)).isoformat()
    threshold_7d = (today - timedelta(days=7)).isoformat()
    threshold_30d = (today - timedelta(days=30)).isoformat()

    users_total = 0
    if await _table_exists(conn, "users"):
        row = await fetchone(conn, "SELECT COUNT(*) AS total FROM users")
        users_total = int(row["total"] or 0) if row else 0

    activity_tables: list[tuple[str, str]] = []
    for name in ("daily_checkins", "intake_log", "user_intake_log"):
        if not await _table_exists(conn, name):
            continue
        date_col = await _get_date_col(conn, name)
        cols = await _table_columns(conn, name)
        if date_col and "user_id" in cols:
            activity_tables.append((name, date_col))

    async def _count_active(threshold: str) -> int:
        if not activity_tables:
            return 0
        selects = [f"SELECT user_id FROM {table} WHERE {date_col} >= ?" for table, date_col in activity_tables]
        sql = "SELECT COUNT(DISTINCT user_id) AS total FROM (" + " UNION ".join(selects) + ")"
        params = tuple(threshold for _ in activity_tables)
        row = await fetchone(conn, sql, params)
        return int(row["total"] or 0) if row else 0

    active_1d = await _count_active(threshold_1d)
    active_7d = await _count_active(threshold_7d)
    active_30d = await _count_active(threshold_30d)

    avg_active_supplements_overall = None
    for name in ("user_supplements", "user_stack_items", "user_base_stack"):
        if not await _table_exists(conn, name):
            continue
        cols = await _table_columns(conn, name)
        active_col = "is_active" if "is_active" in cols else "active" if "active" in cols else None
        if "user_id" not in cols or not active_col:
            continue
        row = await fetchone(
            conn,
            f"""SELECT AVG(cnt) AS avg_cnt FROM (
                   SELECT user_id, COUNT(*) AS cnt
                   FROM {name}
                   WHERE {active_col}=1
                   GROUP BY user_id
               )""",
        )
        avg_active_supplements_overall = float(row["avg_cnt"]) if row and row["avg_cnt"] is not None else None
        break

    avg_enabled_reminders = None
    if await _table_exists(conn, "reminders"):
        cols = await _table_columns(conn, "reminders")
        enabled_col = "is_enabled" if "is_enabled" in cols else "enabled" if "enabled" in cols else None
        if "user_id" in cols and enabled_col:
            row = await fetchone(
                conn,
                f"""SELECT AVG(cnt) AS avg_cnt FROM (
                       SELECT user_id, COUNT(*) AS cnt
                       FROM reminders
                       WHERE {enabled_col}=1
                       GROUP BY user_id
                   )""",
            )
            avg_enabled_reminders = float(row["avg_cnt"]) if row and row["avg_cnt"] is not None else None
    if avg_enabled_reminders is None and await _table_exists(conn, "user_schedule_slots"):
        cols = await _table_columns(conn, "user_schedule_slots")
        if "user_id" in cols and "slots_json" in cols:
            rows = await fetchall(conn, "SELECT user_id, slots_json FROM user_schedule_slots")
            counts: list[int] = []
            for row in rows:
                raw = row["slots_json"]
                try:
                    slots = json.loads(raw) if raw else []
                except Exception:
                    slots = []
                if not isinstance(slots, list):
                    slots = []
                enabled_count = 0
                for slot in slots:
                    if isinstance(slot, dict) and bool(slot.get("is_enabled", False)):
                        enabled_count += 1
                counts.append(enabled_count)
            if counts:
                avg_enabled_reminders = float(sum(counts) / len(counts))

    checkins_30d_total = 0
    if await _table_exists(conn, "daily_checkins"):
        date_col = await _get_date_col(conn, "daily_checkins")
        if date_col:
            row = await fetchone(
                conn,
                f"SELECT COUNT(*) AS total FROM daily_checkins WHERE {date_col} >= ?",
                (threshold_30d,),
            )
            checkins_30d_total = int(row["total"] or 0) if row else 0

    intake_30d_total = 0
    for intake_table in ("intake_log", "user_intake_log"):
        if not await _table_exists(conn, intake_table):
            continue
        date_col = await _get_date_col(conn, intake_table)
        if not date_col:
            continue
        row = await fetchone(
            conn,
            f"SELECT COUNT(*) AS total FROM {intake_table} WHERE {date_col} >= ?",
            (threshold_30d,),
        )
        intake_30d_total = int(row["total"] or 0) if row else 0
        break

    return {
        "users_total": users_total,
        "active_1d": active_1d,
        "active_7d": active_7d,
        "active_30d": active_30d,
        "avg_active_supplements_overall": avg_active_supplements_overall,
        "avg_enabled_reminders": avg_enabled_reminders,
        "checkins_30d_total": checkins_30d_total,
        "intake_30d_total": intake_30d_total,
    }

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


async def admin_list_curated_stacks(conn: aiosqlite.Connection, limit: int, offset: int = 0):
    return await fetchall(
        conn,
        """SELECT * FROM curated_stacks
           ORDER BY updated_at DESC, created_at DESC
           LIMIT ? OFFSET ?""",
        (limit, offset),
    )


async def admin_get_curated_stack_by_id(conn: aiosqlite.Connection, stack_id: int):
    return await fetchone(conn, "SELECT * FROM curated_stacks WHERE id=?", (stack_id,))


async def admin_get_curated_stack_by_slug(conn: aiosqlite.Connection, slug: str):
    return await fetchone(conn, "SELECT * FROM curated_stacks WHERE slug=?", (slug,))


async def admin_upsert_curated_stack(
    conn: aiosqlite.Connection,
    *,
    slug: str,
    title_ru: str,
    author_ru: str,
    description_ru: str,
    source_url: str,
    tags: list[str],
    is_active: int,
):
    tags_json = json.dumps(list(tags or []), ensure_ascii=False)
    await conn.execute(
        """INSERT INTO curated_stacks(
               slug, title_ru, author_ru, description_ru, source_url, tags_json, is_active, created_at, updated_at
           ) VALUES(?,?,?,?,?,?,?,datetime('now'),datetime('now'))
           ON CONFLICT(slug) DO UPDATE SET
             title_ru=excluded.title_ru,
             author_ru=excluded.author_ru,
             description_ru=excluded.description_ru,
             source_url=excluded.source_url,
             tags_json=excluded.tags_json,
             is_active=excluded.is_active,
             updated_at=datetime('now')""",
        (slug, title_ru, author_ru, description_ru, source_url, tags_json, int(is_active)),
    )
    await conn.commit()


async def admin_set_curated_stack_active(conn: aiosqlite.Connection, stack_id: int, is_active: int):
    await conn.execute(
        "UPDATE curated_stacks SET is_active=?, updated_at=datetime('now') WHERE id=?",
        (int(is_active), int(stack_id)),
    )
    await conn.commit()


async def admin_add_curated_stack_item(
    conn: aiosqlite.Connection,
    stack_id: int,
    supplement_slug: str,
    supplement_name: str,
    dose_ru: str,
    timing_ru: str,
    notes_ru: str,
):
    row = await fetchone(
        conn,
        "SELECT COALESCE(MAX(sort_order), 0) AS max_sort FROM curated_stack_items WHERE stack_id=?",
        (int(stack_id),),
    )
    sort_order = int(row["max_sort"] or 0) + 10 if row else 10
    await conn.execute(
        """INSERT INTO curated_stack_items(
               stack_id, supplement_slug, supplement_name, dose_ru, timing_ru, notes_ru, sort_order
           ) VALUES(?,?,?,?,?,?,?)""",
        (
            int(stack_id),
            str(supplement_slug or "").strip(),
            str(supplement_name or "").strip(),
            str(dose_ru or "").strip(),
            str(timing_ru or "").strip(),
            str(notes_ru or "").strip(),
            sort_order,
        ),
    )
    await conn.commit()


async def admin_get_curated_stack_item(conn: aiosqlite.Connection, item_id: int):
    return await fetchone(conn, "SELECT * FROM curated_stack_items WHERE id=?", (int(item_id),))


async def admin_update_curated_stack_item(conn: aiosqlite.Connection, item_id: int, dose_ru: str, timing_ru: str, notes_ru: str):
    await conn.execute(
        "UPDATE curated_stack_items SET dose_ru=?, timing_ru=?, notes_ru=? WHERE id=?",
        (str(dose_ru or "").strip(), str(timing_ru or "").strip(), str(notes_ru or "").strip(), int(item_id)),
    )
    await conn.commit()


async def admin_delete_curated_stack_item(conn: aiosqlite.Connection, item_id: int):
    await conn.execute("DELETE FROM curated_stack_items WHERE id=?", (int(item_id),))
    await conn.commit()


async def admin_move_curated_stack_item(conn: aiosqlite.Connection, stack_id: int, item_id: int, direction: str):
    rows = await fetchall(
        conn,
        "SELECT id, sort_order FROM curated_stack_items WHERE stack_id=? ORDER BY sort_order, id",
        (int(stack_id),),
    )
    idx = -1
    for i, row in enumerate(rows):
        if int(row["id"]) == int(item_id):
            idx = i
            break
    if idx < 0:
        return
    swap_idx = idx - 1 if direction == "up" else idx + 1
    if swap_idx < 0 or swap_idx >= len(rows):
        return
    cur = rows[idx]
    other = rows[swap_idx]
    await conn.execute("UPDATE curated_stack_items SET sort_order=? WHERE id=?", (int(other["sort_order"]), int(cur["id"])))
    await conn.execute("UPDATE curated_stack_items SET sort_order=? WHERE id=?", (int(cur["sort_order"]), int(other["id"])))
    await conn.commit()

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
    rows, _ = await search_supplements(conn, name, page=0, page_size=1)
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
            """INSERT INTO user_supplements(user_id, supplement_id, is_active, enabled, created_at)
               VALUES(?,?,1,1,?)
               ON CONFLICT(user_id, supplement_id) DO UPDATE SET
                 is_active=1,
                 enabled=1""",
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

async def get_supplement_links(conn: aiosqlite.Connection, supplement_id: int) -> dict:
    synergy_ids: set[int] = set()
    antagonist_ids: set[int] = set()
    rows = await fetchall(
        conn,
        """SELECT a_id, b_id, relation
           FROM interactions
           WHERE a_id=? OR b_id=?""",
        (supplement_id, supplement_id),
    )
    for row in rows:
        a_id = int(row["a_id"])
        b_id = int(row["b_id"])
        other_id = b_id if a_id == supplement_id else a_id
        relation = str(row["relation"] or "").strip().lower()
        if relation in {"synergy"}:
            synergy_ids.add(other_id)
        elif relation in {"antagonist", "antagonism", "avoid"}:
            antagonist_ids.add(other_id)
    return {
        "synergy": sorted(synergy_ids),
        "antagonists": sorted(antagonist_ids),
    }

async def list_linked_supplements(conn: aiosqlite.Connection, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    cleaned = [int(item) for item in ids]
    placeholders = ",".join("?" for _ in cleaned)
    rows = await fetchall(
        conn,
        f"""SELECT id, name_ru, name_en
            FROM supplements
            WHERE id IN ({placeholders})""",
        tuple(cleaned),
    )
    by_id = {int(row["id"]): dict(row) for row in rows}
    return [by_id[item] for item in cleaned if item in by_id]

# user library
async def add_user_supplement(conn: aiosqlite.Connection, tg_user_id: int, supplement_id: int) -> None:
    user_id = await get_or_create_user(conn, tg_user_id)
    await conn.execute(
        """INSERT INTO user_supplements(user_id, supplement_id, is_active, enabled, created_at)
           VALUES(?,?,1,1,?)
           ON CONFLICT(user_id, supplement_id) DO UPDATE SET
             is_active=1,
             enabled=1""",
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

async def add_user_supp(conn: aiosqlite.Connection, user_id: int, supplement_id: int) -> None:
    await add_user_supplement(conn, user_id, supplement_id)

async def remove_user_supplement(conn: aiosqlite.Connection, tg_user_id: int, supplement_id: int) -> None:
    user_id = await get_or_create_user(conn, tg_user_id)
    await conn.execute("DELETE FROM user_supplements WHERE user_id=? AND supplement_id=?", (user_id, supplement_id))
    await conn.execute(
        "DELETE FROM user_supp_settings WHERE user_id=? AND supplement_id=?",
        (user_id, supplement_id),
    )
    await conn.execute(
        "DELETE FROM reminders WHERE user_id=? AND supplement_id=?",
        (tg_user_id, supplement_id),
    )
    await conn.commit()

async def remove_user_supp(conn: aiosqlite.Connection, user_id: int, supplement_id: int) -> None:
    await remove_user_supplement(conn, user_id, supplement_id)

async def remove_user_supplements_bulk(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    supplement_ids: list[int],
) -> None:
    if not supplement_ids:
        return
    cleaned = [int(item) for item in supplement_ids if str(item).isdigit()]
    if not cleaned:
        return
    user_id = await get_or_create_user(conn, tg_user_id)
    placeholders = ",".join("?" for _ in cleaned)
    await conn.execute(
        f"DELETE FROM user_supplements WHERE user_id=? AND supplement_id IN ({placeholders})",
        (user_id, *cleaned),
    )
    await conn.execute(
        f"DELETE FROM user_supp_settings WHERE user_id=? AND supplement_id IN ({placeholders})",
        (user_id, *cleaned),
    )
    await conn.execute(
        f"DELETE FROM reminders WHERE user_id=? AND supplement_id IN ({placeholders})",
        (tg_user_id, *cleaned),
    )
    await conn.commit()

async def get_user_supplement(conn: aiosqlite.Connection, tg_user_id: int, supplement_id: int):
    user_id = await get_or_create_user(conn, tg_user_id)
    return await fetchone(
        conn,
        """SELECT us.id, us.user_id, us.supplement_id, us.is_active, us.enabled,
                  COALESCE(NULLIF(uss.dose_text, ''), us.dose_text) AS dose_text,
                  us.note_text, us.notes, us.created_at,
                  s.slug, s.name, s.name_ru, s.name_en,
                  s.summary_ru, s.summary_verified, s.summary_theoretical,
                  s.molecular_mechanisms, s.mechanisms
           FROM user_supplements us
           JOIN supplements s ON s.id=us.supplement_id
           LEFT JOIN user_supp_settings uss ON uss.user_id=us.user_id AND uss.supplement_id=us.supplement_id
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
        "LEFT JOIN users u ON u.id=us.user_id "
        "LEFT JOIN supplement_day_state sds ON sds.user_id=u.tg_user_id "
        "AND sds.supplement_id=us.supplement_id AND sds.day=?"
    )
    params.extend([user_id, supplement_id])
    return await fetchone(
        conn,
        f"""SELECT us.*, s.slug, s.name, s.name_ru, s.name_en,
                   s.summary_ru, s.summary_verified, s.summary_theoretical,
                   s.molecular_mechanisms, s.mechanisms,
                   sch.time_hhmm, sch.is_enabled,
                   sds.status AS today_status
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
        "LEFT JOIN users u ON u.id=us.user_id "
        "LEFT JOIN supplement_day_state sds ON sds.user_id=u.tg_user_id "
        "AND sds.supplement_id=us.supplement_id AND sds.day=?"
    )
    rows = await fetchall(
        conn,
        f"""SELECT us.*, s.id AS supplement_id, s.slug, s.name, s.name_ru, s.name_en,
                  s.summary_ru, s.summary_verified, s.summary_theoretical,
                  s.molecular_mechanisms, s.mechanisms,
                  sds.status AS today_status
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

async def set_user_supp_dose_legacy(
    conn: aiosqlite.Connection,
    user_id: int,
    supplement_id: int,
    dose_text: str | None,
) -> None:
    await set_user_supplement_dose(conn, user_id, supplement_id, dose_text)

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

async def list_user_supps(conn: aiosqlite.Connection, user_id: int):
    return await list_user_supplements(conn, user_id, limit=1000)

async def list_user_stack_details(conn: aiosqlite.Connection, tg_user_id: int, local_date: str | None = None):
    user_id = await get_or_create_user(conn, tg_user_id)
    params: list = [local_date or "", user_id]
    status_join = (
        "LEFT JOIN users u ON u.id=us.user_id "
        "LEFT JOIN supplement_day_state sds ON sds.user_id=u.tg_user_id "
        "AND sds.supplement_id=us.supplement_id AND sds.day=?"
    )
    return await fetchall(
        conn,
        f"""SELECT us.supplement_id,
                   COALESCE(NULLIF(uss.dose_text, ''), us.dose_text) AS dose_text,
                   us.note_text, us.is_active,
                   s.name, s.slug,
                   sch.time_hhmm,
                   sds.status AS today_status
            FROM user_supplements us
            JOIN supplements s ON s.id=us.supplement_id
            LEFT JOIN user_supp_settings uss ON uss.user_id=us.user_id AND uss.supplement_id=us.supplement_id
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

# new reminders system
async def upsert_reminder(
    conn: aiosqlite.Connection,
    user_id: int,
    supplement_id: int,
    time_local: str,
    enabled: int = 1,
) -> None:
    await conn.execute(
        """INSERT INTO reminders(user_id, supplement_id, time_local, enabled)
           VALUES(?,?,?,?)
           ON CONFLICT(user_id, supplement_id, time_local) DO UPDATE SET
             enabled=excluded.enabled""",
        (user_id, supplement_id, time_local, int(enabled)),
    )
    await conn.commit()

async def delete_reminder(
    conn: aiosqlite.Connection,
    user_id: int,
    supplement_id: int,
    time_local: str,
) -> None:
    await conn.execute(
        "DELETE FROM reminders WHERE user_id=? AND supplement_id=? AND time_local=?",
        (user_id, supplement_id, time_local),
    )
    await conn.commit()

async def delete_reminders_for_supplement(
    conn: aiosqlite.Connection,
    user_id: int,
    supplement_id: int,
) -> None:
    await conn.execute(
        "DELETE FROM reminders WHERE user_id=? AND supplement_id=?",
        (user_id, supplement_id),
    )
    await conn.commit()

async def list_reminders_for_user(conn: aiosqlite.Connection, user_id: int):
    return await fetchall(
        conn,
        """SELECT * FROM reminders
           WHERE user_id=?
           ORDER BY time_local""",
        (user_id,),
    )

async def list_due_reminders(conn: aiosqlite.Connection):
    rows = await fetchall(
        conn,
        """SELECT r.*, u.tz_name
           FROM reminders r
           JOIN users u ON u.tg_user_id=r.user_id
           WHERE r.enabled=1""",
    )
    due = []
    for row in rows:
        tz_name = str(row["tz_name"] or "").strip()
        local_now = datetime.now(parse_utc_tz(tz_name))
        if local_now.strftime("%H:%M") != str(row["time_local"] or ""):
            continue
        due.append(
            {
                "user_id": int(row["user_id"]),
                "supplement_id": int(row["supplement_id"]),
                "time_local": str(row["time_local"]),
                "day": local_now.date().isoformat(),
                "tz_name": tz_name,
            }
        )
    return due

async def insert_reminder_fire(
    conn: aiosqlite.Connection,
    user_id: int,
    supplement_id: int,
    day: str,
    time_local: str,
) -> bool:
    before = conn.total_changes
    await conn.execute(
        """INSERT OR IGNORE INTO reminder_fires(user_id, supplement_id, day, time_local, fired_at)
           VALUES(?,?,?,?,datetime('now'))""",
        (user_id, supplement_id, day, time_local),
    )
    await conn.commit()
    return conn.total_changes > before

async def save_ui_state(
    conn: aiosqlite.Connection,
    user_id: int,
    message_id: int | None,
    screen: str,
    payload_json: str,
) -> None:
    await conn.execute(
        """INSERT INTO ui_state(user_id, last_message_id, last_screen, last_payload, updated_at)
           VALUES(?,?,?,?,datetime('now'))
           ON CONFLICT(user_id) DO UPDATE SET
             last_message_id=excluded.last_message_id,
             last_screen=excluded.last_screen,
             last_payload=excluded.last_payload,
             updated_at=excluded.updated_at""",
        (user_id, message_id, screen, payload_json),
    )
    await conn.commit()

async def update_ui_state_payload(
    conn: aiosqlite.Connection,
    user_id: int,
    payload_json: str,
) -> None:
    await conn.execute(
        "UPDATE ui_state SET last_payload=?, updated_at=datetime('now') WHERE user_id=?",
        (payload_json, user_id),
    )
    await conn.commit()

async def get_ui_state(conn: aiosqlite.Connection, user_id: int) -> tuple[int | None, str, str]:
    row = await fetchone(
        conn,
        "SELECT last_message_id, last_screen, last_payload FROM ui_state WHERE user_id=?",
        (user_id,),
    )
    if not row:
        return (None, "", "")
    return (
        int(row["last_message_id"]) if row["last_message_id"] is not None else None,
        str(row["last_screen"] or ""),
        str(row["last_payload"] or ""),
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
    # LEGACY API: intake status source of truth is supplement_day_state.
    return await get_day_status(conn, tg_user_id, local_date, supplement_id)

async def set_today_intake_status(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    supplement_id: int,
    local_date: str,
    status: str | None,
) -> None:
    # LEGACY API: writes are redirected to supplement_day_state only.
    await set_day_status(conn, tg_user_id, local_date, supplement_id, status)


async def set_day_status(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    day: str,
    supplement_id: int,
    status: str | None,
) -> None:
    if status not in {"taken", "skipped", "none", None}:
        raise ValueError("status must be one of: taken, skipped, none")
    normalized = None if status in {None, "none"} else status
    if normalized is None:
        await conn.execute(
            "DELETE FROM supplement_day_state WHERE user_id=? AND supplement_id=? AND day=?",
            (tg_user_id, supplement_id, day),
        )
        await conn.commit()
        return
    await conn.execute(
        """INSERT INTO supplement_day_state(user_id, supplement_id, day, status, dose_override)
           VALUES(?,?,?,?,NULL)
           ON CONFLICT(user_id, supplement_id, day) DO UPDATE SET
             status=excluded.status""",
        (tg_user_id, supplement_id, day, normalized),
    )
    await conn.commit()


async def get_day_status(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    day: str,
    supplement_id: int,
) -> str | None:
    row = await fetchone(
        conn,
        """SELECT status FROM supplement_day_state
           WHERE user_id=? AND supplement_id=? AND day=?""",
        (tg_user_id, supplement_id, day),
    )
    return str(row["status"]) if row else None


async def get_day_status_map(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    day: str,
) -> dict[int, str]:
    rows = await fetchall(
        conn,
        "SELECT supplement_id, status FROM supplement_day_state WHERE user_id=? AND day=?",
        (tg_user_id, day),
    )
    out: dict[int, str] = {}
    for row in rows:
        sid = int(row["supplement_id"])
        status = str(row["status"] or "")
        if status in {"taken", "skipped"}:
            out[sid] = status
    return out

async def set_today_status(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    supplement_id: int,
    status: str,
) -> None:
    day = await ensure_daily_rollover(conn, tg_user_id)
    await set_day_status(conn, tg_user_id, day, supplement_id, status)

async def get_today_status(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    supplement_id: int,
) -> str | None:
    day = await ensure_daily_rollover(conn, tg_user_id)
    return await get_day_status(conn, tg_user_id, day, supplement_id)

async def list_supplement_day_states(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    supplement_ids: list[int],
    start_day: str,
    end_day: str,
) -> list[dict]:
    if not supplement_ids:
        return []
    placeholders = ",".join(["?"] * len(supplement_ids))
    query = (
        "SELECT supplement_id, day, status FROM supplement_day_state "
        f"WHERE user_id=? AND supplement_id IN ({placeholders}) AND day>=? AND day<=? "
        "ORDER BY day"
    )
    params = [tg_user_id, *supplement_ids, start_day, end_day]
    return await fetchall(conn, query, tuple(params))

async def clear_today_status(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    supplement_id: int,
) -> None:
    day = await ensure_daily_rollover(conn, tg_user_id)
    await set_day_status(conn, tg_user_id, day, supplement_id, None)

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

# checkins + day plan
async def get_user_by_tg_id(conn: aiosqlite.Connection, tg_id: int) -> dict | None:
    row = await fetchone(conn, "SELECT * FROM users WHERE tg_user_id=?", (tg_id,))
    return dict(row) if row else None

async def upsert_daily_checkin(
    conn: aiosqlite.Connection,
    *,
    user_id: int,
    date_local: str,
    tz_name: str,
    answers: dict,
) -> None:
    await conn.execute(
        """INSERT INTO daily_checkins(user_id, date_local, tz_name, answers_json, created_at_utc)
           VALUES(?,?,?,?,?)
           ON CONFLICT(user_id, date_local) DO UPDATE SET
             tz_name=excluded.tz_name,
             answers_json=excluded.answers_json,
             created_at_utc=excluded.created_at_utc""",
        (
            user_id,
            date_local,
            tz_name,
            json.dumps(answers, ensure_ascii=False),
            utc_now_iso(),
        ),
    )
    await conn.commit()

async def list_recent_checkins(conn: aiosqlite.Connection, user_id: int, limit: int = 3):
    return await fetchall(
        conn,
        "SELECT * FROM daily_checkins WHERE user_id=? ORDER BY created_at_utc DESC LIMIT ?",
        (user_id, limit),
    )



async def list_recent_checkins_window(
    conn: aiosqlite.Connection,
    user_id: int,
    start_day: str,
    end_day: str,
    limit: int = 14,
):
    return await fetchall(
        conn,
        """SELECT * FROM daily_checkins
           WHERE user_id=? AND date_local>=? AND date_local<=?
           ORDER BY date_local DESC
           LIMIT ?""",
        (user_id, start_day, end_day, limit),
    )


async def list_day_plan_history(
    conn: aiosqlite.Connection,
    user_id: int,
    start_day: str,
    end_day: str,
    limit: int = 120,
) -> list[dict]:
    rows = await fetchall(
        conn,
        """SELECT dpi.date_local, dpi.supplement_id, dpi.action, dpi.dose_text, dpi.time_hint,
                  s.slug, s.name_ru, s.name_en, s.name,
                  sds.status
           FROM day_plan_items dpi
           JOIN supplements s ON s.id=dpi.supplement_id
           JOIN users u ON u.id=dpi.user_id
           LEFT JOIN supplement_day_state sds
             ON sds.user_id=u.tg_user_id AND sds.supplement_id=dpi.supplement_id AND sds.day=dpi.date_local
           WHERE dpi.user_id=? AND dpi.date_local>=? AND dpi.date_local<=?
           ORDER BY dpi.date_local DESC, dpi.id DESC
           LIMIT ?""",
        (user_id, start_day, end_day, limit),
    )
    return [dict(row) for row in rows]
async def get_user_base_stack(conn: aiosqlite.Connection, user_id: int) -> list[dict]:
    rows = await fetchall(
        conn,
        """SELECT us.supplement_id, us.dose_text,
                  s.slug, s.name_ru, s.name_en, s.name,
                  sch.time_hhmm
           FROM user_supplements us
           JOIN supplements s ON s.id=us.supplement_id
           LEFT JOIN user_schedules sch ON sch.user_id=us.user_id AND sch.supplement_id=us.supplement_id
           WHERE us.user_id=? AND us.is_active=1
           ORDER BY s.name""",
        (user_id,),
    )
    items = []
    for row in rows:
        name_ru = (row["name_ru"] or row["name_en"] or row["name"] or "").strip()
        items.append(
            {
                "supplement_id": int(row["supplement_id"]),
                "slug": str(row["slug"] or "").strip(),
                "ru_name": name_ru,
                "dose_text": str(row["dose_text"] or "").strip(),
                "time_local": str(row["time_hhmm"] or "").strip(),
            }
        )
    return items

async def get_user_slots(conn: aiosqlite.Connection, user_id: int) -> list[dict]:
    row = await fetchone(
        conn,
        "SELECT slots_json FROM user_schedule_slots WHERE user_id=?",
        (user_id,),
    )
    if not row:
        slots = default_user_slots()
        await conn.execute(
            "INSERT INTO user_schedule_slots(user_id, slots_json) VALUES(?,?)",
            (user_id, json.dumps(slots, ensure_ascii=False)),
        )
        await conn.commit()
        return slots
    try:
        slots_raw = json.loads(str(row["slots_json"] or "[]"))
    except json.JSONDecodeError:
        slots_raw = []
    slots = _normalize_slots(slots_raw)
    if slots != slots_raw:
        await set_user_slots(conn, user_id, slots)
    return slots


async def set_user_slots(conn: aiosqlite.Connection, user_id: int, slots_list: list[dict]) -> None:
    slots = _normalize_slots(slots_list)
    await conn.execute(
        """INSERT INTO user_schedule_slots(user_id, slots_json)
           VALUES(?,?)
           ON CONFLICT(user_id) DO UPDATE SET
             slots_json=excluded.slots_json""",
        (user_id, json.dumps(slots, ensure_ascii=False)),
    )
    await conn.commit()


async def upsert_intake_log(
    conn: aiosqlite.Connection,
    user_id: int,
    date_local: str,
    slot_id: str,
    taken_slugs: list[str],
    skipped_slugs: list[str],
    note: str = "",
) -> None:
    now = utc_now_iso()
    await conn.execute(
        """INSERT INTO intake_log(user_id, date_local, slot_id, taken_slugs_json, skipped_slugs_json, note, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(user_id, date_local, slot_id) DO UPDATE SET
             taken_slugs_json=excluded.taken_slugs_json,
             skipped_slugs_json=excluded.skipped_slugs_json,
             note=excluded.note,
             updated_at=excluded.updated_at""",
        (
            user_id,
            date_local,
            slot_id,
            json.dumps(sorted(set(taken_slugs)), ensure_ascii=False),
            json.dumps(sorted(set(skipped_slugs)), ensure_ascii=False),
            note or "",
            now,
            now,
        ),
    )
    await conn.commit()


async def get_intake_log(conn: aiosqlite.Connection, user_id: int, date_local: str) -> dict[str, dict]:
    rows = await fetchall(
        conn,
        "SELECT * FROM intake_log WHERE user_id=? AND date_local=?",
        (user_id, date_local),
    )
    out: dict[str, dict] = {}
    for row in rows:
        try:
            taken = json.loads(str(row["taken_slugs_json"] or "[]"))
        except json.JSONDecodeError:
            taken = []
        try:
            skipped = json.loads(str(row["skipped_slugs_json"] or "[]"))
        except json.JSONDecodeError:
            skipped = []
        slot_id = str(row["slot_id"] or "")
        out[slot_id] = {
            "id": int(row["id"]),
            "user_id": int(row["user_id"]),
            "date_local": str(row["date_local"] or ""),
            "slot_id": slot_id,
            "taken_slugs": taken if isinstance(taken, list) else [],
            "skipped_slugs": skipped if isinstance(skipped, list) else [],
            "note": str(row["note"] or ""),
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }
    return out


async def get_or_create_intake_log_slot(
    conn: aiosqlite.Connection,
    user_id: int,
    date_local: str,
    slot_id: str,
) -> dict:
    logs = await get_intake_log(conn, user_id, date_local)
    if slot_id in logs:
        return logs[slot_id]
    await upsert_intake_log(conn, user_id, date_local, slot_id, [], [], note="")
    logs = await get_intake_log(conn, user_id, date_local)
    return logs.get(slot_id, {
        "user_id": user_id,
        "date_local": date_local,
        "slot_id": slot_id,
        "taken_slugs": [],
        "skipped_slugs": [],
        "note": "",
        "created_at": "",
        "updated_at": "",
    })


async def get_catalog_candidates(conn: aiosqlite.Connection, goals: list[str], limit: int = 60) -> list[dict]:
    keywords_map = {
        "focus": ["концентра", "вниман", "focus", "attention", "ноотроп"],
        "energy": ["энерг", "energy", "бодрост"],
        "training": ["трениров", "вынослив", "performance", "спорт"],
        "sleep": ["сон", "sleep", "мелатон", "засып"],
        "calm": ["стресс", "спокой", "calm", "anxiety", "релакс"],
        "recovery": ["восстанов", "recovery", "мышц"],
        "gut": ["жкт", "киш", "gut", "digest"],
        "mood": ["настроен", "mood", "депресс", "серотон"],
    }
    keywords: list[str] = []
    for goal in goals:
        keywords.extend(keywords_map.get(goal, []))
    keywords = [kw.lower() for kw in keywords if kw]

    if not keywords:
        rows = await fetchall(
            conn,
            """SELECT slug, name_ru, name_en, name,
                      summary_ru, summary_verified, summary_theoretical,
                      tags, goals, mechanisms
               FROM supplements
               WHERE is_active=1
               ORDER BY COALESCE(NULLIF(name_ru, ''), NULLIF(name_en, ''), name)
               LIMIT ?""",
            (limit,),
        )
    else:
        fields = [
            "tags",
            "goals",
            "rules_tags",
            "summary_ru",
            "summary_verified",
            "summary_theoretical",
            "mechanisms",
            "search_blob",
        ]
        clauses = []
        params: list[str] = []
        for kw in keywords:
            for field in fields:
                clauses.append(f"LOWER(COALESCE({field}, '')) LIKE ?")
                params.append(f"%{kw}%")
        where = " OR ".join(clauses) if clauses else "1=1"
        sql = f"""SELECT slug, name_ru, name_en, name,
                         summary_ru, summary_verified, summary_theoretical,
                         tags, goals, mechanisms
                  FROM supplements
                  WHERE is_active=1 AND ({where})
                  ORDER BY COALESCE(NULLIF(name_ru, ''), NULLIF(name_en, ''), name)
                  LIMIT ?"""
        params.append(limit)
        rows = await fetchall(conn, sql, tuple(params))

    candidates = []
    for row in rows:
        name_ru = (row["name_ru"] or row["name_en"] or row["name"] or "").strip()
        hint = (
            str(row["summary_ru"] or "").strip()
            or str(row["summary_verified"] or "").strip()
            or str(row["summary_theoretical"] or "").strip()
            or str(row["goals"] or "").strip()
            or str(row["tags"] or "").strip()
            or str(row["mechanisms"] or "").strip()
        )
        if hint:
            hint = hint.replace("\n", " ")
            hint = hint[:140]
        candidates.append(
            {
                "slug": str(row["slug"] or "").strip(),
                "ru_name": name_ru,
                "hint": hint,
            }
        )
    return candidates[:limit]

async def clear_day_plan(conn: aiosqlite.Connection, user_id: int, date_local: str) -> None:
    await conn.execute(
        "DELETE FROM day_plan_items WHERE user_id=? AND date_local=?",
        (user_id, date_local),
    )
    await conn.commit()

async def list_day_plan_items(conn: aiosqlite.Connection, user_id: int, date_local: str) -> list[dict]:
    rows = await fetchall(
        conn,
        """SELECT dpi.*, s.slug, s.name_ru, s.name_en, s.name
           FROM day_plan_items dpi
           JOIN supplements s ON s.id=dpi.supplement_id
           WHERE dpi.user_id=? AND dpi.date_local=?
           ORDER BY dpi.id""",
        (user_id, date_local),
    )
    return [dict(row) for row in rows]

async def list_day_plan_items_with_status(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    date_local: str,
) -> list[dict]:
    user_id = await get_or_create_user(conn, tg_user_id)
    rows = await fetchall(
        conn,
        """SELECT dpi.*, s.slug, s.name_ru, s.name_en, s.name,
                  sds.status AS today_status
           FROM day_plan_items dpi
           JOIN supplements s ON s.id=dpi.supplement_id
           LEFT JOIN supplement_day_state sds
             ON sds.user_id=? AND sds.supplement_id=dpi.supplement_id AND sds.day=dpi.date_local
           WHERE dpi.user_id=? AND dpi.date_local=?
           ORDER BY dpi.id""",
        (tg_user_id, user_id, date_local),
    )
    return [dict(row) for row in rows]

async def apply_day_plan_all(conn: aiosqlite.Connection, user_id: int, date_local: str, ai_actions: dict) -> int:
    await clear_day_plan(conn, user_id, date_local)
    return await _apply_day_plan_actions(conn, user_id, date_local, ai_actions)

async def apply_day_plan_selective(
    conn: aiosqlite.Connection,
    user_id: int,
    date_local: str,
    selected_action_ids: list[str],
    ai_actions: dict,
) -> int:
    await clear_day_plan(conn, user_id, date_local)
    return await _apply_day_plan_actions(
        conn,
        user_id,
        date_local,
        ai_actions,
        selected_ids=set(selected_action_ids),
    )

async def _apply_day_plan_actions(
    conn: aiosqlite.Connection,
    user_id: int,
    date_local: str,
    ai_actions: dict,
    *,
    selected_ids: set[str] | None = None,
) -> int:
    actions = ai_actions.get("actions") if isinstance(ai_actions, dict) else None
    if not isinstance(actions, list):
        return 0
    inserted = 0
    for action in actions:
        if not isinstance(action, dict):
            continue
        action_id = str(action.get("id") or "").strip()
        if selected_ids is not None and action_id not in selected_ids:
            continue
        slug = str(action.get("slug") or "").strip()
        supplement_id = action.get("supplement_id")
        if not supplement_id and slug:
            row = await get_supplement_by_slug(conn, slug)
            supplement_id = int(row["id"]) if row else None
        if not supplement_id:
            continue
        await conn.execute(
            """INSERT OR IGNORE INTO day_plan_items(
                   user_id, date_local, supplement_id, action, dose_text, time_hint, reason, source, created_at_utc
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                user_id,
                date_local,
                int(supplement_id),
                str(action.get("type") or "").strip(),
                str(action.get("dose_text") or "").strip() or None,
                str(action.get("time_hint") or "").strip() or None,
                str(action.get("reason_short_ru") or "").strip() or None,
                "ai",
                utc_now_iso(),
            ),
        )
        inserted += 1
    await conn.commit()
    return inserted
