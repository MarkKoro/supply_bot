from __future__ import annotations
import calendar
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


ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_EDITOR = "editor"
ROLE_SUPPORT = "support"
ROLE_NONE = "none"

ALL_ROLES = {ROLE_OWNER, ROLE_ADMIN, ROLE_EDITOR, ROLE_SUPPORT, ROLE_NONE}

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "content_edit": {ROLE_OWNER, ROLE_ADMIN, ROLE_EDITOR},
    "content_bulk": {ROLE_OWNER, ROLE_ADMIN},
    "users_view": {ROLE_OWNER, ROLE_ADMIN, ROLE_SUPPORT},
    "users_ban": {ROLE_OWNER, ROLE_ADMIN, ROLE_SUPPORT},
    "users_delete": {ROLE_OWNER, ROLE_ADMIN},
    "roles_manage": {ROLE_OWNER},
    "payments_view": {ROLE_OWNER, ROLE_ADMIN},
    "system_diag": {ROLE_OWNER, ROLE_ADMIN},
}


def role_has(role: str, perm: str) -> bool:
    return str(role or ROLE_NONE) in ROLE_PERMISSIONS.get(str(perm or ""), set())


def role_perms(role: str) -> dict[str, bool]:
    safe_role = str(role or ROLE_NONE)
    return {perm: role_has(safe_role, perm) for perm in ROLE_PERMISSIONS}

SCHEMA_SQL = """PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  tg_user_id    INTEGER UNIQUE NOT NULL,
  tz_name       TEXT NOT NULL DEFAULT 'UTC',
  created_at    TEXT NOT NULL,
  created_at_utc TEXT DEFAULT '',
  last_seen_utc TEXT DEFAULT '',
  tg_username   TEXT DEFAULT '',
  tg_first_name TEXT DEFAULT '',
  tg_last_name  TEXT DEFAULT '',
  is_banned     INTEGER DEFAULT 0,
  banned_reason TEXT DEFAULT '',
  banned_at_utc TEXT DEFAULT '',
  deleted_at_utc TEXT DEFAULT '',
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
  is_active INTEGER NOT NULL DEFAULT 1,
  updated_at_utc TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS admin_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  admin_tg_id INTEGER NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id INTEGER NOT NULL,
  field TEXT NOT NULL,
  old_value TEXT,
  new_value TEXT,
  action TEXT NOT NULL,
  created_at_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_admin_audit_entity_created
  ON admin_audit(entity_type, entity_id, created_at_utc DESC);

CREATE TABLE IF NOT EXISTS user_roles (
  tg_user_id INTEGER PRIMARY KEY,
  role TEXT NOT NULL,
  granted_by_tg_id INTEGER NOT NULL,
  granted_at_utc TEXT NOT NULL,
  note TEXT DEFAULT ''
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
  ai_prompt_hash TEXT,
  ai_raw_json   TEXT,
  applied_json  TEXT,
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
  slot_id       TEXT,
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

CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  provider TEXT NOT NULL,
  currency TEXT NOT NULL,
  amount INTEGER NOT NULL,
  payload TEXT NOT NULL,
  telegram_payment_charge_id TEXT,
  provider_payment_charge_id TEXT,
  status TEXT NOT NULL,
  created_at_utc TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_tg_charge_unique
  ON payments(telegram_payment_charge_id)
  WHERE telegram_payment_charge_id IS NOT NULL AND telegram_payment_charge_id != '';

CREATE TABLE IF NOT EXISTS user_complexes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  note TEXT DEFAULT '',
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT DEFAULT '',
  UNIQUE(user_id, name)
);

CREATE TABLE IF NOT EXISTS user_complex_items (
  complex_id INTEGER NOT NULL,
  supplement_id INTEGER NOT NULL,
  dose_override TEXT DEFAULT '',
  sort_order INTEGER DEFAULT 0,
  PRIMARY KEY (complex_id, supplement_id)
);

CREATE TABLE IF NOT EXISTS user_complex_schedules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  complex_id INTEGER NOT NULL,
  slot_id TEXT NOT NULL,
  time_local TEXT DEFAULT '',
  is_enabled INTEGER DEFAULT 1,
  created_at_utc TEXT NOT NULL,
  UNIQUE(user_id, complex_id, slot_id)
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
        try:
            from settings import get_settings
            await bootstrap_roles_from_settings(conn, get_settings())
        except Exception:
            pass
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
    await _ensure_column(conn, "users", "premium_until_utc", "TEXT")
    await _ensure_column(conn, "users", "premium_plan", "TEXT")
    await _ensure_column(conn, "users", "premium_source", "TEXT")
    await _ensure_column(conn, "users", "premium_updated_at_utc", "TEXT")
    await conn.execute(
        """CREATE TABLE IF NOT EXISTS user_complexes (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL,
          name TEXT NOT NULL,
          note TEXT DEFAULT '',
          created_at_utc TEXT NOT NULL,
          updated_at_utc TEXT DEFAULT '',
          UNIQUE(user_id, name)
        )"""
    )
    await conn.execute(
        """CREATE TABLE IF NOT EXISTS user_complex_items (
          complex_id INTEGER NOT NULL,
          supplement_id INTEGER NOT NULL,
          dose_override TEXT DEFAULT '',
          sort_order INTEGER DEFAULT 0,
          PRIMARY KEY (complex_id, supplement_id)
        )"""
    )
    await conn.execute(
        """CREATE TABLE IF NOT EXISTS user_complex_schedules (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL,
          complex_id INTEGER NOT NULL,
          slot_id TEXT NOT NULL,
          time_local TEXT DEFAULT '',
          is_enabled INTEGER DEFAULT 1,
          created_at_utc TEXT NOT NULL,
          UNIQUE(user_id, complex_id, slot_id)
        )"""
    )
    await _ensure_column(conn, "users", "created_at_utc", "TEXT DEFAULT ''")
    await _ensure_column(conn, "users", "last_seen_utc", "TEXT DEFAULT ''")
    await _ensure_column(conn, "users", "tg_username", "TEXT DEFAULT ''")
    await _ensure_column(conn, "users", "tg_first_name", "TEXT DEFAULT ''")
    await _ensure_column(conn, "users", "tg_last_name", "TEXT DEFAULT ''")
    await _ensure_column(conn, "users", "is_banned", "INTEGER DEFAULT 0")
    await _ensure_column(conn, "users", "banned_reason", "TEXT DEFAULT ''")
    await _ensure_column(conn, "users", "banned_at_utc", "TEXT DEFAULT ''")
    await _ensure_column(conn, "users", "deleted_at_utc", "TEXT DEFAULT ''")
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
    await _ensure_column(conn, "supplements", "purchase_url", "TEXT DEFAULT ''")
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
    await _ensure_column(conn, "supplements", "updated_at_utc", "TEXT NOT NULL DEFAULT ''")
    await _ensure_admin_audit(conn)
    await _ensure_user_roles(conn)
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
    await _ensure_payments_table(conn)
    await _ensure_achievements_tables(conn)
    await _ensure_integrity_unique_indexes(conn)
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
        "UPDATE supplements SET updated_at_utc='' WHERE updated_at_utc IS NULL"
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
        "UPDATE users SET created_at_utc=COALESCE(created_at,'') WHERE created_at_utc IS NULL OR created_at_utc=''"
    )
    await conn.execute(
        "UPDATE users SET last_seen_utc='' WHERE last_seen_utc IS NULL"
    )
    await conn.execute(
        "UPDATE users SET tg_username='' WHERE tg_username IS NULL"
    )
    await conn.execute(
        "UPDATE users SET tg_first_name='' WHERE tg_first_name IS NULL"
    )
    await conn.execute(
        "UPDATE users SET tg_last_name='' WHERE tg_last_name IS NULL"
    )
    await conn.execute(
        "UPDATE users SET is_banned=0 WHERE is_banned IS NULL"
    )
    await conn.execute(
        "UPDATE users SET banned_reason='' WHERE banned_reason IS NULL"
    )
    await conn.execute(
        "UPDATE users SET banned_at_utc='' WHERE banned_at_utc IS NULL"
    )
    await conn.execute(
        "UPDATE users SET deleted_at_utc='' WHERE deleted_at_utc IS NULL"
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
    await bootstrap_achievements(conn)

async def _ensure_achievements_tables(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        """CREATE TABLE IF NOT EXISTS achievements (
          key TEXT PRIMARY KEY,
          title_ru TEXT NOT NULL,
          desc_ru TEXT NOT NULL,
          category TEXT NOT NULL,
          rarity TEXT NOT NULL,
          points INTEGER NOT NULL DEFAULT 0,
          reward_json TEXT NOT NULL DEFAULT '{}',
          updated_at_utc TEXT NOT NULL DEFAULT ''
        )"""
    )
    await conn.execute(
        """CREATE TABLE IF NOT EXISTS user_achievements (
          user_id INTEGER NOT NULL,
          achievement_key TEXT NOT NULL,
          unlocked_at_utc TEXT NOT NULL,
          meta_json TEXT NOT NULL DEFAULT '{}',
          PRIMARY KEY(user_id, achievement_key)
        )"""
    )
    await conn.execute(
        """CREATE TABLE IF NOT EXISTS user_cosmetics (
          user_id INTEGER PRIMARY KEY,
          theme_id TEXT NOT NULL DEFAULT 'base',
          tab_skin_id TEXT NOT NULL DEFAULT 'classic',
          badge_id TEXT NOT NULL DEFAULT 'none',
          updated_at_utc TEXT NOT NULL DEFAULT ''
        )"""
    )
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_user_achievements_user ON user_achievements(user_id)")


async def bootstrap_achievements(conn: aiosqlite.Connection) -> None:
    from achievements import ACHIEVEMENTS

    now = utc_now_iso()
    for ach in ACHIEVEMENTS:
        reward_json = json.dumps(ach.get("reward") or {}, ensure_ascii=False)
        await conn.execute(
            """INSERT INTO achievements(key, title_ru, desc_ru, category, rarity, points, reward_json, updated_at_utc)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                 title_ru=excluded.title_ru,
                 desc_ru=excluded.desc_ru,
                 category=excluded.category,
                 rarity=excluded.rarity,
                 points=excluded.points,
                 reward_json=excluded.reward_json,
                 updated_at_utc=excluded.updated_at_utc""",
            (
                str(ach.get("key") or "").strip(),
                str(ach.get("title_ru") or ""),
                str(ach.get("desc_ru") or ""),
                str(ach.get("category") or ""),
                str(ach.get("rarity") or "common"),
                int(ach.get("points") or 0),
                reward_json,
                now,
            ),
        )


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
        await conn.execute("UPDATE user_schedules SET slot_id='morning' WHERE slot_id IS NULL OR slot_id=''")
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
              slot_id       TEXT,
              is_enabled    INTEGER NOT NULL DEFAULT 1,
              UNIQUE(user_id, supplement_id)
            )"""
        )
        await conn.execute(
            "UPDATE user_schedules SET slot_id='morning' WHERE slot_id IS NULL OR slot_id=''"
        )
        return

    await _ensure_column(conn, "user_schedules", "time_hhmm", "TEXT")
    cols = await _table_columns(conn, "user_schedules")
    if "slot_id" not in cols:
        await conn.execute("ALTER TABLE user_schedules ADD COLUMN slot_id TEXT")
    await conn.execute(
        "UPDATE user_schedules SET slot_id='morning' WHERE slot_id IS NULL OR slot_id=''"
    )
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
    exists = await _table_exists(conn, "user_schedule_slots")
    legacy_rows: list[aiosqlite.Row] = []
    if exists:
        cols = await _table_columns(conn, "user_schedule_slots")
        if "slots_json" in cols:
            legacy_rows = await fetchall(conn, "SELECT user_id, slots_json FROM user_schedule_slots")
            await conn.execute("ALTER TABLE user_schedule_slots RENAME TO user_schedule_slots_legacy")
            exists = False

    if not exists:
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS user_schedule_slots (
              user_id INTEGER NOT NULL,
              slot_id TEXT NOT NULL,
              title_ru TEXT NOT NULL DEFAULT '',
              time_local TEXT NOT NULL DEFAULT '08:00',
              is_enabled INTEGER NOT NULL DEFAULT 1,
              PRIMARY KEY(user_id, slot_id)
            )"""
        )

    cols = await _table_columns(conn, "user_schedule_slots")
    if "slot_id" not in cols:
        await conn.execute("ALTER TABLE user_schedule_slots ADD COLUMN slot_id TEXT")
    if "title_ru" not in cols:
        await conn.execute("ALTER TABLE user_schedule_slots ADD COLUMN title_ru TEXT NOT NULL DEFAULT ''")
    if "time_local" not in cols:
        await conn.execute("ALTER TABLE user_schedule_slots ADD COLUMN time_local TEXT NOT NULL DEFAULT '08:00'")
    if "is_enabled" not in cols:
        await conn.execute("ALTER TABLE user_schedule_slots ADD COLUMN is_enabled INTEGER NOT NULL DEFAULT 1")

    for row in legacy_rows:
        user_id = int(row["user_id"] or 0)
        try:
            parsed = json.loads(str(row["slots_json"] or "[]"))
        except json.JSONDecodeError:
            parsed = []
        for slot in _normalize_slots(parsed):
            await conn.execute(
                """INSERT OR REPLACE INTO user_schedule_slots(user_id, slot_id, title_ru, time_local, is_enabled)
                   VALUES(?,?,?,?,?)""",
                (
                    user_id,
                    str(slot.get("slot_id") or ""),
                    str(slot.get("title_ru") or "Слот"),
                    str(slot.get("time_local") or "08:00"),
                    1 if slot.get("is_enabled", True) else 0,
                ),
            )

    await conn.execute("DROP TABLE IF EXISTS user_schedule_slots_legacy")


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
    prompt_hash_expr = _pick_expr("ai_prompt_hash", [], "''")
    ai_raw_expr = _pick_expr("ai_raw_json", [], "''")
    applied_expr = _pick_expr("applied_json", [], "''")

    await conn.execute("DROP TABLE IF EXISTS daily_checkins__new")
    await conn.execute(
        """CREATE TABLE daily_checkins__new (
          id            INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          date_local    TEXT NOT NULL,
          tz_name       TEXT NOT NULL,
          answers_json  TEXT NOT NULL,
          ai_prompt_hash TEXT,
          ai_raw_json   TEXT,
          applied_json  TEXT,
          created_at_utc TEXT NOT NULL,
          UNIQUE(user_id, date_local)
        )"""
    )
    await conn.execute(
        f"""INSERT INTO daily_checkins__new(user_id, date_local, tz_name, answers_json, ai_prompt_hash, ai_raw_json, applied_json, created_at_utc)
           SELECT user_id,
                  {date_expr} AS date_local,
                  {tz_expr} AS tz_name,
                  {answers_expr} AS answers_json,
                  {prompt_hash_expr} AS ai_prompt_hash,
                  {ai_raw_expr} AS ai_raw_json,
                  {applied_expr} AS applied_json,
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
              ai_prompt_hash TEXT,
              ai_raw_json   TEXT,
              applied_json  TEXT,
              created_at_utc TEXT NOT NULL,
              UNIQUE(user_id, date_local)
            )"""
        )
        return
    await _ensure_column(conn, "daily_checkins", "date_local", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "daily_checkins", "tz_name", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "daily_checkins", "answers_json", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "daily_checkins", "ai_prompt_hash", "TEXT")
    await _ensure_column(conn, "daily_checkins", "ai_raw_json", "TEXT")
    await _ensure_column(conn, "daily_checkins", "applied_json", "TEXT")
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
    await _ensure_column(conn, "day_plan_items", "slot_id", "TEXT")
    await _ensure_column(conn, "day_plan_items", "time_local", "TEXT")
    await _ensure_column(conn, "day_plan_items", "reason", "TEXT")
    await _ensure_column(conn, "day_plan_items", "note", "TEXT")
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

async def _ensure_payments_table(conn: aiosqlite.Connection) -> None:
    table = await fetchone(
        conn,
        "SELECT name FROM sqlite_master WHERE type='table' AND name='payments'",
    )
    if not table:
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS payments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER NOT NULL,
              provider TEXT NOT NULL,
              currency TEXT NOT NULL,
              amount INTEGER NOT NULL,
              payload TEXT NOT NULL,
              telegram_payment_charge_id TEXT,
              provider_payment_charge_id TEXT,
              status TEXT NOT NULL,
              created_at_utc TEXT NOT NULL
            )"""
        )
        return

    await _ensure_column(conn, "payments", "user_id", "INTEGER NOT NULL DEFAULT 0")
    await _ensure_column(conn, "payments", "provider", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "payments", "currency", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "payments", "amount", "INTEGER NOT NULL DEFAULT 0")
    await _ensure_column(conn, "payments", "payload", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "payments", "telegram_payment_charge_id", "TEXT")
    await _ensure_column(conn, "payments", "provider_payment_charge_id", "TEXT")
    await _ensure_column(conn, "payments", "status", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "payments", "created_at_utc", "TEXT NOT NULL DEFAULT ''")
    await conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_tg_charge_unique
           ON payments(telegram_payment_charge_id)
           WHERE telegram_payment_charge_id IS NOT NULL AND telegram_payment_charge_id != ''"""
    )


async def _ensure_integrity_unique_indexes(conn: aiosqlite.Connection) -> None:
    await _dedupe_keep_latest(conn, "supplement_day_state", ["user_id", "day", "supplement_id"], "created_at")
    await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_sds ON supplement_day_state(user_id, day, supplement_id)")

    if await _table_exists(conn, "intake_log"):
        await _dedupe_keep_latest(conn, "intake_log", ["user_id", "date_local", "slot_id"], "updated_at")
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_rlog ON intake_log(user_id, date_local, slot_id)")

    if await _table_exists(conn, "user_complex_items"):
        await _dedupe_keep_latest(conn, "user_complex_items", ["complex_id", "supplement_id"], None)
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_citems ON user_complex_items(complex_id, supplement_id)")

    if await _table_exists(conn, "user_complex_schedules"):
        await _dedupe_keep_latest(conn, "user_complex_schedules", ["user_id", "complex_id", "slot_id"], "created_at_utc")
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_csched ON user_complex_schedules(user_id, complex_id, slot_id)")

    if await _table_exists(conn, "user_schedules"):
        await _dedupe_keep_latest(conn, "user_schedules", ["user_id", "supplement_id"], "id")
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_usched ON user_schedules(user_id, supplement_id)")


async def _dedupe_keep_latest(
    conn: aiosqlite.Connection,
    table: str,
    keys: list[str],
    order_col: str | None,
) -> None:
    cols = await _table_columns(conn, table)
    if not all(k in cols for k in keys):
        return
    if order_col and order_col not in cols:
        order_col = None
    group_expr = ", ".join(keys)
    if order_col:
        order_expr = f"CASE WHEN {order_col} IS NULL OR {order_col}='' THEN 0 ELSE 1 END DESC, {order_col} DESC, rowid DESC"
    else:
        order_expr = "rowid DESC"
    await conn.execute(
        f"""DELETE FROM {table}
           WHERE rowid IN (
             SELECT rowid FROM (
               SELECT rowid, ROW_NUMBER() OVER (PARTITION BY {group_expr} ORDER BY {order_expr}) AS rn
               FROM {table}
             ) t
             WHERE rn > 1
           )"""
    )


async def _ensure_user_roles(conn: aiosqlite.Connection) -> None:
    if not await _table_exists(conn, "user_roles"):
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS user_roles (
  tg_user_id INTEGER PRIMARY KEY,
  role TEXT NOT NULL,
  granted_by_tg_id INTEGER NOT NULL,
  granted_at_utc TEXT NOT NULL,
  note TEXT DEFAULT ''
)"""
        )
    await _ensure_column(conn, "user_roles", "tg_user_id", "INTEGER")
    await _ensure_column(conn, "user_roles", "role", "TEXT NOT NULL DEFAULT 'none'")
    await _ensure_column(conn, "user_roles", "granted_by_tg_id", "INTEGER NOT NULL DEFAULT 0")
    await _ensure_column(conn, "user_roles", "granted_at_utc", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "user_roles", "note", "TEXT DEFAULT ''")


