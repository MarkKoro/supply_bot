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
    OPENAI_API_KEY: str | None
    OPENAI_MODEL: str
    AI_MAX_PER_DAY: int

def get_settings() -> Settings:
    tg = os.getenv("TG_TOKEN", "").strip()
    if not tg:
        raise RuntimeError("TG_TOKEN is missing in .env")

    return Settings(
        TG_TOKEN=tg,
        APP_TZ=os.getenv("APP_TZ", "UTC").strip(),
        DB_PATH=os.getenv("DB_PATH", "data/supply_bot.sqlite3").strip(),
        OPENAI_API_KEY=os.getenv("OPENAI_API_KEY", "").strip() or None,
        OPENAI_MODEL=os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip(),
        AI_MAX_PER_DAY=int(os.getenv("AI_MAX_PER_DAY", "1").strip()),
    )
