from __future__ import annotations
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import tz

def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить добавку", callback_data="menu:add")],
        [InlineKeyboardButton("📚 Каталог", callback_data="menu:catalog")],
        [InlineKeyboardButton("🧠 Готовые стеки", callback_data="menu:curated")],
        [InlineKeyboardButton("📦 Мои БАДы", callback_data="menu:my")],
        [InlineKeyboardButton("🧩 Совместимость", callback_data="menu:compat")],
        [InlineKeyboardButton("📝 Опрос дня", callback_data="menu:checkin")],
        [InlineKeyboardButton("🤖 AI-подбор", callback_data="menu:ai")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="menu:settings")],
    ])

def back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")]])

def my_list_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="mine:list:0")]])

def my_hub_back(supplement_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"mine:hub:{supplement_id}")]])

def pick_search_results(rows, prefix: str) -> InlineKeyboardMarkup:
    btns = []
    for r in rows:
        btns.append([InlineKeyboardButton(r["name"], callback_data=f"{prefix}:{r['slug']}")])
    btns.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")])
    return InlineKeyboardMarkup(btns)

def my_supplements_list(rows, page: int, has_prev: bool, has_next: bool, *, show_clear: bool) -> InlineKeyboardMarkup:
    btns = []
    for r in rows:
        status = r.get("today_status")
        if status == "taken":
            marker = "✅"
        elif status == "skipped":
            marker = "❌"
        else:
            marker = "…"
        label = f"{r['name']} {marker}"
        btns.append([InlineKeyboardButton(label, callback_data=f"mine:open:{r['supplement_id']}")])
    search_row = [InlineKeyboardButton("🔎 Поиск", callback_data="mine:search")]
    if show_clear:
        search_row.append(InlineKeyboardButton("❌ Сбросить поиск", callback_data="mine:clear"))
    btns.append(search_row)
    nav = []
    if has_prev:
        nav.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"mine:list:{page - 1}"))
    nav.append(InlineKeyboardButton("⬅️ Назад", callback_data="menu:home"))
    if has_next:
        nav.append(InlineKeyboardButton("➡️ Далее", callback_data=f"mine:list:{page + 1}"))
    btns.append(nav)
    return InlineKeyboardMarkup(btns)

def catalog_page(rows, page: int, has_prev: bool, has_next: bool) -> InlineKeyboardMarkup:
    btns = []
    for r in rows:
        btns.append([InlineKeyboardButton(r["name"], callback_data=f"supp:open:{r['id']}")])
    nav = []
    if has_prev:
        nav.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"cat:page:{page - 1}"))
    nav.append(InlineKeyboardButton("🏠 Меню", callback_data="menu:home"))
    if has_next:
        nav.append(InlineKeyboardButton("➡️ Далее", callback_data=f"cat:page:{page + 1}"))
    if nav:
        btns.append(nav)
    return InlineKeyboardMarkup(btns)

def curated_stacks_list(rows, page: int, has_prev: bool, has_next: bool) -> InlineKeyboardMarkup:
    btns = []
    for r in rows:
        label = f"🧠 {r['title_ru']}"
        btns.append([InlineKeyboardButton(label, callback_data=f"curated:open:{r['slug']}")])
    nav = []
    if has_prev:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"curated:page:{page - 1}"))
    nav.append(InlineKeyboardButton("⬅️ Назад", callback_data="menu:home"))
    if has_next:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"curated:page:{page + 1}"))
    btns.append(nav)
    return InlineKeyboardMarkup(btns)

def curated_stack_card(slug: str, *, source_url: str | None, back_page: int) -> InlineKeyboardMarkup:
    btns = []
    if source_url:
        btns.append([InlineKeyboardButton("🔗 Источник", url=source_url)])
    btns.append([InlineKeyboardButton("➕ Добавить себе", callback_data=f"curated:add:{slug}")])
    btns.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"curated:page:{back_page}")])
    return InlineKeyboardMarkup(btns)

def supplement_page(
    supplement_id: int,
    in_stack: bool,
    show_buy: str | None,
    back_route: str,
) -> InlineKeyboardMarkup:
    btns = []
    if in_stack:
        btns.append([InlineKeyboardButton("✅ Уже в моих — Управлять", callback_data=f"mine:open:{supplement_id}")])
    else:
        btns.append([InlineKeyboardButton("➕ Добавить в Мои БАДы", callback_data=f"my:add:{supplement_id}")])
    btns.append([
        InlineKeyboardButton("✅ Совместимо", callback_data=f"supp:compat:{supplement_id}:synergy:0"),
        InlineKeyboardButton("⛔ Несовместимо", callback_data=f"supp:compat:{supplement_id}:antagonism:0"),
    ])
    btns.append([InlineKeyboardButton("⚠️ Осторожно", callback_data=f"supp:compat:{supplement_id}:caution:0")])
    if show_buy:
        btns.append([InlineKeyboardButton("🛒 Купить", url=show_buy)])
    btns.append([InlineKeyboardButton("⬅️ Назад", callback_data=back_route)])
    return InlineKeyboardMarkup(btns)