async def get_role(conn: aiosqlite.Connection, tg_user_id: int) -> str:
    row = await fetchone(conn, "SELECT role FROM user_roles WHERE tg_user_id=? LIMIT 1", (int(tg_user_id),))
    if not row:
        return ROLE_NONE
    role = str(row["role"] or ROLE_NONE).strip().lower()
    return role if role in ALL_ROLES else ROLE_NONE


async def set_role(conn: aiosqlite.Connection, owner_tg_id: int, target_tg_id: int, role: str, note: str = "") -> None:
    safe_role = str(role or "").strip().lower()
    if safe_role not in ALL_ROLES or safe_role == ROLE_NONE:
        raise ValueError("invalid_role")
    now = utc_now_iso()
    await conn.execute(
        """INSERT INTO user_roles(tg_user_id, role, granted_by_tg_id, granted_at_utc, note)
           VALUES(?,?,?,?,?)
           ON CONFLICT(tg_user_id) DO UPDATE SET
             role=excluded.role,
             granted_by_tg_id=excluded.granted_by_tg_id,
             granted_at_utc=excluded.granted_at_utc,
             note=excluded.note""",
        (int(target_tg_id), safe_role, int(owner_tg_id), now, str(note or "")[:500]),
    )
    await conn.execute(
        """INSERT INTO admin_audit(admin_tg_id, entity_type, entity_id, field, old_value, new_value, action, created_at_utc)
           VALUES(?,?,?,?,?,?,?,?)""",
        (int(owner_tg_id), "user", int(target_tg_id), "role", None, safe_role, "set", now),
    )


async def remove_role(conn: aiosqlite.Connection, owner_tg_id: int, target_tg_id: int) -> None:
    prev = await get_role(conn, int(target_tg_id))
    await conn.execute("DELETE FROM user_roles WHERE tg_user_id=?", (int(target_tg_id),))
    now = utc_now_iso()
    await conn.execute(
        """INSERT INTO admin_audit(admin_tg_id, entity_type, entity_id, field, old_value, new_value, action, created_at_utc)
           VALUES(?,?,?,?,?,?,?,?)""",
        (int(owner_tg_id), "user", int(target_tg_id), "role", prev, ROLE_NONE, "clear", now),
    )


async def bootstrap_roles_from_settings(conn: aiosqlite.Connection, settings) -> None:
    owner_tg_id = int(getattr(settings, "OWNER_TG_ID", 0) or 0)
    admin_tg_ids = set(getattr(settings, "ADMIN_TG_IDS", set()) or set())
    if owner_tg_id > 0:
        await set_role(conn, owner_tg_id, owner_tg_id, ROLE_OWNER, "bootstrap owner")
    for admin_tg_id in admin_tg_ids:
        aid = int(admin_tg_id or 0)
        if aid <= 0:
            continue
        existing_role = await get_role(conn, aid)
        if existing_role == ROLE_NONE:
            actor = owner_tg_id if owner_tg_id > 0 else aid
            await set_role(conn, actor, aid, ROLE_ADMIN, "bootstrap admin")


async def _ensure_admin_audit(conn: aiosqlite.Connection) -> None:
    table = await fetchone(
        conn,
        "SELECT name FROM sqlite_master WHERE type='table' AND name='admin_audit'",
    )
    if not table:
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS admin_audit (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              admin_tg_id INTEGER NOT NULL,
              entity_type TEXT NOT NULL,
              entity_id INTEGER NOT NULL,
              field TEXT NOT NULL,
              old_value TEXT,
              new_value TEXT,
              action TEXT NOT NULL,
              created_at_utc TEXT NOT NULL
            )"""
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_admin_audit_entity_created ON admin_audit(entity_type, entity_id, created_at_utc DESC)"
        )
        return

    await _ensure_column(conn, "admin_audit", "admin_tg_id", "INTEGER NOT NULL DEFAULT 0")
    await _ensure_column(conn, "admin_audit", "entity_type", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "admin_audit", "entity_id", "INTEGER NOT NULL DEFAULT 0")
    await _ensure_column(conn, "admin_audit", "field", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "admin_audit", "old_value", "TEXT")
    await _ensure_column(conn, "admin_audit", "new_value", "TEXT")
    await _ensure_column(conn, "admin_audit", "action", "TEXT NOT NULL DEFAULT ''")
    await _ensure_column(conn, "admin_audit", "created_at_utc", "TEXT NOT NULL DEFAULT ''")
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_admin_audit_entity_created ON admin_audit(entity_type, entity_id, created_at_utc DESC)"
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

async def touch_user(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    username: str | None,
    first: str | None,
    last: str | None,
) -> int:
    user_id = await get_or_create_user(conn, int(tg_user_id), tz_name="")
    now = utc_now_iso()
    row = await fetchone(
        conn,
        "SELECT created_at_utc, tg_username, tg_first_name, tg_last_name FROM users WHERE tg_user_id=?",
        (int(tg_user_id),),
    )
    created_at_utc = str((row["created_at_utc"] if row else "") or "").strip()
    if not created_at_utc:
        await conn.execute(
            "UPDATE users SET created_at_utc=COALESCE(NULLIF(created_at_utc,''), NULLIF(created_at,''), ?), last_seen_utc=? WHERE tg_user_id=?",
            (now, now, int(tg_user_id)),
        )
    else:
        await conn.execute(
            "UPDATE users SET last_seen_utc=? WHERE tg_user_id=?",
            (now, int(tg_user_id)),
        )

    updates: list[str] = []
    params: list[object] = []
    if username is not None:
        safe_username = str(username).strip()
        if row is None or safe_username != str(row["tg_username"] or ""):
            updates.append("tg_username=?")
            params.append(safe_username)
    if first is not None:
        safe_first = str(first).strip()
        if row is None or safe_first != str(row["tg_first_name"] or ""):
            updates.append("tg_first_name=?")
            params.append(safe_first)
    if last is not None:
        safe_last = str(last).strip()
        if row is None or safe_last != str(row["tg_last_name"] or ""):
            updates.append("tg_last_name=?")
            params.append(safe_last)

    if updates:
        params.append(int(tg_user_id))
        await conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE tg_user_id=?", tuple(params))

    await conn.commit()
    return int(user_id)


async def get_user_status(conn: aiosqlite.Connection, tg_user_id: int) -> dict:
    row = await fetchone(
        conn,
        """SELECT is_banned, deleted_at_utc, banned_reason, premium_until_utc, created_at_utc, last_seen_utc
           FROM users
           WHERE tg_user_id=?""",
        (int(tg_user_id),),
    )
    if not row:
        return {
            "is_banned": 0,
            "deleted_at_utc": "",
            "banned_reason": "",
            "premium_until_utc": None,
            "created_at_utc": "",
            "last_seen_utc": "",
        }
    return {
        "is_banned": int(row["is_banned"] or 0),
        "deleted_at_utc": str(row["deleted_at_utc"] or ""),
        "banned_reason": str(row["banned_reason"] or ""),
        "premium_until_utc": row["premium_until_utc"],
        "created_at_utc": str(row["created_at_utc"] or ""),
        "last_seen_utc": str(row["last_seen_utc"] or ""),
    }


async def admin_get_user(conn: aiosqlite.Connection, tg_user_id: int) -> dict | None:
    row = await fetchone(
        conn,
        """SELECT id, tg_user_id, tg_username, tg_first_name, created_at_utc, last_seen_utc,
                  is_banned, banned_reason, banned_at_utc, deleted_at_utc, premium_until_utc
           FROM users WHERE tg_user_id=?""",
        (int(tg_user_id),),
    )
    if not row:
        return None
    paid_row = await fetchone(
        conn,
        "SELECT COUNT(*) AS cnt FROM payments WHERE user_id=? AND status='paid'",
        (int(row["id"]),),
    )
    return {
        "tg_user_id": int(row["tg_user_id"]),
        "tg_username": str(row["tg_username"] or ""),
        "tg_first_name": str(row["tg_first_name"] or ""),
        "created_at_utc": str(row["created_at_utc"] or ""),
        "last_seen_utc": str(row["last_seen_utc"] or ""),
        "is_banned": int(row["is_banned"] or 0),
        "banned_reason": str(row["banned_reason"] or ""),
        "banned_at_utc": str(row["banned_at_utc"] or ""),
        "deleted_at_utc": str(row["deleted_at_utc"] or ""),
        "premium_until_utc": row["premium_until_utc"],
        "payments_paid_count": int((paid_row["cnt"] if paid_row else 0) or 0),
    }


async def admin_set_ban(
    conn: aiosqlite.Connection,
    admin_tg_id: int,
    tg_user_id: int,
    is_banned: int,
    reason: str = "",
) -> None:
    row = await fetchone(
        conn,
        "SELECT is_banned, banned_reason FROM users WHERE tg_user_id=?",
        (int(tg_user_id),),
    )
    if not row:
        raise ValueError("USER_NOT_FOUND")
    prev = int(row["is_banned"] or 0)
    next_value = 1 if int(is_banned) else 0
    safe_reason = str(reason or "").strip()
    banned_at = utc_now_iso() if next_value else ""
    await conn.execute(
        "UPDATE users SET is_banned=?, banned_reason=?, banned_at_utc=? WHERE tg_user_id=?",
        (next_value, safe_reason if next_value else "", banned_at, int(tg_user_id)),
    )
    await conn.execute(
        """INSERT INTO admin_audit(admin_tg_id, entity_type, entity_id, field, old_value, new_value, action, created_at_utc)
           VALUES(?,?,?,?,?,?,?,?)""",
        (int(admin_tg_id), "user", int(tg_user_id), "is_banned", str(prev), str(next_value), "set", utc_now_iso()),
    )
    await conn.commit()


async def admin_delete_user(conn: aiosqlite.Connection, admin_tg_id: int, tg_user_id: int) -> None:
    row = await fetchone(conn, "SELECT id, deleted_at_utc FROM users WHERE tg_user_id=?", (int(tg_user_id),))
    if not row:
        raise ValueError("USER_NOT_FOUND")
    user_id = int(row["id"])
    now = utc_now_iso()
    await conn.execute(
        "UPDATE users SET deleted_at_utc=?, is_banned=1, banned_reason=COALESCE(NULLIF(banned_reason,''),'deleted'), banned_at_utc=COALESCE(NULLIF(banned_at_utc,''),?) WHERE tg_user_id=?",
        (now, now, int(tg_user_id)),
    )
    for table in (
        "supplement_day_state",
        "day_plan_items",
        "daily_checkins",
        "user_schedules",
        "user_supplements",
        "user_schedule_slots",
        "user_intake_log",
        "user_supp_settings",
        "reminders",
        "reminder_fires",
        "intake_log",
        "ui_state",
    ):
        await conn.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
    await conn.execute(
        """INSERT INTO admin_audit(admin_tg_id, entity_type, entity_id, field, old_value, new_value, action, created_at_utc)
           VALUES(?,?,?,?,?,?,?,?)""",
        (int(admin_tg_id), "user", int(tg_user_id), "deleted_at_utc", str(row["deleted_at_utc"] or ""), now, "delete", now),
    )
    await conn.commit()


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

def _parse_utc_iso_dt(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        candidate = raw.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

def _to_utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        value = dt.replace(tzinfo=timezone.utc)
    else:
        value = dt.astimezone(timezone.utc)
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")

def _add_months_utc(dt: datetime, months: int) -> datetime:
    if months < 0:
        raise ValueError("months must be >= 0")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    target_month = dt.month - 1 + months
    year = dt.year + target_month // 12
    month = target_month % 12 + 1
    max_day = calendar.monthrange(year, month)[1]
    day = min(dt.day, max_day)
    return dt.replace(year=year, month=month, day=day)

async def get_premium_until(conn: aiosqlite.Connection, tg_user_id: int) -> str | None:
    row = await fetchone(conn, "SELECT premium_until_utc FROM users WHERE tg_user_id=?", (tg_user_id,))
    if not row:
        return None
    value = str(row["premium_until_utc"] or "").strip()
    return value or None

async def is_premium_active(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    now_utc: datetime | None = None,
) -> bool:
    premium_until_iso = await get_premium_until(conn, tg_user_id)
    premium_until = _parse_utc_iso_dt(premium_until_iso)
    if premium_until is None:
        return False
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    return premium_until > now

async def set_premium_until(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    until_utc: datetime,
    source: str,
    plan_id: str,
) -> str:
    await get_or_create_user(conn, tg_user_id)
    until_value = _to_utc_iso(until_utc)
    await conn.execute(
        """UPDATE users
           SET premium_until_utc=?, premium_source=?, premium_plan=?, premium_updated_at_utc=?
           WHERE tg_user_id=?""",
        (until_value, source, plan_id, utc_now_iso(), tg_user_id),
    )
    await conn.commit()
    return until_value

async def extend_premium(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    months: int,
    source: str,
    plan_id: str,
) -> str:
    if months <= 0:
        raise ValueError("months must be > 0")
    now = datetime.now(timezone.utc)
    current_until = _parse_utc_iso_dt(await get_premium_until(conn, tg_user_id))
    baseline = now
    if current_until is not None and current_until > now:
        baseline = current_until
    new_until = _add_months_utc(baseline, months)
    return await set_premium_until(conn, tg_user_id, new_until, source, plan_id)

async def log_payment_paid(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    provider: str,
    currency: str,
    amount: int,
    payload: str,
    telegram_payment_charge_id: str | None = None,
    provider_payment_charge_id: str | None = None,
) -> bool:
    created = utc_now_iso()
    if telegram_payment_charge_id:
        existing = await fetchone(
            conn,
            "SELECT id FROM payments WHERE telegram_payment_charge_id=?",
            (telegram_payment_charge_id,),
        )
        if existing:
            return False
    user_id = await get_or_create_user(conn, tg_user_id)
    try:
        await conn.execute(
            """INSERT INTO payments(
                   user_id, provider, currency, amount, payload,
                   telegram_payment_charge_id, provider_payment_charge_id, status, created_at_utc
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                user_id,
                provider,
                currency,
                amount,
                payload,
                telegram_payment_charge_id,
                provider_payment_charge_id,
                "paid",
                created,
            ),
        )
        await conn.commit()
        return True
    except aiosqlite.IntegrityError:
        if telegram_payment_charge_id:
            return False
        raise

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
    {"slot_id": "morning", "title_ru": "Утро", "time_local": "08:00", "is_enabled": True},
    {"slot_id": "noon", "title_ru": "День", "time_local": "14:00", "is_enabled": True},
    {"slot_id": "evening", "title_ru": "Вечер", "time_local": "19:00", "is_enabled": True},
]


