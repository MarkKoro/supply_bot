from __future__ import annotations

from datetime import datetime, timezone

RU_TZ_CITY = {
    2: "Калининград",
    3: "Москва",
    4: "Самара",
    5: "Екатеринбург",
    6: "Омск",
    7: "Красноярск",
    8: "Иркутск",
    9: "Якутск",
    10: "Владивосток",
    11: "Магадан",
    12: "Камчатка",
}


def now_utc_hhmm() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M")

def catalog() -> list[int]:
    return list(range(-12, 15))


def format_offset_tag(hours: int) -> str:
    return f"{hours:+03d}"


def format_offset_label(hours: int) -> str:
    tag = format_offset_tag(hours)
    label = f"UTC{tag}"
    city = RU_TZ_CITY.get(hours)
    if city:
        return f"{label} ({city})"
    return label


def format_confirmation_label(tz_name: str) -> str:
    hours = parse_tz_hours(tz_name)
    if hours is None:
        return tz_name
    tag = format_offset_tag(hours)
    label = f"UTC{tag}:00"
    city = RU_TZ_CITY.get(hours)
    if city:
        return f"{label} ({city})"
    return label


def parse_tz_hours(tz_name: str) -> int | None:
    if not tz_name:
        return None
    value = tz_name.strip().upper()
    if value == "UTC":
        return 0
    if value.startswith("UTC"):
        value = value[3:]
    if value.startswith(("+", "-")):
        try:
            return int(value[:3])
        except ValueError:
            return None
    return None


def tz_name_from_tag(tag: str) -> str:
    hours = int(tag)
    return f"UTC{format_offset_tag(hours)}:00"
