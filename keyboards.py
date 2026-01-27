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
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="my:list:0")]])

def my_hub_back(supplement_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"my:open:{supplement_id}")]])

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
        btns.append([InlineKeyboardButton(label, callback_data=f"my:open:{r['supplement_id']}")])
    search_row = [InlineKeyboardButton("🔎 Поиск", callback_data="my:search")]
    if show_clear:
        search_row.append(InlineKeyboardButton("❌ Сбросить поиск", callback_data="my:clear"))
    btns.append(search_row)
    nav = []
    if has_prev:
        nav.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"my:list:{page - 1}"))
    nav.append(InlineKeyboardButton("🏠 Меню", callback_data="menu:home"))
    if has_next:
        nav.append(InlineKeyboardButton("➡️ Далее", callback_data=f"my:list:{page + 1}"))
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
        btns.append([InlineKeyboardButton("✅ Уже в моих — Управлять", callback_data=f"my:open:{supplement_id}")])
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
) -> InlineKeyboardMarkup:
    btns: list[list[InlineKeyboardButton]] = []
    for item in items:
        btns.append([InlineKeyboardButton(item["title"], callback_data=f"supp:open:{item['other_id']}")])
    nav: list[InlineKeyboardButton] = []
    if has_prev:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"supp:compat:{supplement_id}:{kind}:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"supp:compat:{supplement_id}:{kind}:{page + 1}"))
    if nav:
        btns.append(nav)
    btns.append([InlineKeyboardButton("⬅️ К карточке", callback_data=f"supp:open:{supplement_id}")])
    return InlineKeyboardMarkup(btns)

def my_supplement_hub(
    supplement_id: int,
    is_active: bool,
    has_time: bool,
    today_status: str | None,
    *,
    show_debug: bool = False,
) -> InlineKeyboardMarkup:
    toggle_label = "⏸ Пауза" if is_active else "▶️ Активировать"
    schedule_row = [InlineKeyboardButton("⏰ Время", callback_data=f"my:settime:{supplement_id}")]
    if has_time:
        schedule_row.append(InlineKeyboardButton("🗑 Убрать время", callback_data=f"my:cleartime:{supplement_id}"))

    today_row = [
        InlineKeyboardButton("✅ Выпил", callback_data=f"my:marktaken:{supplement_id}"),
        InlineKeyboardButton("❌ Не выпил", callback_data=f"my:markskipped:{supplement_id}"),
    ]
    reset_row = [InlineKeyboardButton("↩️ Сбросить", callback_data=f"my:markreset:{supplement_id}")]
    btns = [
        schedule_row,
        today_row,
        reset_row,
        [InlineKeyboardButton("✏️ Доза", callback_data=f"my:setdose:{supplement_id}")],
        [InlineKeyboardButton("📝 Заметка", callback_data=f"my:setnote:{supplement_id}")],
        [InlineKeyboardButton(toggle_label, callback_data=f"my:toggle:{supplement_id}")],
        [InlineKeyboardButton("🗑 Удалить из моих", callback_data=f"my:remove:{supplement_id}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="my:list:0")],
    ]
    if show_debug:
        btns.insert(-1, [InlineKeyboardButton("🧪 Тест напоминания (10 сек)", callback_data=f"my:debugrem:10:{supplement_id}")])
        btns.insert(-1, [InlineKeyboardButton("🧪 Тест (через 3 сек)", callback_data=f"my:debugrem:3:{supplement_id}")])
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
        [InlineKeyboardButton("✅ Выпил", callback_data=f"rem:taken:{supplement_id}")],
        [InlineKeyboardButton("⏰ Через 15 минут", callback_data=f"rem:snooze:{supplement_id}")],
        [InlineKeyboardButton("❌ Сегодня не пью", callback_data=f"rem:skip:{supplement_id}")],
    ])

def settings_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🕒 Часовой пояс", callback_data="settings:tz")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")],
    ])