def default_user_slots() -> list[dict]:
    return [dict(item) for item in DEFAULT_USER_SLOTS]


def _is_valid_hhmm(value: str) -> bool:
    return bool(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(value or "").strip()))


def _normalize_hhmm(value: str) -> str | None:
    s = str(value or "").strip()
    if _is_valid_hhmm(s):
        return s
    match = re.fullmatch(r"([0-2]?\d)[:.]([0-5]?\d)", s)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return f"{hour:02d}:{minute:02d}"
    return None


def _slot_id_from_time(time_local: str) -> str:
    normalized = _normalize_hhmm(time_local) or "00:00"
    return f"t_{normalized.replace(':', '')}"


def _normalize_slot_id(slot_id: str | None, time_local: str | None = None) -> tuple[str | None, str | None]:
    raw_slot = str(slot_id or "").strip()
    raw_time = str(time_local or "").strip()
    normalized_time = _normalize_hhmm(raw_time)
    if raw_slot in {"morning", "noon", "evening"}:
        return raw_slot, normalized_time
    if re.fullmatch(r"t_\d{4}", raw_slot):
        hhmm = f"{raw_slot[2:4]}:{raw_slot[4:6]}"
        if _is_valid_hhmm(hhmm):
            return raw_slot, normalized_time or hhmm
        return None, normalized_time
    if _is_valid_hhmm(raw_slot):
        hhmm = _normalize_hhmm(raw_slot)
        if not hhmm:
            return None, normalized_time
        return _slot_id_from_time(hhmm), normalized_time or hhmm
    converted_time = _normalize_hhmm(raw_slot)
    if converted_time:
        return _slot_id_from_time(converted_time), normalized_time or converted_time
    return None, normalized_time


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
        time_local = _normalize_hhmm(str(raw.get("time_local") or "").strip()) or ""
        if not _is_valid_hhmm(time_local):
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
    if not out:
        return default_user_slots()
    out.sort(key=lambda item: str(item.get("time_local") or "99:99"))
    return out

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
    page: int | None = None,
    page_size: int = 10,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> tuple[list[dict], int] | list[dict]:
    if page is None:
        qn = normalize_text(query)
        if not qn:
            return []
        safe_limit = max(1, min(int(limit or 20), 100))
        safe_offset = max(0, int(offset))
        q_like = f"%{qn}%"
        rows = await fetchall(
            conn,
            """SELECT id,
                      slug,
                      COALESCE(name_ru, '') AS name_ru,
                      COALESCE(name_en, '') AS name_en,
                      COALESCE(evidence_grade, '') AS evidence_grade,
                      CASE WHEN COALESCE(TRIM(purchase_url), '') <> '' THEN 1 ELSE 0 END AS has_link
               FROM supplements
               WHERE is_active=1
                 AND (
                    LOWER(COALESCE(name_ru, '')) LIKE ?
                    OR LOWER(COALESCE(name_en, '')) LIKE ?
                    OR LOWER(COALESCE(slug, '')) LIKE ?
                    OR LOWER(COALESCE(search_blob, '')) LIKE ?
                 )
               ORDER BY name_ru COLLATE NOCASE
               LIMIT ? OFFSET ?""",
            (q_like, q_like, q_like, q_like, safe_limit, safe_offset),
        )
        return [
            {
                "id": int(row["id"]),
                "slug": str(row["slug"] or ""),
                "name_ru": str(row["name_ru"] or ""),
                "name_en": str(row["name_en"] or ""),
                "evidence_grade": str(row["evidence_grade"] or ""),
                "has_link": bool(int(row["has_link"] or 0)),
            }
            for row in rows
        ]

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


async def get_supplement_admin(conn: aiosqlite.Connection, supplement_id: int) -> dict | None:
    row = await fetchone(
        conn,
        """SELECT id, slug, name_ru, name_en, purchase_url, evidence_grade, confidence,
                  summary_verified, summary_theoretical, dose_ru, timing_ru, safety_ru,
                  tags_json, body_targets_json, body_burdens_json, is_active, updated_at_utc
           FROM supplements
           WHERE id=?
           LIMIT 1""",
        (int(supplement_id),),
    )
    if not row:
        return None
    return {
        "id": int(row["id"]),
        "slug": str(row["slug"] or ""),
        "name_ru": str(row["name_ru"] or ""),
        "name_en": str(row["name_en"] or ""),
        "purchase_url": str(row["purchase_url"] or ""),
        "evidence_grade": str(row["evidence_grade"] or ""),
        "confidence": str(row["confidence"] or ""),
        "summary_ru_verified": str(row["summary_verified"] or ""),
        "summary_ru_theoretical": str(row["summary_theoretical"] or ""),
        "dose_ru": str(row["dose_ru"] or ""),
        "timing_ru": str(row["timing_ru"] or ""),
        "safety_ru": str(row["safety_ru"] or ""),
        "tags_json": str(row["tags_json"] or ""),
        "body_targets_json": str(row["body_targets_json"] or ""),
        "body_burdens_json": str(row["body_burdens_json"] or ""),
        "is_active": int(row["is_active"] or 0),
        "updated_at_utc": str(row["updated_at_utc"] or ""),
    }


async def set_purchase_url(conn: aiosqlite.Connection, supplement_id: int, url: str) -> None:
    await conn.execute(
        "UPDATE supplements SET purchase_url=? WHERE id=?",
        (str(url or "").strip()[:500], int(supplement_id)),
    )


async def clear_purchase_url(conn: aiosqlite.Connection, supplement_id: int) -> None:
    await conn.execute(
        "UPDATE supplements SET purchase_url='' WHERE id=?",
        (int(supplement_id),),
    )


ADMIN_EDITABLE_SUPPLEMENT_FIELDS: dict[str, dict] = {
    "name_ru": {"kind": "short_text", "max_len": 120},
    "name_en": {"kind": "short_text", "max_len": 120},
    "summary_ru_verified": {"kind": "long_text", "max_len": 8000},
    "summary_ru_theoretical": {"kind": "long_text", "max_len": 8000},
    "dose_ru": {"kind": "long_text", "max_len": 8000},
    "timing_ru": {"kind": "long_text", "max_len": 8000},
    "safety_ru": {"kind": "long_text", "max_len": 8000},
    "evidence_grade": {"kind": "enum", "values": {"A", "B", "C", "D"}},
    "confidence": {"kind": "enum", "values": {"High", "Medium", "Low", "Unknown"}},
    "tags_json": {"kind": "json_string_array"},
    "body_targets_json": {"kind": "json_string_array"},
    "body_burdens_json": {"kind": "json_string_array"},
    "purchase_url": {"kind": "url", "max_len": 500},
}

SUPPLEMENT_FIELD_COLUMN_MAP: dict[str, str] = {
    "summary_ru_verified": "summary_verified",
    "summary_ru_theoretical": "summary_theoretical",
}


def _supp_col(field: str) -> str:
    return SUPPLEMENT_FIELD_COLUMN_MAP.get(field, field)


def _validate_admin_supplement_value(field: str, value: str | None) -> str | None:
    cfg = ADMIN_EDITABLE_SUPPLEMENT_FIELDS.get(field)
    if not cfg:
        raise ValueError("unsupported field")
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return ""
    kind = str(cfg.get("kind") or "")
    if kind in {"short_text", "long_text", "url"}:
        max_len = int(cfg.get("max_len") or 8000)
        if len(text) > max_len:
            raise ValueError(f"max_len_{max_len}")
        if kind == "url" and not (text.startswith("http://") or text.startswith("https://")):
            raise ValueError("invalid_url")
        return text
    if kind == "enum":
        values = {str(v) for v in (cfg.get("values") or set())}
        if text not in values:
            raise ValueError("invalid_choice")
        return text
    if kind == "json_string_array":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid_json") from exc
        if not isinstance(data, list):
            raise ValueError("json_array_required")
        cleaned = []
        for item in data:
            if not isinstance(item, str):
                raise ValueError("json_strings_required")
            val = item.strip()
            if val:
                cleaned.append(val)
        return json.dumps(cleaned, ensure_ascii=False)
    return text


async def _admin_insert_audit(
    conn: aiosqlite.Connection,
    admin_tg_id: int,
    entity_id: int,
    field: str,
    old_value: str | None,
    new_value: str | None,
    action: str,
) -> None:
    await conn.execute(
        """INSERT INTO admin_audit(admin_tg_id, entity_type, entity_id, field, old_value, new_value, action, created_at_utc)
           VALUES(?,?,?,?,?,?,?,?)""",
        (int(admin_tg_id), "supplement", int(entity_id), str(field), old_value, new_value, str(action), utc_now_iso()),
    )


async def admin_set_supplement_field(
    conn: aiosqlite.Connection,
    admin_tg_id: int,
    supplement_id: int,
    field: str,
    value: str | None,
    *,
    commit: bool = True,
) -> None:
    safe_field = str(field or "").strip()
    column = _supp_col(safe_field)
    if safe_field not in ADMIN_EDITABLE_SUPPLEMENT_FIELDS:
        raise ValueError("unsupported field")
    actor_role = await get_role(conn, int(admin_tg_id))
    if actor_role == ROLE_EDITOR and safe_field in {"evidence_grade", "confidence"}:
        raise ValueError("permission_denied")
    normalized = _validate_admin_supplement_value(safe_field, value)
    row = await fetchone(
        conn,
        f"SELECT {column} AS current_value FROM supplements WHERE id=? LIMIT 1",
        (int(supplement_id),),
    )
    if not row:
        raise ValueError("supplement_not_found")
    old_value = row["current_value"]
    old_text = None if old_value is None else str(old_value)
    action = "clear" if (normalized is None or normalized == "") else "set"
    await conn.execute(
        f"UPDATE supplements SET {column}=?, updated_at_utc=? WHERE id=?",
        (normalized if normalized is not None else "", utc_now_iso(), int(supplement_id)),
    )
    await _admin_insert_audit(
        conn,
        int(admin_tg_id),
        int(supplement_id),
        safe_field,
        old_text,
        normalized,
        action,
    )
    if commit:
        await conn.commit()


async def admin_toggle_supplement_active(
    conn: aiosqlite.Connection,
    admin_tg_id: int,
    supplement_id: int,
    is_active: int,
    *,
    commit: bool = True,
) -> None:
    row = await fetchone(
        conn,
        "SELECT is_active FROM supplements WHERE id=? LIMIT 1",
        (int(supplement_id),),
    )
    if not row:
        raise ValueError("supplement_not_found")
    old_value = str(int(row["is_active"] or 0))
    new_value = str(1 if int(is_active) else 0)
    await conn.execute(
        "UPDATE supplements SET is_active=?, updated_at_utc=? WHERE id=?",
        (int(new_value), utc_now_iso(), int(supplement_id)),
    )
    await _admin_insert_audit(
        conn,
        int(admin_tg_id),
        int(supplement_id),
        "is_active",
        old_value,
        new_value,
        "toggle_active",
    )
    if commit:
        await conn.commit()


