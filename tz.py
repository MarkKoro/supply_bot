from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

RU_TZ_OFFSETS: list[tuple[str, str]] = [
    ("+03:00", "Москва"),
    ("+05:00", "Екатеринбург"),
    ("+04:00", "Самара"),
    ("+02:00", "Калининград"),
    ("+06:00", "Омск"),
    ("+07:00", "Красноярск"),
    ("+08:00", "Иркутск"),
    ("+09:00", "Якутск"),
    ("+10:00", "Владивосток"),
    ("+11:00", "Магадан"),
    ("+12:00", "Камчатка"),
]

RU_TZ_CITY = {offset: city for offset, city in RU_TZ_OFFSETS}


def parse_offset(value: str) -> timezone:
    if not re.fullmatch(r"[+-](?:0\\d|1\\d|2[0-3]):[0-5]\\d", value):
        return timezone.utc
    sign = 1 if value[0] == "+" else -1
    hours_str, minutes_str = value[1:].split(":")
    offset = timedelta(hours=int(hours_str), minutes=int(minutes_str))
    return timezone(sign * offset)


def now_utc_hhmm() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M")


def now_local_hhmm(offset: str) -> str:
    tz = parse_offset(offset)
    return datetime.now(tz).strftime("%H:%M")


def format_offset_label(offset: str, *, include_city: bool = True) -> str:
    city = RU_TZ_CITY.get(offset) if include_city else None
    label = f"UTC{offset}"
    if city:
        return f"{label} — {city}"
    return label


def format_confirmation_label(offset: str) -> str:
    city = RU_TZ_CITY.get(offset)
    if city:
        return f"UTC{offset} ({city})"
    return f"UTC{offset}"


def catalog(mode: str) -> list[str]:
    if mode == "ru":
        return [offset for offset, _ in RU_TZ_OFFSETS]
    return [f"{offset:+03d}:00" for offset in range(-12, 15)]
