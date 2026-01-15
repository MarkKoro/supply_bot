from __future__ import annotations
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить добавку", callback_data="menu:add")],
        [InlineKeyboardButton("📚 Каталог", callback_data="menu:catalog")],
        [InlineKeyboardButton("📦 Мои добавки", callback_data="menu:mine")],
        [InlineKeyboardButton("🧩 Совместимость", callback_data="menu:compat")],
        [InlineKeyboardButton("📝 Чекин (демо)", callback_data="menu:checkin_demo")],
        [InlineKeyboardButton("🤖 AI-совет (лимит/день)", callback_data="menu:ai")],
    ])

def back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")]])

def pick_search_results(rows, prefix: str) -> InlineKeyboardMarkup:
    btns = []
    for r in rows:
        btns.append([InlineKeyboardButton(r["name"], callback_data=f"{prefix}:{r['slug']}")])
    btns.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")])
    return InlineKeyboardMarkup(btns)

def my_supplements(rows) -> InlineKeyboardMarkup:
    btns = []
    btns.append([InlineKeyboardButton("🧩 Проверить стек", callback_data="mine:check")])
    for r in rows:
        btns.append([InlineKeyboardButton(f"❌ {r['name']}", callback_data=f"mine:del:{r['slug']}")])
    btns.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")])
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
    slug: str,
    back_page: int,
    in_stack: bool,
    show_buy: str | None,
) -> InlineKeyboardMarkup:
    btns = []
    if in_stack:
        btns.append([InlineKeyboardButton("❌ Убрать из стека", callback_data=f"supp:del:{slug}:{back_page}")])
    else:
        btns.append([InlineKeyboardButton("➕ В мой стек", callback_data=f"supp:add:{slug}:{back_page}")])
    btns.append([InlineKeyboardButton("🧩 Совместимость", callback_data=f"supp:compat:{slug}:{back_page}")])
    if show_buy:
        btns.append([InlineKeyboardButton("🛒 Купить", url=show_buy)])
    btns.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"cat:page:{back_page}")])
    return InlineKeyboardMarkup(btns)

def supplement_compat_back(slug: str, back_page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"supp:view:{slug}:{back_page}")],
    ])
