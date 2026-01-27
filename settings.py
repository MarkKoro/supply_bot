from __future__ import annotations
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Settings:
    TG_TOKEN: str
    APP_TZ: str
    DB_PATH: str
    SEED_SUPPLEMENTS_PATH: str
    SEED_INTERACTIONS_PATH: str
    AUTO_SEED_ON_START: bool
    OPENAI_API_KEY: str | None
    OPENAI_MODEL: str
    AI_MAX_PER_DAY: int
    DEBUG: bool
    ADMIN_IDS: list[int]

def _parse_admin_ids(value: str) -> list[int]:
    ids: list[int] = []
    for raw in value.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            ids.append(int(raw))
        except ValueError:
            continue
    return ids

def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}

def get_settings() -> Settings:
    tg = os.getenv("TG_TOKEN", "").strip()
    if not tg:
        raise RuntimeError("TG_TOKEN is missing in .env")

    return Settings(
        TG_TOKEN=tg,
        APP_TZ=os.getenv("APP_TZ", "UTC").strip(),
        DB_PATH=os.getenv("DB_PATH", "data/supply_bot.sqlite3").strip(),
        SEED_SUPPLEMENTS_PATH=os.getenv(
            "SEED_SUPPLEMENTS_PATH",
            "data/seed_rich/supplements_rich.jsonl",
        ).strip(),
        SEED_INTERACTIONS_PATH=os.getenv(
            "SEED_INTERACTIONS_PATH",
            "data/seed_rich/interactions_rich.jsonl",
        ).strip(),
        AUTO_SEED_ON_START=bool(int(os.getenv("AUTO_SEED_ON_START", "1").strip() or "1")),
        OPENAI_API_KEY=os.getenv("OPENAI_API_KEY", "").strip() or None,
        OPENAI_MODEL=os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip(),
        AI_MAX_PER_DAY=int(os.getenv("AI_MAX_PER_DAY", "1").strip()),
        DEBUG=_parse_bool(os.getenv("DEBUG", "")),
        ADMIN_IDS=_parse_admin_ids(os.getenv("ADMIN_IDS", "")),
    )
