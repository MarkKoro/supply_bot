from __future__ import annotations
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить добавку", callback_data="menu:add")],
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
    for r in rows:
        btns.append([InlineKeyboardButton(f"❌ {r['name']}", callback_data=f"mine:del:{r['slug']}")])
    btns.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")])
    return InlineKeyboardMarkup(btns)
