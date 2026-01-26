from __future__ import annotations
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить добавку", callback_data="menu:add")],
        [InlineKeyboardButton("📚 Каталог", callback_data="menu:catalog")],
        [InlineKeyboardButton("📦 Мои БАДы", callback_data="menu:my")],
        [InlineKeyboardButton("🧩 Совместимость", callback_data="menu:compat")],
        [InlineKeyboardButton("📝 Чекин (демо)", callback_data="menu:checkin_demo")],
        [InlineKeyboardButton("🤖 AI-совет (лимит/день)", callback_data="menu:ai")],
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
        btns.append([InlineKeyboardButton(r["name"], callback_data=f"my:open:{r['supplement_id']}")])
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
        btns.append([InlineKeyboardButton(r["name"], callback_data=f"supp:view:{r['slug']}:{page}")])
    nav = []
    if has_prev:
        nav.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"cat:page:{page - 1}"))
    nav.append(InlineKeyboardButton("🏠 Меню", callback_data="menu:home"))
    if has_next:
        nav.append(InlineKeyboardButton("➡️ Далее", callback_data=f"cat:page:{page + 1}"))
    if nav:
        btns.append(nav)
    return InlineKeyboardMarkup(btns)

def supplement_page(
    supplement_id: int,
    slug: str,
    back_page: int,
    in_stack: bool,
    show_buy: str | None,
) -> InlineKeyboardMarkup:
    btns = []
    if in_stack:
        btns.append([InlineKeyboardButton("✅ Уже в моих — Управлять", callback_data=f"my:open:{supplement_id}")])
    else:
        btns.append([InlineKeyboardButton("➕ Добавить в мои БАДы", callback_data=f"my:add:{supplement_id}")])
    btns.append([InlineKeyboardButton("🧩 Совместимость", callback_data=f"supp:compat:{slug}:{back_page}")])
    if show_buy:
        btns.append([InlineKeyboardButton("🛒 Купить", url=show_buy)])
    btns.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"cat:page:{back_page}")])
    return InlineKeyboardMarkup(btns)

def supplement_compat_back(slug: str, back_page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"supp:view:{slug}:{back_page}")],
    ])

def my_supplement_hub(supplement_id: int, is_active: bool) -> InlineKeyboardMarkup:
    toggle_label = "⏸ Пауза" if is_active else "▶️ Активировать"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Доза", callback_data=f"my:setdose:{supplement_id}")],
        [InlineKeyboardButton("📝 Заметка", callback_data=f"my:setnote:{supplement_id}")],
        [InlineKeyboardButton(toggle_label, callback_data=f"my:toggle:{supplement_id}")],
        [InlineKeyboardButton("🗑 Удалить из моих", callback_data=f"my:remove:{supplement_id}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="my:list:0")],
    ])