def _json_list_load(s: str) -> list[str]:
    raw = str(s or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out: list[str] = []
    for item in data:
        if not isinstance(item, str):
            continue
        token = _token_norm(item)
        if token:
            out.append(token)
    return sorted(set(out))


def _json_list_dump(lst: list[str]) -> str:
    cleaned = sorted({_token_norm(item) for item in list(lst or []) if _token_norm(item)})
    return json.dumps(cleaned, ensure_ascii=False)


def _token_norm(t: str) -> str:
    return str(t or "").strip().lower().replace(" ", "_")


async def admin_bulk_apply(
    conn,
    admin_tg_id: int,
    ids: list[int],
    op: str,
    params: dict,
    dry_run: bool = True,
) -> dict:
    target_ids = sorted({int(item) for item in list(ids or []) if int(item) > 0})
    if not target_ids:
        raise ValueError("validation")

    op_name = str(op or "").strip()
    payload = dict(params or {})
    fields = ["id", "slug", "is_active", "evidence_grade", "confidence", "tags_json", "body_targets_json", "body_burdens_json", "purchase_url"]
    placeholders = ",".join("?" for _ in target_ids)
    rows = await fetchall(
        conn,
        f"""SELECT {', '.join(fields)}
              FROM supplements
              WHERE id IN ({placeholders})""",
        tuple(target_ids),
    )
    by_id = {int(row["id"]): row for row in rows}

    actions: list[dict] = []
    examples: list[dict] = []
    changed = 0
    skipped = 0

    for supplement_id in target_ids:
        row = by_id.get(supplement_id)
        if not row:
            skipped += 1
            continue
        slug = str(row["slug"] or "")
        field = ""
        old_value = ""
        new_value = ""

        if op_name == "set_active":
            if "is_active" not in payload:
                raise ValueError("validation")
            target_active = 1 if int(payload.get("is_active") or 0) else 0
            old_active = int(row["is_active"] or 0)
            if old_active == target_active:
                skipped += 1
                continue
            field = "is_active"
            old_value = str(old_active)
            new_value = str(target_active)
        elif op_name == "set_evidence":
            evidence = str(payload.get("evidence_grade") or "").strip().upper()
            if evidence not in {"A", "B", "C", "D"}:
                raise ValueError("validation")
            old_text = str(row["evidence_grade"] or "")
            if old_text == evidence:
                skipped += 1
                continue
            field = "evidence_grade"
            old_value = old_text
            new_value = evidence
        elif op_name == "set_confidence":
            confidence = str(payload.get("confidence") or "").strip()
            if confidence not in {"High", "Medium", "Low", "Unknown"}:
                raise ValueError("validation")
            old_text = str(row["confidence"] or "")
            if old_text == confidence:
                skipped += 1
                continue
            field = "confidence"
            old_value = old_text
            new_value = confidence
        elif op_name in {"token_add", "token_remove"}:
            field_name = str(payload.get("field") or "").strip()
            if field_name not in {"tags_json", "body_targets_json", "body_burdens_json"}:
                raise ValueError("validation")
            token = _token_norm(str(payload.get("token") or ""))
            if not token:
                raise ValueError("validation")
            current_tokens = set(_json_list_load(str(row[field_name] or "[]")))
            before = sorted(current_tokens)
            if op_name == "token_add":
                current_tokens.add(token)
            else:
                current_tokens.discard(token)
            after = sorted(current_tokens)
            if before == after:
                skipped += 1
                continue
            field = field_name
            old_value = _json_list_dump(before)
            new_value = _json_list_dump(after)
        elif op_name == "purchase_template":
            template = str(payload.get("template") or "").strip()
            if not template or "{slug}" not in template or not (template.startswith("http://") or template.startswith("https://")):
                raise ValueError("validation")
            old_text = str(row["purchase_url"] or "")
            next_url = template.replace("{slug}", slug)
            if old_text == next_url:
                skipped += 1
                continue
            field = "purchase_url"
            old_value = old_text
            new_value = next_url
        elif op_name == "purchase_clear":
            old_text = str(row["purchase_url"] or "")
            if not old_text:
                skipped += 1
                continue
            field = "purchase_url"
            old_value = old_text
            new_value = ""
        else:
            raise ValueError("validation")

        changed += 1
        actions.append({"id": supplement_id, "field": field, "value": new_value, "is_active": (int(new_value) if field == "is_active" else None)})
        if len(examples) < 8:
            examples.append({"id": supplement_id, "slug": slug, "field": field, "old": old_value, "new": new_value})

    if not dry_run and actions:
        await conn.execute("BEGIN")
        try:
            for action in actions:
                if action["field"] == "is_active":
                    await admin_toggle_supplement_active(conn, int(admin_tg_id), int(action["id"]), int(action["is_active"]), commit=False)
                else:
                    await admin_set_supplement_field(conn, int(admin_tg_id), int(action["id"]), str(action["field"]), action["value"], commit=False)
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

    return {
        "dry_run": bool(dry_run),
        "requested": len(ids or []),
        "changed": changed,
        "skipped": skipped,
        "examples": examples,
    }


async def admin_delete_supplement_hard(conn: aiosqlite.Connection, admin_tg_id: int, supplement_id: int) -> None:
    row = await fetchone(conn, "SELECT id FROM supplements WHERE id=? LIMIT 1", (int(supplement_id),))
    if not row:
        raise ValueError("supplement_not_found")
    await _admin_insert_audit(
        conn,
        int(admin_tg_id),
        int(supplement_id),
        "supplement",
        str(int(supplement_id)),
        None,
        "delete",
    )
    await conn.execute("DELETE FROM supplements WHERE id=?", (int(supplement_id),))
    await conn.commit()


async def get_admin_audit_for_supplement(conn: aiosqlite.Connection, supplement_id: int, limit: int = 20) -> list[dict]:
    rows = await fetchall(
        conn,
        """SELECT field, old_value, new_value, action, admin_tg_id, created_at_utc
           FROM admin_audit
           WHERE entity_type='supplement' AND entity_id=?
           ORDER BY id DESC
           LIMIT ?""",
        (int(supplement_id), max(1, int(limit))),
    )
    return [
        {
            "field": str(r["field"] or ""),
            "old_value": None if r["old_value"] is None else str(r["old_value"]),
            "new_value": None if r["new_value"] is None else str(r["new_value"]),
            "action": str(r["action"] or ""),
            "admin_tg_id": int(r["admin_tg_id"] or 0),
            "created_at_utc": str(r["created_at_utc"] or ""),
        }
        for r in rows
    ]


async def admin_search_supplements(
    conn: aiosqlite.Connection,
    q: str,
    is_active: int | None,
    evidence_grade: str | None,
    page: int,
    page_size: int,
) -> dict:
    page = max(0, int(page))
    page_size = max(1, min(int(page_size), 100))
    offset = page * page_size
    clauses: list[str] = ["1=1"]
    params: list = []
    qn = normalize_text(q)
    if qn:
        clauses.append("(s.search_blob LIKE ? OR LOWER(COALESCE(s.slug,'')) LIKE ?)")
        params.extend([f"%{qn}%", f"%{qn}%"])
    if is_active in {0, 1}:
        clauses.append("s.is_active=?")
        params.append(int(is_active))
    ev = str(evidence_grade or "").strip().upper()
    if ev in {"A", "B", "C", "D"}:
        clauses.append("UPPER(COALESCE(s.evidence_grade,''))=?")
        params.append(ev)
    where_sql = " AND ".join(clauses)

    total_row = await fetchone(conn, f"SELECT COUNT(*) AS total FROM supplements s WHERE {where_sql}", tuple(params))
    total = int(total_row["total"] or 0) if total_row else 0
    rows = await fetchall(
        conn,
        f"""SELECT s.id, s.slug,
                   COALESCE(NULLIF(s.name_ru,''), NULLIF(s.name_en,''), s.name) AS name_ru,
                   COALESCE(s.evidence_grade, '') AS evidence_grade,
                   COALESCE(s.confidence, '') AS confidence,
                   COALESCE(s.is_active, 1) AS is_active,
                   CASE WHEN COALESCE(TRIM(s.purchase_url), '') <> '' THEN 1 ELSE 0 END AS has_link,
                   COALESCE(s.updated_at_utc, '') AS updated_at_utc
            FROM supplements s
            WHERE {where_sql}
            ORDER BY COALESCE(NULLIF(s.name_ru,''), NULLIF(s.name_en,''), s.name) COLLATE NOCASE
            LIMIT ? OFFSET ?""",
        (*params, page_size, offset),
    )
    items = [
        {
            "id": int(r["id"]),
            "slug": str(r["slug"] or ""),
            "name_ru": str(r["name_ru"] or ""),
            "evidence_grade": str(r["evidence_grade"] or ""),
            "confidence": str(r["confidence"] or ""),
            "is_active": int(r["is_active"] or 0),
            "has_link": bool(int(r["has_link"] or 0)),
            "updated_at_utc": str(r["updated_at_utc"] or ""),
        }
        for r in rows
    ]
    return {"items": items, "total": total, "page": page, "page_size": page_size}

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


def _catalog_where_clause(
    q: str | None,
    evidence: list[str] | None,
    targets: list[str] | None,
    burdens: list[str] | None,
) -> tuple[str, list]:
    clauses = ["s.is_active=1"]
    params: list = []

    qn = normalize_text(q)
    if qn:
        clauses.append("s.search_blob LIKE ?")
        params.append(f"%{qn}%")

    evidence_clean = [str(item or "").strip().upper() for item in (evidence or [])]
    evidence_clean = [item for item in evidence_clean if item in {"A", "B", "C", "D"}]
    if evidence_clean:
        placeholders = ",".join(["?"] * len(evidence_clean))
        clauses.append(f"UPPER(COALESCE(s.evidence_grade, '')) IN ({placeholders})")
        params.extend(evidence_clean)

    targets_clean = [normalize_text(item) for item in (targets or []) if str(item or "").strip()]
    if targets_clean:
        target_parts: list[str] = []
        for token in targets_clean:
            target_parts.append(
                "(LOWER(COALESCE(s.body_targets_json, '')) LIKE ? OR LOWER(COALESCE(s.body_targets_json, '')) LIKE ? "
                "OR LOWER(COALESCE(s.tags_json, '')) LIKE ? OR LOWER(COALESCE(s.tags_json, '')) LIKE ?)"
            )
            exact = f'%"{token}"%'
            prefix = f'%"{token}_%'
            params.extend([exact, prefix, exact, prefix])
        clauses.append(f"({' AND '.join(target_parts)})")

    burdens_clean = [normalize_text(item) for item in (burdens or []) if str(item or "").strip()]
    if burdens_clean:
        burden_parts: list[str] = []
        for token in burdens_clean:
            burden_parts.append("(LOWER(COALESCE(s.body_burdens_json, '')) LIKE ? OR LOWER(COALESCE(s.body_burdens_json, '')) LIKE ?)")
            exact = f'%"{token}"%'
            prefix = f'%"{token}_%'
            params.extend([exact, prefix])
        clauses.append(f"({' AND '.join(burden_parts)})")

    return " AND ".join(clauses), params


def _catalog_order_by(sort: str | None) -> str:
    alpha = "COALESCE(NULLIF(s.name_ru, ''), NULLIF(s.name_en, ''), s.name) COLLATE NOCASE, s.id"
    key = str(sort or "").strip().lower()
    if key == "evidence":
        return (
            "CASE UPPER(COALESCE(s.evidence_grade, '')) "
            "WHEN 'A' THEN 1 WHEN 'B' THEN 2 WHEN 'C' THEN 3 WHEN 'D' THEN 4 ELSE 5 END, "
            f"{alpha}"
        )
    if key == "effect":
        return (
            "CAST(COALESCE(NULLIF(s.avg_effect_percent_est, ''), '0') AS REAL) DESC, "
            f"{alpha}"
        )
    if key == "trials":
        return (
            "CAST(COALESCE(NULLIF(s.human_trials_count_est, ''), '0') AS INTEGER) DESC, "
            "CAST(COALESCE(NULLIF(s.participants_total_est, ''), '0') AS INTEGER) DESC, "
            f"{alpha}"
        )
    return alpha


async def catalog_search(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    q: str | None,
    evidence: list[str] | None,
    targets: list[str] | None,
    burdens: list[str] | None,
    sort: str | None,
    page: int,
    page_size: int,
) -> dict:
    user_id = await get_or_create_user(conn, tg_user_id)
    page = max(int(page), 0)
    page_size = max(1, min(int(page_size), 100))
    offset = page * page_size

    where_sql, where_params = _catalog_where_clause(q, evidence, targets, burdens)
    total_row = await fetchone(
        conn,
        f"SELECT COUNT(*) AS total FROM supplements s WHERE {where_sql}",
        tuple(where_params),
    )
    total = int(total_row["total"] or 0) if total_row else 0

    order_sql = _catalog_order_by(sort)
    rows = await fetchall(
        conn,
        f"""SELECT s.id,
                   s.slug,
                   COALESCE(NULLIF(s.name_ru, ''), NULLIF(s.name_en, ''), s.name) AS name_ru,
                   COALESCE(NULLIF(s.name_en, ''), s.name) AS name_en,
                   COALESCE(s.evidence_grade, '') AS evidence_grade,
                   COALESCE(s.confidence, '') AS confidence,
                   us.supplement_id AS in_stack_id,
                   CASE WHEN COALESCE(TRIM(s.purchase_url), '') <> '' THEN 1 ELSE 0 END AS has_purchase
            FROM supplements s
            LEFT JOIN user_supplements us
              ON us.user_id=? AND us.supplement_id=s.id
            WHERE {where_sql}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?""",
        (user_id, *where_params, page_size, offset),
    )
    items = []
    for row in rows:
        items.append(
            {
                "id": int(row["id"]),
                "slug": str(row["slug"] or ""),
                "name_ru": str(row["name_ru"] or ""),
                "name_en": str(row["name_en"] or ""),
                "evidence_grade": str(row["evidence_grade"] or ""),
                "confidence": str(row["confidence"] or ""),
                "in_stack": row["in_stack_id"] is not None,
                "has_purchase": bool(int(row["has_purchase"] or 0)),
            }
        )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


async def get_supplement_detail_for_user(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    slug: str,
) -> dict | None:
    user_id = await get_or_create_user(conn, tg_user_id)
    row = await fetchone(
        conn,
        """SELECT s.id, s.slug, s.name_ru, s.name_en, s.name,
                  s.evidence_grade, s.confidence,
                  s.summary_verified, s.summary_theoretical,
                  s.molecular_mechanisms,
                  s.dose_ru, s.timing_ru, s.safety_ru,
                  s.purchase_url,
                  s.tags_json, s.body_targets_json, s.body_burdens_json,
                  s.body_effects_json,
                  us.supplement_id AS in_stack_id
           FROM supplements s
           LEFT JOIN user_supplements us ON us.user_id=? AND us.supplement_id=s.id
           WHERE s.slug=? AND s.is_active=1
           LIMIT 1""",
        (user_id, slug),
    )
    if not row:
        return None
    return {
        "id": int(row["id"]),
        "slug": str(row["slug"] or ""),
        "name_ru": str(row["name_ru"] or row["name_en"] or row["name"] or ""),
        "name_en": str(row["name_en"] or row["name"] or ""),
        "evidence_grade": str(row["evidence_grade"] or ""),
        "confidence": str(row["confidence"] or ""),
        "summary_verified": str(row["summary_verified"] or ""),
        "summary_theoretical": str(row["summary_theoretical"] or ""),
        "molecular_mechanisms": str(row["molecular_mechanisms"] or ""),
        "dose_ru": str(row["dose_ru"] or ""),
        "timing_ru": str(row["timing_ru"] or ""),
        "safety_ru": str(row["safety_ru"] or ""),
        "purchase_url": str(row["purchase_url"] or ""),
        "tags_json": str(row["tags_json"] or "[]"),
        "body_targets_json": str(row["body_targets_json"] or "[]"),
        "body_burdens_json": str(row["body_burdens_json"] or "[]"),
        "body_effects_json": str(row["body_effects_json"] or "[]"),
        "in_stack": row["in_stack_id"] is not None,
    }

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
    await conn.execute(
        """INSERT OR IGNORE INTO user_schedules(user_id, supplement_id, time_hhmm, slot_id, is_enabled)
           VALUES(?, ?, NULL, 'morning', 1)""",
        (user_id, supplement_id),
    )
    await conn.execute(
        "UPDATE user_schedules SET slot_id='morning' WHERE user_id=? AND supplement_id=? AND (slot_id IS NULL OR slot_id='')",
        (user_id, supplement_id),
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
        """INSERT INTO user_schedules(user_id, supplement_id, time_hhmm, slot_id, is_enabled)
           VALUES(?,?,?,?,?)
           ON CONFLICT(user_id, supplement_id) DO UPDATE SET
             time_hhmm=excluded.time_hhmm,
             is_enabled=excluded.is_enabled""",
        (user_id, supplement_id, time_hhmm, "morning", 1 if time_hhmm else 0),
    )
    await conn.execute(
        "UPDATE user_schedules SET slot_id='morning' WHERE user_id=? AND supplement_id=? AND (slot_id IS NULL OR slot_id='')",
        (user_id, supplement_id),
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
    await set_day_state(conn, tg_user_id, day, supplement_id, status or "none")


async def get_day_status(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    day: str,
    supplement_id: int,
) -> str | None:
    state_map = await get_day_state_map(conn, tg_user_id, day)
    return state_map.get(int(supplement_id))


async def get_day_status_map(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    day: str,
) -> dict[int, str]:
    return await get_day_state_map(conn, tg_user_id, day)


async def set_day_state(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    local_date: str,
    supplement_id: int,
    status: str,
) -> None:
    if status not in {"taken", "skipped", "none"}:
        raise ValueError("status must be one of: taken, skipped, none")
    user_id = await get_or_create_user(conn, tg_user_id)
    if status == "none":
        await conn.execute(
            "DELETE FROM supplement_day_state WHERE user_id=? AND day=? AND supplement_id=?",
            (user_id, local_date, supplement_id),
        )
        await conn.commit()
        return
    await conn.execute(
        """INSERT INTO supplement_day_state(user_id, day, supplement_id, status)
           VALUES(?,?,?,?)
           ON CONFLICT(user_id, supplement_id, day) DO UPDATE SET status=excluded.status""",
        (user_id, local_date, supplement_id, status),
    )
    await conn.commit()


async def get_day_state_map(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    local_date: str,
) -> dict[int, str]:
    user_id = await get_or_create_user(conn, tg_user_id)
    rows = await fetchall(
        conn,
        "SELECT supplement_id, status FROM supplement_day_state WHERE user_id=? AND day=?",
        (user_id, local_date),
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
    user_id = await get_or_create_user(conn, tg_user_id)
    params = [user_id, *supplement_ids, start_day, end_day]
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

async def upsert_daily_checkin(conn: aiosqlite.Connection, tg_user_id: int, local_date: str, answers: dict) -> int:
    user_id = await get_or_create_user(conn, int(tg_user_id))
    tz_name = await get_user_tz(conn, int(tg_user_id)) or "UTC"
    now = utc_now_iso()
    await conn.execute(
        """INSERT INTO daily_checkins(user_id, date_local, tz_name, answers_json, created_at_utc)
           VALUES(?,?,?,?,?)
           ON CONFLICT(user_id, date_local) DO UPDATE SET
             tz_name=excluded.tz_name,
             answers_json=excluded.answers_json,
             created_at_utc=excluded.created_at_utc""",
        (user_id, local_date, tz_name, json.dumps(answers, ensure_ascii=False), now),
    )
    row = await fetchone(
        conn,
        "SELECT id FROM daily_checkins WHERE user_id=? AND date_local=?",
        (user_id, local_date),
    )
    await conn.commit()
    return int(row["id"]) if row else 0



async def save_ai_result(
    conn: aiosqlite.Connection,
    checkin_id: int,
    ai_raw_json: dict | list,
    prompt_hash: str,
) -> None:
    await conn.execute(
        """UPDATE daily_checkins
           SET ai_raw_json=?, ai_prompt_hash=?
           WHERE id=?""",
        (
            json.dumps(ai_raw_json, ensure_ascii=False),
            (prompt_hash or "").strip(),
            int(checkin_id),
        ),
    )
    await conn.commit()


async def apply_recommendations_to_today(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    local_date: str,
    recs: list[dict],
    mode: str = "apply_all",
) -> dict:
    user_id = await get_or_create_user(conn, int(tg_user_id))
    _ = mode
    now = utc_now_iso()
    existing_today = set(await get_today_supplement_ids(conn, tg_user_id, local_date))
    details: list[dict] = []
    applied = 0
    skipped_existing = 0
    unknown_slugs = 0

    for raw in list(recs or []):
        action = str(raw.get("action") or "add").strip().lower()
        if action != "add":
            continue
        supplement_id = int(raw.get("supplement_id") or 0)
        slug = str(raw.get("slug") or "").strip().lower()
        if supplement_id <= 0 and slug:
            row = await fetchone(conn, "SELECT id FROM supplements WHERE slug=?", (slug,))
            supplement_id = int(row["id"]) if row else 0
        if supplement_id <= 0:
            unknown_slugs += 1
            details.append({"slug": slug, "result": "unknown", "slot_id": None})
            continue

        time_local = _normalize_hhmm(str(raw.get("suggested_time") or raw.get("time_local") or "").strip())
        slot_id, normalized_time = _normalize_slot_id(
            raw.get("slot_id") or raw.get("suggested_slot_id"),
            time_local,
        )
        if not slot_id and normalized_time:
            slot_id = _slot_id_from_time(normalized_time)
        if not slot_id:
            slot_id = "morning"

        if supplement_id in existing_today:
            skipped_existing += 1
            details.append({"slug": slug, "result": "skipped", "slot_id": slot_id})
            continue

        row_s = await fetchone(
            conn,
            "SELECT COALESCE(NULLIF(dose_text,''), '') AS dose_text FROM user_supp_settings WHERE user_id=? AND supplement_id=?",
            (user_id, supplement_id),
        )
        dose_text = str((row_s["dose_text"] if row_s else "") or "").strip() or None

        await conn.execute(
            """INSERT INTO day_plan_items(
                 user_id, date_local, supplement_id, action, dose_text, time_hint, slot_id, time_local, reason, source, created_at_utc
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(user_id, date_local, supplement_id, action) DO UPDATE SET
                 dose_text=COALESCE(excluded.dose_text, day_plan_items.dose_text),
                 time_hint=excluded.time_hint,
                 slot_id=excluded.slot_id,
                 time_local=excluded.time_local,
                 reason=excluded.reason,
                 source='ai',
                 created_at_utc=excluded.created_at_utc""",
            (
                user_id,
                local_date,
                supplement_id,
                "add",
                dose_text,
                normalized_time,
                slot_id,
                normalized_time,
                str(raw.get("reason") or raw.get("reason_ru") or "").strip() or None,
                "ai",
                now,
            ),
        )
        existing_today.add(supplement_id)
        applied += 1
        details.append({"slug": slug, "result": "added", "slot_id": slot_id})

    await conn.commit()
    return {
        "applied": applied,
        "skipped_existing": skipped_existing,
        "unknown_slugs": unknown_slugs,
        "details": details,
    }


async def apply_ai_plan_items(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    local_date: str,
    selected: list[dict],
) -> None:
    await apply_recommendations_to_today(conn, tg_user_id, local_date, selected, mode="apply_all")


async def build_ai_context_compact(conn: aiosqlite.Connection, tg_user_id: int, local_date: str) -> dict:
    user_id = await get_or_create_user(conn, int(tg_user_id))
    stack_rows = await fetchall(
        conn,
        """SELECT s.slug, COALESCE(NULLIF(s.name_ru,''), NULLIF(s.name_en,''), s.name) AS name_ru,
                  sch.slot_id, sch.time_hhmm
           FROM user_supplements us
           JOIN supplements s ON s.id=us.supplement_id
           LEFT JOIN user_schedules sch
             ON sch.user_id=us.user_id AND sch.supplement_id=us.supplement_id
           WHERE us.user_id=? AND us.is_active=1
           ORDER BY s.name
           LIMIT 60""",
        (user_id,),
    )
    stack = [
        {
            "slug": str(r["slug"] or ""),
            "name": str(r["name_ru"] or ""),
            "slot_id": str(r["slot_id"] or "") or None,
            "time_override": _normalize_hhmm(r["time_hhmm"]) if r["time_hhmm"] else None,
        }
        for r in stack_rows
    ]

    end = datetime.strptime(local_date, "%Y-%m-%d").date()
    start = end - timedelta(days=6)
    rows = await fetchall(
        conn,
        """SELECT date_local, ai_raw_json, answers_json
           FROM daily_checkins
           WHERE user_id=? AND date_local>=? AND date_local<=?
           ORDER BY date_local DESC
           LIMIT 7""",
        (user_id, start.isoformat(), end.isoformat()),
    )
    last7 = []
    for r in rows:
        day = str(r["date_local"] or "")
        status_rows = await fetchall(
            conn,
            "SELECT status FROM supplement_day_state WHERE user_id=? AND day=?",
            (user_id, day),
        )
        total = len(status_rows)
        taken = sum(1 for x in status_rows if str(x["status"] or "") == "taken")
        adherence_pct = int(round((taken / total) * 100)) if total else 0
        ai_added = []
        try:
            raw = json.loads(r["ai_raw_json"] or "[]")
            for it in list(raw or []):
                if str(it.get("action") or "add") == "add" and it.get("slug"):
                    ai_added.append(str(it.get("slug")))
        except Exception:
            pass
        notes_short = ""
        try:
            ans = json.loads(r["answers_json"] or "{}")
            notes_short = str(ans.get("note") or "").strip()[:120]
        except Exception:
            pass
        last7.append({"date": day, "adherence_pct": adherence_pct, "ai_added": ai_added[:6], "notes_short": notes_short})

    burdens = await body_map_aggregate(conn, int(tg_user_id), local_date)
    systems = burdens.get("systems") if isinstance(burdens, dict) else {}
    burden_out = {
        "liver": int((systems or {}).get("liver", {}).get("burden_count", 0) or 0),
        "kidney": int((systems or {}).get("kidney", {}).get("burden_count", 0) or 0),
        "gut": int((systems or {}).get("gut", {}).get("burden_count", 0) or 0),
    }

    complexes_rows = await fetchall(
        conn,
        """SELECT c.id, c.name
           FROM user_complexes c
           WHERE c.user_id=?
           ORDER BY c.updated_at_utc DESC, c.id DESC
           LIMIT 30""",
        (user_id,),
    )
    complexes = []
    for row in complexes_rows:
        items = await fetchall(
            conn,
            """SELECT s.slug
               FROM user_complex_items ci
               JOIN supplements s ON s.id=ci.supplement_id
               WHERE ci.complex_id=?
               ORDER BY ci.sort_order, s.slug
               LIMIT 60""",
            (int(row["id"]),),
        )
        complexes.append({"name": str(row["name"] or ""), "slugs": [str(x["slug"] or "") for x in items if str(x["slug"] or "").strip()]})

    stats = await build_stats_compact(conn, tg_user_id, local_date)
    return {"date": local_date, "stack": stack, "last7": last7, "burdens": burden_out, "complexes": complexes, "stats": stats}


async def get_daily_checkin_by_id(conn: aiosqlite.Connection, checkin_id: int, tg_user_id: int) -> dict | None:
    user_id = await get_or_create_user(conn, int(tg_user_id))
    row = await fetchone(conn, "SELECT * FROM daily_checkins WHERE id=? AND user_id=?", (int(checkin_id), user_id))
    return dict(row) if row else None


async def get_today_checkin(conn: aiosqlite.Connection, tg_user_id: int, local_date: str) -> dict | None:
    user_id = await get_or_create_user(conn, int(tg_user_id))
    row = await fetchone(
        conn,
        "SELECT * FROM daily_checkins WHERE user_id=? AND date_local=? ORDER BY id DESC LIMIT 1",
        (user_id, local_date),
    )
    return dict(row) if row else None

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
             ON sds.user_id=u.id AND sds.supplement_id=dpi.supplement_id AND sds.day=dpi.date_local
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
                  sch.time_hhmm, sch.slot_id
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
                "slot_id": str(row["slot_id"] or "").strip(),
            }
        )
    return items


async def get_today_plan_groups(conn: aiosqlite.Connection, tg_user_id: int, local_date: str) -> list[dict]:
    user_id = await get_or_create_user(conn, tg_user_id)
    slots = await get_user_slots(conn, user_id)
    slots_by_id = {str(s.get("slot_id") or ""): s for s in slots}

    slot_title_by_time = {
        str(s.get("time_local") or ""): str(s.get("title_ru") or "Слот")
        for s in slots
        if bool(s.get("is_enabled", True))
    }
    by_time: dict[str, dict] = {}

    schedule_rows = await fetchall(
        conn,
        """SELECT cs.complex_id, cs.slot_id, cs.time_local, c.name
           FROM user_complex_schedules cs
           JOIN user_complexes c ON c.id=cs.complex_id
           WHERE cs.user_id=? AND cs.is_enabled=1
           ORDER BY c.name""",
        (user_id,),
    )
    state_map = await get_day_state_map(conn, tg_user_id, local_date)
    slot_complex_item_ids: dict[str, set[int]] = {}

    for row in schedule_rows:
        slot_id = str(row["slot_id"] or "").strip() or "morning"
        slot_info = slots_by_id.get(slot_id) or (slots[0] if slots else {"slot_id": "morning", "title_ru": "Утро", "time_local": "08:00"})
        effective_time = _normalize_hhmm(str(row["time_local"] or "").strip() or str(slot_info.get("time_local") or "08:00")) or str(slot_info.get("time_local") or "08:00")
        group = by_time.setdefault(
            effective_time,
            {
                "slot_id": str(slot_id),
                "title": slot_title_by_time.get(effective_time, str(slot_info.get("title_ru") or "Слот")),
                "time": effective_time,
                "items": [],
                "complexes": [],
            },
        )
        complex_id = int(row["complex_id"] or 0)
        items = await list_complex_items(conn, tg_user_id, complex_id)
        taken = 0
        total = len(items)
        for it in items:
            sid = int(it.get("supplement_id") or 0)
            st = state_map.get(sid, "none")
            it["status"] = st
            if st == "taken":
                taken += 1
            slot_complex_item_ids.setdefault(str(group["slot_id"]), set()).add(sid)
        status = "none"
        if total > 0 and taken == total:
            status = "taken"
        elif taken > 0:
            status = "partial"
        group["complexes"].append(
            {
                "complex_id": complex_id,
                "name": str(row["name"] or ""),
                "status": status,
                "taken_count": taken,
                "total_count": total,
                "items": items,
            }
        )

    rows = await fetchall(
        conn,
        """SELECT us.supplement_id,
                  COALESCE(NULLIF(uss.dose_text,''), us.dose_text, '') AS dose_text,
                  s.slug, COALESCE(NULLIF(s.name_ru,''), NULLIF(s.name_en,''), s.name) AS name_ru,
                  sch.slot_id, sch.time_hhmm
           FROM user_supplements us
           JOIN supplements s ON s.id=us.supplement_id
           LEFT JOIN user_supp_settings uss ON uss.user_id=us.user_id AND uss.supplement_id=us.supplement_id
           LEFT JOIN user_schedules sch ON sch.user_id=us.user_id AND sch.supplement_id=us.supplement_id
           WHERE us.user_id=? AND us.is_active=1
           ORDER BY s.name_ru, s.name, us.supplement_id""",
        (user_id,),
    )
    for row in rows:
        item_slot_id = str(row["slot_id"] or "").strip() or "morning"
        slot_info = slots_by_id.get(item_slot_id) or (slots[0] if slots else {"slot_id": "morning", "title_ru": "Утро", "time_local": "08:00"})
        effective_time = _normalize_hhmm(str(row["time_hhmm"] or "").strip() or str(slot_info.get("time_local") or "08:00")) or str(slot_info.get("time_local") or "08:00")
        group = by_time.setdefault(
            effective_time,
            {
                "slot_id": str(slot_info.get("slot_id") or item_slot_id),
                "title": slot_title_by_time.get(effective_time, str(slot_info.get("title_ru") or "Слот")),
                "time": effective_time,
                "items": [],
                "complexes": [],
            },
        )
        sid = int(row["supplement_id"] or 0)
        if sid in slot_complex_item_ids.get(str(group["slot_id"]), set()):
            continue
        group["items"].append(
            {
                "supplement_id": sid,
                "slug": str(row["slug"] or "").strip(),
                "name_ru": str(row["name_ru"] or "").strip(),
                "dose_text": str(row["dose_text"] or "").strip(),
            }
        )

    present_ids: set[int] = set()
    for group in by_time.values():
        for item in list(group.get("items") or []):
            present_ids.add(int(item.get("supplement_id") or 0))
        for cx in list(group.get("complexes") or []):
            for ci in list(cx.get("items") or []):
                present_ids.add(int(ci.get("supplement_id") or 0))

    ai_rows = await fetchall(
        conn,
        """SELECT dpi.supplement_id,
                  COALESCE(NULLIF(dpi.dose_text,''), '') AS dose_text,
                  COALESCE(NULLIF(dpi.slot_id,''), NULLIF(dpi.time_local,''), NULLIF(dpi.time_hint,''), 'morning') AS slot_or_time,
                  COALESCE(NULLIF(dpi.time_local,''), NULLIF(dpi.time_hint,''), '') AS time_local,
                  s.slug,
                  COALESCE(NULLIF(s.name_ru,''), NULLIF(s.name_en,''), s.name) AS name_ru
           FROM day_plan_items dpi
           JOIN supplements s ON s.id=dpi.supplement_id
           WHERE dpi.user_id=? AND dpi.date_local=? AND dpi.action='add'""",
        (user_id, local_date),
    )
    for row in ai_rows:
        sid = int(row["supplement_id"] or 0)
        if sid in present_ids:
            continue
        slot_id, from_time = _normalize_slot_id(str(row["slot_or_time"] or ""), str(row["time_local"] or ""))
        slot_id = slot_id or "morning"
        slot_info = slots_by_id.get(slot_id) or (slots[0] if slots else {"slot_id": "morning", "title_ru": "Утро", "time_local": "08:00"})
        effective_time = _normalize_hhmm(str(row["time_local"] or "").strip() or str(from_time or "").strip() or str(slot_info.get("time_local") or "08:00")) or str(slot_info.get("time_local") or "08:00")
        group = by_time.setdefault(
            effective_time,
            {
                "slot_id": str(slot_id),
                "title": slot_title_by_time.get(effective_time, str(slot_info.get("title_ru") or "Слот")),
                "time": effective_time,
                "items": [],
                "complexes": [],
            },
        )
        group["items"].append(
            {
                "supplement_id": sid,
                "slug": str(row["slug"] or "").strip(),
                "name_ru": str(row["name_ru"] or "").strip(),
                "dose_text": str(row["dose_text"] or "").strip(),
            }
        )
        present_ids.add(sid)

    out = sorted(by_time.values(), key=lambda g: str(g.get("time") or "99:99"))
    for group in out:
        group["items"].sort(key=lambda item: (str(item.get("name_ru") or ""), int(item.get("supplement_id") or 0)))
        group["complexes"].sort(key=lambda c: str(c.get("name") or ""))
    return out


BODY_MAP_SYSTEM_KEYS = [
    "brain", "heart", "liver", "kidney", "gut", "lungs", "thyroid", "pancreas",
    "joints", "muscle", "bone", "skin", "immune", "blood", "endocrine",
]


def _norm_system(value: str | None) -> str:
    s = str(value or "").strip().lower()
    if not s:
        return ""
    s = re.sub(r"[\s\-\/]+", "_", s)
    s = re.sub(r"[^a-z0-9_]+", "", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


def _is_endocrine_like(system_raw: str) -> bool:
    value = (system_raw or "").strip().lower()
    return any(token in value for token in (
        "hormon", "endocr", "adrenal", "cortisol",
        "testoster", "estrogen", "progester", "androgen",
        "pituitar", "growth_hormone", "gh", "prolact", "melatonin",
    ))


def _body_map_system_key(system_raw: str | None) -> str | None:
    system = _norm_system(system_raw)
    if not system:
        return None
    if system in BODY_MAP_SYSTEM_KEYS:
        return system

    aliases = {
        "stomach": "gut",
        "intestine": "gut",
        "kidneys": "kidney",
        "cns": "brain",
        "brain_cns": "brain",
        "heart_cv": "heart",
        "vessels_blood": "blood",
        "skin_hair": "skin",
    }
    if system in aliases:
        return aliases[system]

    if any(t in system for t in ("thyroid", "thyreoid", "tiroid", "tsh", "thyrox", "t3", "t4")):
        return "thyroid"

    if any(t in system for t in ("pancrea", "insulin", "glucose", "glycem", "hba1c")):
        return "pancreas"

    if any(t in system for t in ("hepat", "hepatic", "bile", "gall")):
        return "liver"

    if any(t in system for t in ("renal", "nephr", "kidney")):
        return "kidney"

    if any(t in system for t in ("gut", "gi", "gastro", "digest", "microbiome", "intestin", "stomach")):
        return "gut"

    if any(t in system for t in ("lung", "respir", "bronch")):
        return "lungs"

    if any(t in system for t in ("heart", "cardio", "cardiac")):
        return "heart"

    if any(t in system for t in ("blood", "vessel", "vascular", "vein", "venous", "arter", "microcircul", "capillar")):
        return "blood"

    if any(t in system for t in ("immune", "immun")):
        return "immune"

    if any(t in system for t in ("skin", "hair", "derm")):
        return "skin"

    if any(t in system for t in ("joint", "cartilage", "connective")):
        return "joints"

    if any(t in system for t in ("muscle", "myo", "strength")):
        return "muscle"

    if any(t in system for t in ("bone", "skelet", "oste")):
        return "bone"

    if any(t in system for t in ("brain", "neuro", "nerv", "cognit", "mood", "sleep", "cns")):
        return "brain"

    if _is_endocrine_like(system):
        return "endocrine"

    return None


async def debug_bodymap_unknown_systems(conn: aiosqlite.Connection, limit: int = 50) -> list[tuple[str, int]]:
    rows = await fetchall(
        conn,
        "SELECT body_effects_json FROM supplements WHERE body_effects_json IS NOT NULL AND TRIM(body_effects_json) != ''",
    )
    counter: dict[str, int] = {}
    for row in rows:
        try:
            effects = json.loads(row["body_effects_json"] or "[]")
        except Exception:
            continue
        if not isinstance(effects, list):
            continue
        for effect in effects:
            if not isinstance(effect, dict):
                continue
            raw = effect.get("system")
            key = _body_map_system_key(raw)
            if key is None:
                normalized = _norm_system(raw)
                if normalized:
                    counter[normalized] = counter.get(normalized, 0) + 1
    items = sorted(counter.items(), key=lambda x: -x[1])
    return items[:limit]



async def create_complex(conn: aiosqlite.Connection, tg_user_id: int, name: str, note: str = "") -> dict:
    user_id = await get_or_create_user(conn, int(tg_user_id))
    now = utc_now_iso()
    title = str(name or "").strip()
    if not title:
        raise ValueError("name_required")
    await conn.execute(
        """INSERT INTO user_complexes(user_id, name, note, created_at_utc, updated_at_utc)
           VALUES(?,?,?,?,?)""",
        (user_id, title[:120], str(note or "").strip()[:500], now, now),
    )
    row = await fetchone(conn, "SELECT id, name, note, created_at_utc, updated_at_utc FROM user_complexes WHERE user_id=? AND name=?", (user_id, title[:120]))
    await conn.commit()
    return dict(row) if row else {"id": 0, "name": title[:120], "note": str(note or "").strip()[:500]}


async def rename_complex(conn: aiosqlite.Connection, tg_user_id: int, complex_id: int, new_name: str) -> None:
    user_id = await get_or_create_user(conn, int(tg_user_id))
    title = str(new_name or "").strip()
    if not title:
        raise ValueError("name_required")
    await conn.execute(
        "UPDATE user_complexes SET name=?, updated_at_utc=? WHERE id=? AND user_id=?",
        (title[:120], utc_now_iso(), int(complex_id), user_id),
    )
    await conn.commit()


async def delete_complex(conn: aiosqlite.Connection, tg_user_id: int, complex_id: int) -> None:
    user_id = await get_or_create_user(conn, int(tg_user_id))
    await conn.execute("DELETE FROM user_complex_items WHERE complex_id=?", (int(complex_id),))
    await conn.execute("DELETE FROM user_complex_schedules WHERE user_id=? AND complex_id=?", (user_id, int(complex_id)))
    await conn.execute("DELETE FROM user_complexes WHERE user_id=? AND id=?", (user_id, int(complex_id)))
    await conn.commit()


async def list_complexes(conn: aiosqlite.Connection, tg_user_id: int) -> list[dict]:
    user_id = await get_or_create_user(conn, int(tg_user_id))
    rows = await fetchall(
        conn,
        """SELECT c.id, c.name, COUNT(ci.supplement_id) AS items_count
           FROM user_complexes c
           LEFT JOIN user_complex_items ci ON ci.complex_id=c.id
           WHERE c.user_id=?
           GROUP BY c.id, c.name
           ORDER BY c.name""",
        (user_id,),
    )
    return [{"id": int(r["id"]), "name": str(r["name"] or ""), "items_count": int(r["items_count"] or 0)} for r in rows]


async def add_item_to_complex(conn: aiosqlite.Connection, tg_user_id: int, complex_id: int, supplement_id: int, dose_override: str = "") -> None:
    user_id = await get_or_create_user(conn, int(tg_user_id))
    owner = await fetchone(conn, "SELECT id FROM user_complexes WHERE id=? AND user_id=?", (int(complex_id), user_id))
    if not owner:
        raise ValueError("complex_not_found")
    row = await fetchone(conn, "SELECT COALESCE(MAX(sort_order), 0) AS mx FROM user_complex_items WHERE complex_id=?", (int(complex_id),))
    nxt = int((row["mx"] if row else 0) or 0) + 1
    await conn.execute(
        """INSERT INTO user_complex_items(complex_id, supplement_id, dose_override, sort_order)
           VALUES(?,?,?,?)
           ON CONFLICT(complex_id, supplement_id) DO UPDATE SET dose_override=excluded.dose_override""",
        (int(complex_id), int(supplement_id), str(dose_override or "").strip()[:120], nxt),
    )
    await conn.commit()


async def remove_item_from_complex(conn: aiosqlite.Connection, tg_user_id: int, complex_id: int, supplement_id: int) -> None:
    user_id = await get_or_create_user(conn, int(tg_user_id))
    owner = await fetchone(conn, "SELECT 1 FROM user_complexes WHERE id=? AND user_id=?", (int(complex_id), user_id))
    if not owner:
        return
    await conn.execute("DELETE FROM user_complex_items WHERE complex_id=? AND supplement_id=?", (int(complex_id), int(supplement_id)))
    await conn.commit()


async def list_complex_items(conn: aiosqlite.Connection, tg_user_id: int, complex_id: int) -> list[dict]:
    user_id = await get_or_create_user(conn, int(tg_user_id))
    owner = await fetchone(conn, "SELECT 1 FROM user_complexes WHERE id=? AND user_id=?", (int(complex_id), user_id))
    if not owner:
        return []
    rows = await fetchall(
        conn,
        """SELECT ci.supplement_id, s.slug, COALESCE(NULLIF(s.name_ru,''), NULLIF(s.name_en,''), s.name) AS name_ru,
                  ci.dose_override
           FROM user_complex_items ci
           JOIN supplements s ON s.id=ci.supplement_id
           WHERE ci.complex_id=?
           ORDER BY ci.sort_order, name_ru""",
        (int(complex_id),),
    )
    return [{"supplement_id": int(r["supplement_id"]), "slug": str(r["slug"] or ""), "name_ru": str(r["name_ru"] or ""), "dose_override": str(r["dose_override"] or "")} for r in rows]


async def set_complex_schedule(conn: aiosqlite.Connection, tg_user_id: int, complex_id: int, slot_id: str, time_local: str | None) -> None:
    user_id = await get_or_create_user(conn, int(tg_user_id))
    owner = await fetchone(conn, "SELECT 1 FROM user_complexes WHERE id=? AND user_id=?", (int(complex_id), user_id))
    if not owner:
        raise ValueError("complex_not_found")
    await conn.execute(
        """INSERT INTO user_complex_schedules(user_id, complex_id, slot_id, time_local, is_enabled, created_at_utc)
           VALUES(?,?,?,?,1,?)
           ON CONFLICT(user_id, complex_id, slot_id) DO UPDATE SET
             time_local=excluded.time_local,
             is_enabled=1""",
        (user_id, int(complex_id), str(slot_id or "morning").strip() or "morning", _normalize_hhmm(time_local) or "", utc_now_iso()),
    )
    await conn.commit()


async def remove_complex_schedule(conn: aiosqlite.Connection, tg_user_id: int, complex_id: int, slot_id: str) -> None:
    user_id = await get_or_create_user(conn, int(tg_user_id))
    await conn.execute(
        "DELETE FROM user_complex_schedules WHERE user_id=? AND complex_id=? AND slot_id=?",
        (user_id, int(complex_id), str(slot_id or "").strip()),
    )
    await conn.commit()


async def list_complex_schedules(conn: aiosqlite.Connection, tg_user_id: int) -> list[dict]:
    user_id = await get_or_create_user(conn, int(tg_user_id))
    rows = await fetchall(
        conn,
        """SELECT cs.id, cs.complex_id, cs.slot_id, cs.time_local, cs.is_enabled,
                  c.name
           FROM user_complex_schedules cs
           JOIN user_complexes c ON c.id=cs.complex_id
           WHERE cs.user_id=? AND cs.is_enabled=1
           ORDER BY cs.slot_id, c.name""",
        (user_id,),
    )
    return [dict(r) for r in rows]


async def toggle_complex_for_day(conn: aiosqlite.Connection, tg_user_id: int, local_date: str, complex_id: int, status: str) -> None:
    if status not in {"taken", "none"}:
        raise ValueError("status must be taken|none")
    user_id = await get_or_create_user(conn, int(tg_user_id))
    owner = await fetchone(conn, "SELECT 1 FROM user_complexes WHERE id=? AND user_id=?", (int(complex_id), user_id))
    if not owner:
        return
    rows = await fetchall(conn, "SELECT supplement_id FROM user_complex_items WHERE complex_id=?", (int(complex_id),))
    for row in rows:
        sid = int(row["supplement_id"] or 0)
        if sid <= 0:
            continue
        if status == "none":
            await conn.execute(
                "DELETE FROM supplement_day_state WHERE user_id=? AND day=? AND supplement_id=?",
                (user_id, local_date, sid),
            )
        else:
            await conn.execute(
                """INSERT INTO supplement_day_state(user_id, day, supplement_id, status)
                   VALUES(?,?,?,?)
                   ON CONFLICT(user_id, supplement_id, day) DO UPDATE SET status=excluded.status""",
                (user_id, local_date, sid, "taken"),
            )
    await conn.commit()

async def get_today_supplement_ids(conn: aiosqlite.Connection, tg_user_id: int, local_date: str) -> list[int]:
    groups = await get_today_plan_groups(conn, tg_user_id, local_date)
    ids: set[int] = set()
    for group in groups:
        for item in list(group.get("items") or []):
            sid = int(item.get("supplement_id") or 0)
            if sid > 0:
                ids.add(sid)
    return sorted(ids)


async def get_expected_supplement_ids(conn: aiosqlite.Connection, tg_user_id: int, local_date: str) -> set[int]:
    ids = await get_today_supplement_ids(conn, tg_user_id, local_date)
    return {int(sid) for sid in ids if int(sid) > 0}


async def get_taken_supplement_ids(conn: aiosqlite.Connection, tg_user_id: int, local_date: str) -> set[int]:
    user_id = await get_or_create_user(conn, int(tg_user_id))
    rows = await fetchall(
        conn,
        "SELECT supplement_id FROM supplement_day_state WHERE user_id=? AND day=? AND status='taken'",
        (user_id, local_date),
    )
    return {int(r["supplement_id"] or 0) for r in rows if int(r["supplement_id"] or 0) > 0}


async def get_daily_adherence(conn: aiosqlite.Connection, tg_user_id: int, local_date: str) -> dict:
    expected_ids = await get_expected_supplement_ids(conn, tg_user_id, local_date)
    taken_ids = await get_taken_supplement_ids(conn, tg_user_id, local_date)
    expected = len(expected_ids)
    taken = len(expected_ids & taken_ids)
    adherence = int(round((100 * taken) / expected)) if expected > 0 else None
    return {"expected": expected, "taken": taken, "adherence": adherence}


async def get_range_adherence(conn: aiosqlite.Connection, tg_user_id: int, end_date: str, days: int) -> dict:
    safe_days = max(1, min(int(days or 1), 30))
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    expected_total = 0
    taken_total = 0
    for offset in range(safe_days):
        day = (end - timedelta(days=offset)).isoformat()
        daily = await get_daily_adherence(conn, tg_user_id, day)
        expected_total += int(daily.get("expected") or 0)
        taken_total += int(daily.get("taken") or 0)
    adherence = int(round((100 * taken_total) / expected_total)) if expected_total > 0 else None
    return {"days": safe_days, "expected_total": expected_total, "taken_total": taken_total, "adherence": adherence}


async def get_top_misses(conn: aiosqlite.Connection, tg_user_id: int, end_date: str, days: int, limit: int = 10) -> list[dict]:
    safe_days = max(1, min(int(days or 1), 30))
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    misses: dict[int, int] = {}
    for offset in range(safe_days):
        day = (end - timedelta(days=offset)).isoformat()
        expected_ids = await get_expected_supplement_ids(conn, tg_user_id, day)
        taken_ids = await get_taken_supplement_ids(conn, tg_user_id, day)
        for sid in (expected_ids - taken_ids):
            misses[sid] = misses.get(sid, 0) + 1
    if not misses:
        return []
    top_ids = [sid for sid, _ in sorted(misses.items(), key=lambda x: (-x[1], x[0]))[: max(1, int(limit or 10))]]
    placeholders = ",".join("?" for _ in top_ids)
    rows = await fetchall(
        conn,
        f"""SELECT id, slug, COALESCE(NULLIF(name_ru,''), NULLIF(name_en,''), name) AS name_ru
            FROM supplements
            WHERE id IN ({placeholders})""",
        tuple(top_ids),
    )
    meta = {
        int(r["id"]): {"slug": str(r["slug"] or ""), "name_ru": str(r["name_ru"] or "")}
        for r in rows
    }
    out = []
    for sid in top_ids:
        row = meta.get(int(sid), {})
        out.append(
            {
                "supplement_id": int(sid),
                "slug": str(row.get("slug") or ""),
                "name_ru": str(row.get("name_ru") or ""),
                "missed": int(misses.get(int(sid), 0)),
            }
        )
    return out


async def get_user_stack_supplement_ids(conn: aiosqlite.Connection, tg_user_id: int) -> set[int]:
    user_id = await get_or_create_user(conn, int(tg_user_id))
    base_rows = await fetchall(conn, "SELECT supplement_id FROM user_supplements WHERE user_id=? AND is_active=1", (user_id,))
    complex_rows = await fetchall(
        conn,
        """SELECT DISTINCT ci.supplement_id
           FROM user_complexes c
           JOIN user_complex_items ci ON ci.complex_id=c.id
           WHERE c.user_id=?""",
        (user_id,),
    )
    ids = {int(r["supplement_id"] or 0) for r in base_rows}
    ids.update(int(r["supplement_id"] or 0) for r in complex_rows)
    return {sid for sid in ids if sid > 0}


async def get_streaks(
    conn: aiosqlite.Connection,
    tg_user_id: int,
    as_of_date: str,
    supplement_ids: set[int],
    max_back: int = 90,
) -> list[dict]:
    if not supplement_ids:
        return []
    max_days = max(1, int(max_back or 1))
    end = datetime.strptime(as_of_date, "%Y-%m-%d").date()
    expected_cache: dict[str, set[int]] = {}
    taken_cache: dict[str, set[int]] = {}

    async def _expected(day: str) -> set[int]:
        if day not in expected_cache:
            expected_cache[day] = await get_expected_supplement_ids(conn, tg_user_id, day)
        return expected_cache[day]

    async def _taken(day: str) -> set[int]:
        if day not in taken_cache:
            taken_cache[day] = await get_taken_supplement_ids(conn, tg_user_id, day)
        return taken_cache[day]

    streak_map: dict[int, int] = {}
    for sid in sorted(supplement_ids):
        streak = 0
        for offset in range(max_days):
            day = (end - timedelta(days=offset)).isoformat()
            expected_ids = await _expected(day)
            if sid not in expected_ids:
                continue
            if sid in await _taken(day):
                streak += 1
                continue
            break
        streak_map[sid] = streak

    top_ids = [sid for sid, n in sorted(streak_map.items(), key=lambda x: (-x[1], x[0])) if n > 0][:20]
    if not top_ids:
        return []
    placeholders = ",".join("?" for _ in top_ids)
    rows = await fetchall(
        conn,
        f"""SELECT id, slug, COALESCE(NULLIF(name_ru,''), NULLIF(name_en,''), name) AS name_ru
            FROM supplements
            WHERE id IN ({placeholders})""",
        tuple(top_ids),
    )
    meta = {int(r["id"]): {"slug": str(r["slug"] or ""), "name_ru": str(r["name_ru"] or "")} for r in rows}
    out = []
    for sid in top_ids:
        row = meta.get(int(sid), {})
        out.append(
            {
                "supplement_id": int(sid),
                "slug": str(row.get("slug") or ""),
                "name_ru": str(row.get("name_ru") or ""),
                "streak_days": int(streak_map.get(int(sid), 0)),
            }
        )
    return out


async def build_stats_compact(conn: aiosqlite.Connection, tg_user_id: int, local_date: str) -> dict:
    today = await get_daily_adherence(conn, tg_user_id, local_date)
    last7 = await get_range_adherence(conn, tg_user_id, local_date, 7)
    last30 = await get_range_adherence(conn, tg_user_id, local_date, 30)
    misses7 = await get_top_misses(conn, tg_user_id, local_date, 7, limit=5)
    stack_ids = await get_user_stack_supplement_ids(conn, tg_user_id)
    streaks = await get_streaks(conn, tg_user_id, local_date, stack_ids, max_back=90)
    return {
        "today": today,
        "last7": {
            "expected": int(last7.get("expected_total") or 0),
            "taken": int(last7.get("taken_total") or 0),
            "adherence": last7.get("adherence"),
        },
        "last30": {
            "expected": int(last30.get("expected_total") or 0),
            "taken": int(last30.get("taken_total") or 0),
            "adherence": last30.get("adherence"),
        },
        "top_misses_7": [{"slug": str(x.get("slug") or ""), "missed": int(x.get("missed") or 0)} for x in misses7[:5]],
        "top_streaks": [{"slug": str(x.get("slug") or ""), "streak": int(x.get("streak_days") or 0)} for x in streaks[:5]],
    }


def _hhmm_to_int(value: str | None) -> int:
    hhmm = _normalize_hhmm(str(value or ""))
    if not hhmm:
        return 0
    h, m = hhmm.split(":")
    return int(h) * 100 + int(m)


async def get_user_metrics(conn: aiosqlite.Connection, tg_user_id: int, local_date: str) -> dict:
    user_id = await get_or_create_user(conn, int(tg_user_id))
    stats = await build_stats_compact(conn, tg_user_id, local_date)

    metrics = {
        "today_adherence": int(stats.get("today", {}).get("adherence") or 0),
        "today_expected": int(stats.get("today", {}).get("expected") or 0),
        "last7_adherence": int(stats.get("last7", {}).get("adherence") or 0),
        "last7_expected": int(stats.get("last7", {}).get("expected") or 0),
        "last30_adherence": int(stats.get("last30", {}).get("adherence") or 0),
        "last30_expected": int(stats.get("last30", {}).get("expected") or 0),
        "perfect_streak_days": 0,
    }

    cursor = date.fromisoformat(local_date)
    perfect_streak = 0
    for _ in range(30):
        daily = await get_daily_adherence(conn, tg_user_id, cursor.isoformat())
        expected = int(daily.get("expected") or 0)
        adherence = daily.get("adherence")
        if expected >= 3 and adherence == 100:
            perfect_streak += 1
            cursor = cursor - timedelta(days=1)
            continue
        break
    metrics["perfect_streak_days"] = perfect_streak

    row = await fetchone(conn, "SELECT COUNT(*) AS c FROM daily_checkins WHERE user_id=?", (user_id,))
    metrics["checkin_total"] = int((row["c"] if row else 0) or 0)
    row = await fetchone(conn, "SELECT COUNT(*) AS c FROM daily_checkins WHERE user_id=? AND applied_json IS NOT NULL AND TRIM(applied_json)!='' AND TRIM(applied_json)!='null'", (user_id,))
    metrics["apply_total"] = int((row["c"] if row else 0) or 0)
    checkin_rows = await fetchall(conn, "SELECT DISTINCT date_local FROM daily_checkins WHERE user_id=? ORDER BY date_local DESC", (user_id,))
    checkin_set = {str(r["date_local"]) for r in checkin_rows}
    streak = 0
    cur = date.fromisoformat(local_date)
    for _ in range(365):
        if cur.isoformat() in checkin_set:
            streak += 1
            cur -= timedelta(days=1)
        else:
            break
    metrics["checkin_streak_days"] = streak

    stack_ids = await get_user_stack_supplement_ids(conn, tg_user_id)
    metrics["stack_size"] = len(stack_ids)
    row = await fetchone(conn, "SELECT COUNT(*) AS c FROM user_complexes WHERE user_id=?", (user_id,))
    metrics["complex_count"] = int((row["c"] if row else 0) or 0)

    row = await fetchone(conn, "SELECT COUNT(*) AS c FROM user_supp_settings WHERE user_id=? AND reminder_enabled=1", (user_id,))
    metrics["reminders_enabled_count"] = int((row["c"] if row else 0) or 0)

    rows = await fetchall(conn, "SELECT slot_id, time_local FROM user_schedule_slots WHERE user_id=?", (user_id,))
    default_ids = {str(item.get("slot_id") or "") for item in default_user_slots()}
    custom_count = 0
    slot_times: list[int] = []
    for r in rows:
        slot_id = str(r["slot_id"] or "").strip()
        if slot_id.startswith("t_") or slot_id not in default_ids:
            custom_count += 1
        slot_int = _hhmm_to_int(str(r["time_local"] or ""))
        if slot_int > 0:
            slot_times.append(slot_int)
    metrics["custom_slot_count"] = custom_count
    row = await fetchone(conn, "SELECT COUNT(DISTINCT time_hhmm) AS c FROM user_schedules WHERE user_id=? AND is_enabled=1 AND time_hhmm IS NOT NULL AND TRIM(time_hhmm)!=''", (user_id,))
    metrics["distinct_schedule_times"] = int((row["c"] if row else 0) or 0)
    metrics["earliest_slot_hhmm"] = min(slot_times) if slot_times else 0
    metrics["latest_slot_hhmm"] = max(slot_times) if slot_times else 0

    benefit_count_by_system = {k: 0 for k in BODY_MAP_SYSTEM_KEYS}
    burden_count_by_system = {k: 0 for k in BODY_MAP_SYSTEM_KEYS}
    benefit_strength_sum_all = 0
    burden_strength_sum_core = 0
    evidence_ab_count = 0

    if stack_ids:
        placeholders = ",".join("?" for _ in stack_ids)
        supp_rows = await fetchall(conn, f"SELECT id, evidence_grade, body_effects_json FROM supplements WHERE id IN ({placeholders})", tuple(sorted(stack_ids)))
        for r in supp_rows:
            if str(r["evidence_grade"] or "").strip().upper() in {"A", "B"}:
                evidence_ab_count += 1
            try:
                effects = json.loads(r["body_effects_json"] or "[]")
                if not isinstance(effects, list):
                    effects = []
            except Exception:
                effects = []
            for effect in effects:
                if not isinstance(effect, dict):
                    continue
                system_key = _body_map_system_key(effect.get("system"))
                if not system_key:
                    continue
                direction = str(effect.get("direction") or "").strip().lower()
                try:
                    strength = int(effect.get("strength") or 0)
                except Exception:
                    strength = 0
                if strength < 0:
                    strength = 0
                if direction in {"benefit", "mixed"}:
                    benefit_count_by_system[system_key] += 1
                    benefit_strength_sum_all += strength
                if direction in {"burden", "mixed"}:
                    burden_count_by_system[system_key] += 1
                    if system_key in {"liver", "kidney", "gut"}:
                        burden_strength_sum_core += strength

    metrics.update({
        "systems_supported": sum(1 for key in BODY_MAP_SYSTEM_KEYS if int(benefit_count_by_system.get(key) or 0) > 0),
        "burden_strength_total_core": burden_strength_sum_core,
        "benefit_strength_total_all": benefit_strength_sum_all,
        "liver_burden_count": int(burden_count_by_system.get("liver") or 0),
        "kidney_burden_count": int(burden_count_by_system.get("kidney") or 0),
        "gut_burden_count": int(burden_count_by_system.get("gut") or 0),
        "brain_benefit_count": int(benefit_count_by_system.get("brain") or 0),
        "heart_benefit_count": int(benefit_count_by_system.get("heart") or 0),
        "gut_benefit_count": int(benefit_count_by_system.get("gut") or 0),
        "skin_benefit_count": int(benefit_count_by_system.get("skin") or 0),
        "immune_benefit_count": int(benefit_count_by_system.get("immune") or 0),
        "thyroid_benefit_count": int(benefit_count_by_system.get("thyroid") or 0),
        "joints_benefit_count": int(benefit_count_by_system.get("joints") or 0),
        "blood_benefit_count": int(benefit_count_by_system.get("blood") or 0),
        "lungs_benefit_count": int(benefit_count_by_system.get("lungs") or 0),
        "endocrine_benefit_count": int(benefit_count_by_system.get("endocrine") or 0),
        "muscle_benefit_count": int(benefit_count_by_system.get("muscle") or 0),
        "bone_benefit_count": int(benefit_count_by_system.get("bone") or 0),
        "pancreas_benefit_count": int(benefit_count_by_system.get("pancreas") or 0),
        "evidence_ab_count": evidence_ab_count,
    })

    row = await fetchone(conn, "SELECT 1 AS x FROM supplement_day_state WHERE user_id=? AND status='taken' LIMIT 1", (user_id,))
    metrics["has_any_taken"] = 1 if row else 0

    streaks = await get_streaks(conn, tg_user_id, local_date, stack_ids, max_back=90)
    metrics["max_single_supp_streak"] = max([int(item.get("streak_days") or 0) for item in streaks], default=0)
    return metrics


def _compare_values(left: float, op: str, right: float) -> bool:
    if op == ">=":
        return left >= right
    if op == "<=":
        return left <= right
    if op == "==":
        return left == right
    if op == ">":
        return left > right
    if op == "<":
        return left < right
    return False


def _eval_rule(rule: dict, metrics: dict) -> bool:
    if not isinstance(rule, dict):
        return False
    if isinstance(rule.get("all"), list):
        return all(_eval_rule(item, metrics) for item in rule.get("all", []))
    if isinstance(rule.get("any"), list):
        return any(_eval_rule(item, metrics) for item in rule.get("any", []))
    metric = str(rule.get("metric") or "").strip()
    op = str(rule.get("op") or "").strip()
    if not metric or not op:
        return False
    left = float(metrics.get(metric) or 0)
    if "value_metric" in rule:
        right = float(metrics.get(str(rule.get("value_metric") or "").strip()) or 0)
    else:
        right = float(rule.get("value") or 0)
    return _compare_values(left, op, right)


def compute_allowed_cosmetics(unlocked_rewards: list[dict]) -> dict:
    themes = {"base"}
    tab_skins = {"classic"}
    badges = {"none"}
    for reward in unlocked_rewards:
        unlock = dict((reward or {}).get("unlock") or {})
        themes.update([str(x) for x in list(unlock.get("themes") or []) if str(x).strip()])
        tab_skins.update([str(x) for x in list(unlock.get("tab_skins") or []) if str(x).strip()])
        badges.update([str(x) for x in list(unlock.get("badges") or []) if str(x).strip()])
    return {"themes": sorted(themes), "tab_skins": sorted(tab_skins), "badges": sorted(badges)}


async def evaluate_and_unlock_achievements(conn, tg_user_id: int, local_date: str) -> list[str]:
    from achievements import ACHIEVEMENTS

    await bootstrap_achievements(conn)
    metrics = await get_user_metrics(conn, tg_user_id, local_date)
    user_id = await get_or_create_user(conn, int(tg_user_id))
    now = utc_now_iso()
    newly: list[str] = []
    for ach in ACHIEVEMENTS:
        key = str(ach.get("key") or "").strip()
        if not key:
            continue
        if not _eval_rule(ach.get("rule") or {}, metrics):
            continue
        before = conn.total_changes
        await conn.execute(
            "INSERT OR IGNORE INTO user_achievements(user_id, achievement_key, unlocked_at_utc, meta_json) VALUES(?,?,?,?)",
            (user_id, key, now, "{}"),
        )
        if conn.total_changes > before:
            newly.append(key)
    await conn.commit()
    return newly


async def get_user_cosmetics(conn, tg_user_id:int) -> dict:
    user_id = await get_or_create_user(conn, int(tg_user_id))
    await conn.execute("INSERT OR IGNORE INTO user_cosmetics(user_id, updated_at_utc) VALUES(?,?)", (user_id, utc_now_iso()))
    row = await fetchone(conn, "SELECT theme_id, tab_skin_id, badge_id FROM user_cosmetics WHERE user_id=?", (user_id,))
    return {
        "theme_id": str((row["theme_id"] if row else "base") or "base"),
        "tab_skin_id": str((row["tab_skin_id"] if row else "classic") or "classic"),
        "badge_id": str((row["badge_id"] if row else "none") or "none"),
    }


async def set_user_cosmetics(conn, tg_user_id:int, patch:dict, allowed:dict) -> dict:
    current = await get_user_cosmetics(conn, tg_user_id)
    updated = dict(current)
    for key, allow_key in (("theme_id", "themes"), ("tab_skin_id", "tab_skins"), ("badge_id", "badges")):
        if key not in patch or patch.get(key) is None:
            continue
        value = str(patch.get(key) or "").strip()
        if value not in set(allowed.get(allow_key) or []):
            raise ValueError("cosmetic_locked")
        updated[key] = value
    user_id = await get_or_create_user(conn, int(tg_user_id))
    await conn.execute(
        "UPDATE user_cosmetics SET theme_id=?, tab_skin_id=?, badge_id=?, updated_at_utc=? WHERE user_id=?",
        (updated["theme_id"], updated["tab_skin_id"], updated["badge_id"], utc_now_iso(), user_id),
    )
    await conn.commit()
    return updated




async def body_map_aggregate(conn: aiosqlite.Connection, tg_user_id: int, local_date: str) -> dict:
    await get_or_create_user(conn, tg_user_id)
    ids = await get_today_supplement_ids(conn, tg_user_id, local_date)
    systems = {
        key: {
            "benefit_count": 0,
            "burden_count": 0,
            "benefit_strength_sum": 0,
            "burden_strength_sum": 0,
            "top": [],
        }
        for key in BODY_MAP_SYSTEM_KEYS
    }
    if not ids:
        return {"day": local_date, "systems": systems, "max_count": 0}

    placeholders = ",".join("?" for _ in ids)
    rows = await fetchall(
        conn,
        f"""SELECT id, slug, COALESCE(NULLIF(name_ru,''), NULLIF(name_en,''), name) AS title, body_effects_json
            FROM supplements
            WHERE id IN ({placeholders})""",
        tuple(ids),
    )
    for row in rows:
        sid = int(row["id"] or 0)
        slug = str(row["slug"] or "").strip()
        title = str(row["title"] or "").strip()
        raw_effects = row["body_effects_json"]
        try:
            effects = json.loads(raw_effects or "[]")
            if not isinstance(effects, list):
                effects = []
        except Exception:
            effects = []

        for effect in effects:
            if not isinstance(effect, dict):
                continue
            system_key = _body_map_system_key(effect.get("system"))
            if not system_key:
                continue
            direction = str(effect.get("direction") or "").strip().lower()
            try:
                strength = int(effect.get("strength") or 0)
            except Exception:
                strength = 0
            if strength < 0:
                strength = 0

            entry = systems[system_key]
            if direction in {"benefit", "mixed"}:
                entry["benefit_count"] += 1
                entry["benefit_strength_sum"] += strength
            if direction in {"burden", "mixed"}:
                entry["burden_count"] += 1
                entry["burden_strength_sum"] += strength
            if direction not in {"benefit", "burden", "mixed"}:
                continue
            entry["top"].append(
                {
                    "supplement_id": sid,
                    "slug": slug,
                    "name_ru": title,
                    "direction": direction,
                    "strength": strength,
                }
            )

    max_count = 0
    for key in BODY_MAP_SYSTEM_KEYS:
        top = systems[key]["top"]
        top.sort(
            key=lambda item: (
                0 if item.get("direction") == "burden" else 1,
                -(int(item.get("strength") or 0)),
                str(item.get("name_ru") or ""),
            )
        )
        systems[key]["top"] = top[:12]
        total = int(systems[key]["benefit_count"] or 0) + int(systems[key]["burden_count"] or 0)
        if total > max_count:
            max_count = total

    return {"day": local_date, "systems": systems, "max_count": max_count}

async def get_user_slots(conn: aiosqlite.Connection, user_id: int) -> list[dict]:
    rows = await fetchall(
        conn,
        """SELECT slot_id, title_ru, time_local, is_enabled
           FROM user_schedule_slots
           WHERE user_id=?""",
        (user_id,),
    )
    slots = []
    for row in rows:
        time_local = _normalize_hhmm(str(row["time_local"] or "")) or ""
        if not _is_valid_hhmm(time_local):
            continue
        slots.append(
            {
                "slot_id": str(row["slot_id"] or "").strip(),
                "title_ru": str(row["title_ru"] or "Слот").strip() or "Слот",
                "time_local": time_local,
                "is_enabled": bool(int(row["is_enabled"] or 0)),
            }
        )

    by_id = {str(slot.get("slot_id") or ""): slot for slot in slots if str(slot.get("slot_id") or "")}
    changed = False
    for base in default_user_slots():
        base_id = str(base["slot_id"])
        if base_id not in by_id:
            by_id[base_id] = dict(base)
            changed = True

    out = list(by_id.values())
    out.sort(key=lambda item: (str(item.get("time_local") or "99:99"), str(item.get("slot_id") or "")))

    if changed or len(out) != len(rows):
        await set_user_slots(conn, user_id, out)
        return await get_user_slots(conn, user_id)
    return out


async def set_user_slots(conn: aiosqlite.Connection, user_id: int, slots_list: list[dict]) -> None:
    slots = _normalize_slots(slots_list)
    by_id = {str(slot.get("slot_id") or ""): dict(slot) for slot in slots}
    for base in default_user_slots():
        base_id = str(base["slot_id"])
        if base_id not in by_id:
            by_id[base_id] = dict(base)

    await conn.execute("DELETE FROM user_schedule_slots WHERE user_id=?", (user_id,))
    for slot in by_id.values():
        await conn.execute(
            """INSERT INTO user_schedule_slots(user_id, slot_id, title_ru, time_local, is_enabled)
               VALUES(?,?,?,?,?)""",
            (
                user_id,
                str(slot.get("slot_id") or "").strip(),
                str(slot.get("title_ru") or "Слот").strip() or "Слот",
                str(slot.get("time_local") or "08:00").strip(),
                1 if slot.get("is_enabled", True) else 0,
            ),
        )
    await conn.commit()


async def upsert_user_slot(
    conn: aiosqlite.Connection,
    user_id: int,
    slot_id: str,
    title_ru: str,
    time_local: str,
    is_enabled: int = 1,
) -> None:
    normalized = _normalize_hhmm(time_local)
    if not normalized:
        raise ValueError("invalid HH:MM")
    await conn.execute(
        """INSERT INTO user_schedule_slots(user_id, slot_id, title_ru, time_local, is_enabled)
           VALUES(?,?,?,?,?)
           ON CONFLICT(user_id, slot_id) DO UPDATE SET
             title_ru=excluded.title_ru,
             time_local=excluded.time_local,
             is_enabled=excluded.is_enabled""",
        (user_id, slot_id, (title_ru or "Слот").strip(), normalized, 1 if is_enabled else 0),
    )
    await conn.commit()


async def set_user_slot_time(conn: aiosqlite.Connection, user_id: int, slot_id: str, time_local: str) -> None:
    normalized = _normalize_hhmm(time_local)
    if not normalized:
        raise ValueError("invalid HH:MM")
    slots = await get_user_slots(conn, user_id)
    existing = next((s for s in slots if str(s.get("slot_id") or "") == slot_id), None)
    if existing:
        title = str(existing.get("title_ru") or "Слот")
    else:
        title = normalized if slot_id.startswith("t_") else "Слот"
    await upsert_user_slot(conn, user_id, slot_id, title, normalized, is_enabled=1)


async def add_custom_slot(conn: aiosqlite.Connection, user_id: int, time_local: str, title_ru: str | None = None) -> str:
    normalized = _normalize_hhmm(time_local)
    if not normalized:
        raise ValueError("invalid HH:MM")
    base = f"t_{normalized.replace(':', '')}"
    slot_id = base
    n = 2
    while await fetchone(conn, "SELECT 1 FROM user_schedule_slots WHERE user_id=? AND slot_id=?", (user_id, slot_id)):
        slot_id = f"{base}_{n}"
        n += 1
    await upsert_user_slot(conn, user_id, slot_id, (title_ru or normalized), normalized, is_enabled=1)
    return slot_id


async def set_schedule_slot(conn: aiosqlite.Connection, user_id: int, supplement_id: int, slot_id: str) -> None:
    await conn.execute(
        """INSERT INTO user_schedules(user_id, supplement_id, time_hhmm, slot_id, is_enabled)
           VALUES(?, ?, NULL, ?, 1)
           ON CONFLICT(user_id, supplement_id) DO UPDATE SET
             slot_id=excluded.slot_id,
             is_enabled=1""",
        (user_id, supplement_id, slot_id),
    )
    await conn.commit()


async def set_schedule_time_override(
    conn: aiosqlite.Connection,
    user_id: int,
    supplement_id: int,
    time_hhmm: str | None,
) -> None:
    normalized = None
    if time_hhmm is not None:
        normalized = _normalize_hhmm(time_hhmm)
        if not normalized:
            raise ValueError("invalid HH:MM")
    await conn.execute(
        """INSERT INTO user_schedules(user_id, supplement_id, time_hhmm, slot_id, is_enabled)
           VALUES(?, ?, ?, 'morning', 1)
           ON CONFLICT(user_id, supplement_id) DO UPDATE SET
             time_hhmm=excluded.time_hhmm,
             is_enabled=1""",
        (user_id, supplement_id, normalized),
    )
    await conn.execute(
        "UPDATE user_schedules SET slot_id='morning' WHERE user_id=? AND supplement_id=? AND (slot_id IS NULL OR slot_id='')",
        (user_id, supplement_id),
    )
    await conn.commit()


async def get_user_schedule_map(conn: aiosqlite.Connection, user_id: int) -> dict[int, dict]:
    rows = await fetchall(
        conn,
        "SELECT supplement_id, slot_id, time_hhmm FROM user_schedules WHERE user_id=?",
        (user_id,),
    )
    out: dict[int, dict] = {}
    for row in rows:
        sid = int(row["supplement_id"] or 0)
        out[sid] = {
            "slot_id": str(row["slot_id"] or "").strip() or "morning",
            "time_hhmm": _normalize_hhmm(str(row["time_hhmm"] or "")) if row["time_hhmm"] else None,
        }
    return out


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

async def run_diagnostics(conn: aiosqlite.Connection, apply: bool = False) -> dict:
    issues: list[dict] = []
    details: dict[str, list[dict]] = {}

    async def push(code: str, rows: list[dict], fixed: int = 0, note: str = "") -> None:
        issues.append({"code": code, "count": len(rows), "fixed": int(fixed), "note": note})
        if rows:
            details[code] = rows[:50]

    async def duplicates(table: str, keys: list[str], order_col: str | None = None) -> tuple[list[dict], int]:
        cols = await _table_columns(conn, table)
        if not all(k in cols for k in keys):
            return [], 0
        g = ", ".join(keys)
        rows = await fetchall(conn, f"SELECT {g}, COUNT(*) AS c FROM {table} GROUP BY {g} HAVING c>1")
        report = []
        fixed = 0
        for r in rows:
            where = " AND ".join([f"{k}=?" for k in keys])
            params = tuple(r[k] for k in keys)
            all_rows = await fetchall(conn, f"SELECT rowid, * FROM {table} WHERE {where} ORDER BY rowid DESC", params)
            kept = int(all_rows[0]["rowid"]) if all_rows else 0
            removed = max(0, len(all_rows) - 1)
            rec = {k: r[k] for k in keys}
            rec.update({"kept_rowid": kept, "removed": removed})
            report.append(rec)
            if apply and removed > 0:
                drop_ids = [int(x["rowid"]) for x in all_rows[1:]]
                qs = ",".join(["?"] * len(drop_ids))
                await conn.execute(f"DELETE FROM {table} WHERE rowid IN ({qs})", tuple(drop_ids))
                fixed += removed
        return report, fixed

    if apply:
        await conn.execute("BEGIN")
    try:
        rows, fixed = await duplicates("supplement_day_state", ["user_id", "day", "supplement_id"], "created_at")
        await push("DUP_SDS", rows, fixed, "Удалены дубли, сохранена последняя запись")

        rows, fixed = await duplicates("intake_log", ["user_id", "date_local", "slot_id"], "updated_at")
        await push("DUP_REMINDER_LOG", rows, fixed, "Дубли логов напоминаний удалены")

        orphan_ci = await fetchall(conn, """SELECT i.rowid, i.complex_id, i.supplement_id FROM user_complex_items i
            LEFT JOIN user_complexes c ON c.id=i.complex_id WHERE c.id IS NULL""")
        if apply and orphan_ci:
            await conn.execute("DELETE FROM user_complex_items WHERE rowid IN (%s)" % ",".join(["?"]*len(orphan_ci)), tuple(int(r["rowid"]) for r in orphan_ci))
        await push("ORPHAN_COMPLEX_ITEMS", [dict(r) for r in orphan_ci], len(orphan_ci) if apply else 0, "Безопасно удалены сироты")

        orphan_cs = await fetchall(conn, """SELECT s.rowid, s.user_id, s.complex_id, s.slot_id FROM user_complex_schedules s
            LEFT JOIN user_complexes c ON c.id=s.complex_id WHERE c.id IS NULL""")
        if apply and orphan_cs:
            await conn.execute("DELETE FROM user_complex_schedules WHERE rowid IN (%s)" % ",".join(["?"]*len(orphan_cs)), tuple(int(r["rowid"]) for r in orphan_cs))
        await push("ORPHAN_COMPLEX_SCHEDULES", [dict(r) for r in orphan_cs], len(orphan_cs) if apply else 0, "Безопасно удалены сироты")

        orphan_sched = await fetchall(conn, """SELECT s.rowid, s.user_id, s.supplement_id, s.slot_id FROM user_schedules s
            LEFT JOIN supplements x ON x.id=s.supplement_id WHERE x.id IS NULL""")
        orphan_plan = await fetchall(conn, """SELECT p.rowid, p.user_id, p.date_local, p.supplement_id FROM day_plan_items p
            LEFT JOIN supplements x ON x.id=p.supplement_id WHERE x.id IS NULL""")
        if apply and orphan_sched:
            await conn.execute("DELETE FROM user_schedules WHERE rowid IN (%s)" % ",".join(["?"]*len(orphan_sched)), tuple(int(r["rowid"]) for r in orphan_sched))
        if apply and orphan_plan:
            await conn.execute("DELETE FROM day_plan_items WHERE rowid IN (%s)" % ",".join(["?"]*len(orphan_plan)), tuple(int(r["rowid"]) for r in orphan_plan))
        await push("ORPHAN_SUPP_SCHEDULES", [dict(r) for r in orphan_sched], len(orphan_sched) if apply else 0, "Удалены расписания с несуществующей добавкой")
        await push("ORPHAN_PLAN_ITEMS", [dict(r) for r in orphan_plan], len(orphan_plan) if apply else 0, "Удалены план-строки с несуществующей добавкой")

        disabled_rows = await fetchall(conn, """SELECT p.rowid, p.user_id, p.date_local, p.supplement_id FROM day_plan_items p
            JOIN supplements s ON s.id=p.supplement_id WHERE COALESCE(s.is_active,1)=0""")
        if apply and disabled_rows:
            await conn.execute("DELETE FROM day_plan_items WHERE rowid IN (%s)" % ",".join(["?"]*len(disabled_rows)), tuple(int(r["rowid"]) for r in disabled_rows))
        await push("DISABLED_SUPP_IN_PLAN", [dict(r) for r in disabled_rows], len(disabled_rows) if apply else 0, "Неактивные добавки убраны из day plan")

        bad_time_rows = []
        manual_rows = []
        for table, time_col, slot_col in [("user_schedule_slots", "time_local", "slot_id"), ("user_complex_schedules", "time_local", "slot_id"), ("user_schedules", "time_hhmm", "slot_id")]:
            if not await _table_exists(conn, table):
                continue
            rows = await fetchall(conn, f"SELECT rowid, {time_col} AS t, {slot_col} AS slot_id FROM {table}")
            for r in rows:
                norm = _normalize_hhmm(str(r["t"] or "").strip()) if r["t"] is not None else None
                if r["t"] and norm:
                    if str(r["t"]) != norm:
                        bad_time_rows.append({"table": table, "rowid": int(r["rowid"]), "from": str(r["t"]), "to": norm})
                        if apply:
                            await conn.execute(f"UPDATE {table} SET {time_col}=? WHERE rowid=?", (norm, int(r["rowid"])))
                elif r["t"]:
                    manual_rows.append({"table": table, "rowid": int(r["rowid"]), "bad": str(r["t"])})
                    if apply and table in {"user_complex_schedules", "user_schedules", "user_schedule_slots"}:
                        await conn.execute(f"UPDATE {table} SET is_enabled=0 WHERE rowid=?", (int(r["rowid"]),))
        await push("BAD_TIME_FORMAT", bad_time_rows, len(bad_time_rows) if apply else 0, "Время нормализовано до HH:MM")
        await push("BAD_TIME_MANUAL", manual_rows, len(manual_rows) if apply else 0, "Неразбираемые значения отключены")

        slot_norm_rows = []
        for table, time_col in [("user_schedules", "time_hhmm"), ("user_complex_schedules", "time_local")]:
            if not await _table_exists(conn, table):
                continue
            rows = await fetchall(conn, f"SELECT rowid, slot_id, {time_col} AS t FROM {table}")
            for r in rows:
                sid, t = _normalize_slot_id(str(r["slot_id"] or ""), str(r["t"] or ""))
                if sid and sid != str(r["slot_id"] or ""):
                    slot_norm_rows.append({"table": table, "rowid": int(r["rowid"]), "from": str(r["slot_id"] or ""), "to": sid})
                    if apply:
                        await conn.execute(f"UPDATE {table} SET slot_id=?, {time_col}=COALESCE({time_col}, ?) WHERE rowid=?", (sid, t or "", int(r["rowid"])))
        await push("SLOT_ID_NORMALIZATION", slot_norm_rows, len(slot_norm_rows) if apply else 0, "Кастомные slot_id приведены к t_HHMM")

        deleted_not_banned = await fetchall(conn, "SELECT tg_user_id, deleted_at_utc, is_banned FROM users WHERE COALESCE(deleted_at_utc,'')!='' AND COALESCE(is_banned,0)=0")
        if apply and deleted_not_banned:
            await conn.execute("UPDATE users SET is_banned=1 WHERE COALESCE(deleted_at_utc,'')!='' AND COALESCE(is_banned,0)=0")
        await push("USER_DELETED_NOT_BANNED", [dict(r) for r in deleted_not_banned], len(deleted_not_banned) if apply else 0, "deleted user -> banned=1")

        empty_complex = await fetchall(conn, """SELECT c.id AS complex_id, c.user_id FROM user_complexes c
            LEFT JOIN user_complex_items i ON i.complex_id=c.id GROUP BY c.id HAVING COUNT(i.supplement_id)=0""")
        fixed_empty = 0
        if apply and empty_complex:
            for r in empty_complex:
                cur = await conn.execute("UPDATE user_complex_schedules SET is_enabled=0 WHERE complex_id=? AND is_enabled!=0", (int(r["complex_id"]),))
                fixed_empty += int(cur.rowcount or 0)
        await push("COMPLEX_EMPTY", [dict(r) for r in empty_complex], fixed_empty if apply else 0, "Пустые комплексы сохранены, их расписания отключены")

        if apply:
            await _ensure_integrity_unique_indexes(conn)
            await conn.commit()
    except Exception:
        if apply:
            await conn.rollback()
        raise

    return {"ok": True, "apply": bool(apply), "issues": issues, "details": details}