def supplement_compat_list(
    supplement_id: int,
    items: list[dict],
    *,
    kind: str,
    page: int,
    has_prev: bool,
    has_next: bool,
    back_data: str | None = None,
    back_label: str | None = None,
    nav_prefix: str | None = None,
) -> InlineKeyboardMarkup:
    btns: list[list[InlineKeyboardButton]] = []
    for item in items:
        btns.append([InlineKeyboardButton(item["title"], callback_data=f"supp:open:{item['other_id']}")])
    nav: list[InlineKeyboardButton] = []
    nav_prefix = nav_prefix or f"supp:compat:{supplement_id}"
    if has_prev:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"{nav_prefix}:{kind}:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"{nav_prefix}:{kind}:{page + 1}"))
    if nav:
        btns.append(nav)
    if back_data:
        btns.append([InlineKeyboardButton(back_label or "⬅️ Назад", callback_data=back_data)])
    else:
        btns.append([InlineKeyboardButton("⬅️ К карточке", callback_data=f"supp:open:{supplement_id}")])
    return InlineKeyboardMarkup(btns)

def my_supplement_hub(
    supplement_id: int,
    reminder_enabled: bool,
) -> InlineKeyboardMarkup:
    toggle_label = "⏰ Напоминание: ВКЛ" if reminder_enabled else "⏰ Напоминание: ВЫКЛ"
    btns = [
        [InlineKeyboardButton(toggle_label, callback_data=f"mine:hub:toggle:{supplement_id}")],
        [InlineKeyboardButton("🕒 Время", callback_data=f"mine:hub:set_time:{supplement_id}")],
        [InlineKeyboardButton("💊 Доза", callback_data=f"mine:hub:set_dose:{supplement_id}")],
        [InlineKeyboardButton("✅ Выпил сегодня", callback_data=f"mine:hub:taken:{supplement_id}")],
        [InlineKeyboardButton("🚫 Не пью сегодня", callback_data=f"mine:hub:skip:{supplement_id}")],
        [InlineKeyboardButton("🧩 Совместимость", callback_data=f"mine:hub:compat:{supplement_id}")],
        [InlineKeyboardButton("🗑 Удалить из моих", callback_data=f"mine:hub:remove:{supplement_id}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="mine:back")],
    ]
    return InlineKeyboardMarkup(btns)

def timezone_picker(
    mode: str,
    page: int,
    *,
    per_page: int = 8,
    back_data: str | None = None,
) -> InlineKeyboardMarkup:
    mode = "ru" if mode == "ru" else "all"
    offsets = tz.catalog(mode)
    page = max(page, 0)
    start = page * per_page
    end = start + per_page
    sliced = offsets[start:end]
    btns: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for offset in sliced:
        label = tz.format_offset_label(offset)
        row.append(InlineKeyboardButton(label, callback_data=f"tz:pick:{offset}"))
        if len(row) == 2:
            btns.append(row)
            row = []
    if row:
        btns.append(row)
    nav = []
    if start > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"tz:page:{mode}:{page - 1}"))
    if end < len(offsets):
        nav.append(InlineKeyboardButton("➡️", callback_data=f"tz:page:{mode}:{page + 1}"))
    if nav:
        btns.append(nav)
    if mode == "ru":
        btns.append([InlineKeyboardButton("🌍 Другие часовые пояса", callback_data="tz:page:all:0")])
    else:
        btns.append([InlineKeyboardButton("🇷🇺 Часовые пояса РФ", callback_data="tz:page:ru:0")])
    if back_data:
        btns.append([InlineKeyboardButton("⬅️ Назад", callback_data=back_data)])
    return InlineKeyboardMarkup(btns)

def reminder_actions(supplement_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Выпил", callback_data=f"rem:done:{supplement_id}")],
        [InlineKeyboardButton("⏱ Через 15 минут", callback_data=f"rem:snooze:15:{supplement_id}")],
        [InlineKeyboardButton("🚫 Сегодня не пью", callback_data=f"rem:skip:{supplement_id}")],
        [InlineKeyboardButton("↩️ Вернуться", callback_data="rem:back")],
    ])

def settings_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🕒 Часовой пояс", callback_data="settings:tz")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")],
    ])
