from __future__ import annotations

from datetime import datetime, timezone

TZ_CITY = {
    -12: "Бейкер-Айленд",
    -11: "Паго-Паго",
    -10: "Гонолулу",
    -9: "Анкоридж",
    -8: "Лос-Анджелес",
    -7: "Денвер",
    -6: "Мехико",
    -5: "Нью-Йорк",
    -4: "Сантьяго",
    -3: "Буэнос-Айрес",
    -2: "Фернанду-ди-Норонья",
    -1: "Понта-Делгада",
    0: "Лондон",
    1: "Берлин",
    2: "Каир",
    3: "Москва",
    4: "Дубай",
    5: "Карачи",
    6: "Дакка",
    7: "Бангкок",
    8: "Пекин",
    9: "Токио",
    10: "Сидней",
    11: "Нумеа",
    12: "Окленд",
    13: "Апиа",
    14: "Киритимати",
}


def now_utc_hhmm() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M")

def catalog() -> list[int]:
    return list(range(-12, 15))


def format_offset_tag(hours: int) -> str:
    return f"{hours:+03d}"


def format_offset_label(hours: int) -> str:
    tag = format_offset_tag(hours)
    label = f"GMT{tag}"
    city = TZ_CITY.get(hours)
    if city:
        return f"{label} ({city})"
    return label


def format_confirmation_label(tz_name: str) -> str:
    hours = parse_tz_hours(tz_name)
    if hours is None:
        return tz_name
    tag = format_offset_tag(hours)
    label = f"GMT{tag}:00"
    city = TZ_CITY.get(hours)
    if city:
        return f"{label} ({city})"
    return label


def parse_tz_hours(tz_name: str) -> int | None:
    if not tz_name:
        return None
    value = tz_name.strip().upper()
    if value in {"UTC", "GMT"}:
        return 0
    if value.startswith(("UTC", "GMT")):
        value = value[3:]
    if value.startswith(("+", "-")):
        try:
            return int(value[:3])
        except ValueError:
            return None
    return None


def tz_name_from_tag(tag: str) -> str:
    hours = int(tag)
    return f"GMT{format_offset_tag(hours)}:00"
