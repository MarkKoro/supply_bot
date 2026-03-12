from __future__ import annotations
import os
import json
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
    OPENAI_TRANSCRIBE_MODEL: str
    OPENAI_STT_MODEL: str
    AI_MAX_PER_DAY: int
    DEBUG: bool
    ADMIN_IDS: list[int]
    ADMIN_TG_IDS: set[int]
    OWNER_TG_ID: int
    WEBAPP_URL: str
    PUBLIC_BASE_URL: str
    PROJECT_KEY: str
    WEBAPP_PUBLIC_PATH: str
    YOOKASSA_RETURN_URL: str
    BOT_USERNAME: str
    PAYMENTS_EXTERNAL_ONLY: bool
    YOOKASSA_PROVIDER_TOKEN: str
    YOOKASSA_SHOP_ID: str
    YOOKASSA_SECRET_KEY: str
    YOOKASSA_WIDGET_ENABLED: bool
    BILLING_ENABLED_RUB: bool
    BILLING_ENABLED_STARS: bool
    PREMIUM_PRICES_RUB: dict[str, int]
    PREMIUM_PRICES_STARS: dict[str, int]
    PREMIUM_PLANS: dict[str, int]
    OFFER_VERSION: str
    PRIVACY_VERSION: str
    OFFER_URL: str
    PRIVACY_URL: str

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


def get_admin_ids() -> set[int]:
    return set(_parse_admin_ids(os.getenv("ADMIN_TG_IDS", "")))


def is_admin(tg_user_id: int) -> bool:
    return tg_user_id in get_admin_ids()

def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_prices_map(value: str, fallback: dict[str, int]) -> dict[str, int]:
    raw = (value or "").strip()
    if not raw:
        return dict(fallback)
    try:
        parsed = json.loads(raw)
    except Exception:
        return dict(fallback)
    if not isinstance(parsed, dict):
        return dict(fallback)
    out: dict[str, int] = {}
    for key, amount in parsed.items():
        k = str(key).strip()
        if not k:
            continue
        try:
            val = int(amount)
        except Exception:
            continue
        if val > 0:
            out[k] = val
    return out or dict(fallback)

def get_settings() -> Settings:
    tg = os.getenv("TG_TOKEN", "").strip()
    if not tg:
        raise RuntimeError("TG_TOKEN is missing in .env")

    premium_prices_rub_default = {
        "m1": 20000,
        "m3": 54000,
        "m6": 96000,
        "m12": 168000,
    }
    premium_prices_stars_default = {
        "m1": 250,
        "m3": 650,
        "m6": 1200,
        "m12": 2000,
    }
    premium_plans_default = {
        "m1": 1,
        "m3": 3,
        "m6": 6,
        "m12": 12,
    }
    yookassa_provider_token = os.getenv("YOOKASSA_PROVIDER_TOKEN", "").strip()
    yookassa_shop_id = os.getenv("YOOKASSA_SHOP_ID", "").strip()
    yookassa_secret_key = os.getenv("YOOKASSA_SECRET_KEY", "").strip()
    public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip()
    project_key = os.getenv("PROJECT_KEY", "supply_bot").strip() or "supply_bot"
    webapp_public_path = os.getenv("WEBAPP_PUBLIC_PATH", "/supply").strip() or "/supply"
    if not webapp_public_path.startswith("/"):
        webapp_public_path = f"/{webapp_public_path}"
    yookassa_return_url = os.getenv("YOOKASSA_RETURN_URL", "").strip()
    if not yookassa_return_url:
        base = public_base_url.rstrip("/")
        path = webapp_public_path.rstrip("/")
        yookassa_return_url = f"{base}{path}/payment-return.html" if base else f"{path}/payment-return.html"

    billing_enabled_rub_raw = os.getenv("BILLING_ENABLED_RUB", "").strip()
    billing_enabled_rub = _parse_bool(billing_enabled_rub_raw) if billing_enabled_rub_raw else bool(yookassa_shop_id and yookassa_secret_key)

    admin_ids = _parse_admin_ids(os.getenv("ADMIN_IDS", ""))
    admin_tg_ids = get_admin_ids() or set(admin_ids)
    owner_tg_id = int(os.getenv("OWNER_TG_ID", "0").strip() or "0")

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
        OPENAI_TRANSCRIBE_MODEL=os.getenv("OPENAI_TRANSCRIBE_MODEL", "whisper-1").strip(),
        OPENAI_STT_MODEL=(
            os.getenv("OPENAI_STT_MODEL", "").strip()
            or os.getenv("OPENAI_TRANSCRIBE_MODEL", "whisper-1").strip()
        ),
        AI_MAX_PER_DAY=int(os.getenv("AI_MAX_PER_DAY", "1").strip()),
        DEBUG=_parse_bool(os.getenv("DEBUG", "")),
        ADMIN_IDS=admin_ids,
        ADMIN_TG_IDS=admin_tg_ids,
        OWNER_TG_ID=owner_tg_id,
        WEBAPP_URL=os.getenv("WEBAPP_URL", "").strip(),
        PUBLIC_BASE_URL=public_base_url,
        PROJECT_KEY=project_key,
        WEBAPP_PUBLIC_PATH=webapp_public_path,
        YOOKASSA_RETURN_URL=yookassa_return_url,
        BOT_USERNAME=os.getenv("BOT_USERNAME", "").strip(),
        PAYMENTS_EXTERNAL_ONLY=_parse_bool(os.getenv("PAYMENTS_EXTERNAL_ONLY", "")),
        YOOKASSA_PROVIDER_TOKEN=yookassa_provider_token,
        YOOKASSA_SHOP_ID=yookassa_shop_id,
        YOOKASSA_SECRET_KEY=yookassa_secret_key,
        YOOKASSA_WIDGET_ENABLED=_parse_bool(os.getenv("YOOKASSA_WIDGET_ENABLED", "")),
        BILLING_ENABLED_RUB=billing_enabled_rub,
        BILLING_ENABLED_STARS=_parse_bool(os.getenv("BILLING_ENABLED_STARS", "1")),
        PREMIUM_PRICES_RUB=_parse_prices_map(os.getenv("PREMIUM_PRICES_RUB", ""), premium_prices_rub_default),
        PREMIUM_PRICES_STARS=_parse_prices_map(os.getenv("PREMIUM_PRICES_STARS", ""), premium_prices_stars_default),
        PREMIUM_PLANS=_parse_prices_map(os.getenv("PREMIUM_PLANS", ""), premium_plans_default),
        OFFER_VERSION=os.getenv("OFFER_VERSION", "2026-03-08").strip(),
        PRIVACY_VERSION=os.getenv("PRIVACY_VERSION", "2026-03-08").strip(),
        OFFER_URL=os.getenv("OFFER_URL", "https://telegra.ph/Oferta-03-08-3").strip(),
        PRIVACY_URL=os.getenv("PRIVACY_URL", "https://telegra.ph/Popd-03-08").strip(),
    )
