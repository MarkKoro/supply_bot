from __future__ import annotations
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import tz

def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить добавку", callback_data="menu:add")],
        [InlineKeyboardButton("📚 Каталог БАДов", callback_data="menu:catalog")],
        [InlineKeyboardButton("🧠 Готовые стеки", callback_data="menu:curated")],
        [InlineKeyboardButton("💊 Мои БАДы", callback_data="menu:my")],
        [InlineKeyboardButton("🧩 Совместимость", callback_data="menu:compat")],
        [InlineKeyboardButton("📝 Опрос дня (AI)", callback_data="menu:checkin_ai")],
        [InlineKeyboardButton("🤖 AI-подбор", callback_data="menu:ai")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="menu:settings")],
    ])

def ai_checkin_entry_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🟦 Быстрый опрос (кнопки)", callback_data="aicheck:quick")],
            [InlineKeyboardButton("🎙 Опрос голосом", callback_data="aicheck:voice")],
            [InlineKeyboardButton("🏠 Меню", callback_data="menu:home")],
        ]
    )

def ai_checkin_voice_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⬅️ Назад", callback_data="aicheck:entry")],
            [InlineKeyboardButton("❌ Отмена", callback_data="menu:home")],
        ]
    )

def catalog_kb(items: list[dict], page: int, pages: int) -> InlineKeyboardMarkup:
    btns: list[list[InlineKeyboardButton]] = []
    for item in items:
        title = item.get("name_ru") or item.get("name_en") or "Без названия"
        btns.append([InlineKeyboardButton(f"• {title}", callback_data=f"cat:open:{item['id']}:{page}")])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"cat:page:{page - 1}"))
    nav.append(InlineKeyboardButton(f"Стр. {page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"cat:page:{page + 1}"))
    if nav:
        btns.append(nav)
    btns.append([InlineKeyboardButton("🔎 Поиск", callback_data="cat:search")])
    btns.append([InlineKeyboardButton("⬅️ Назад", callback_data="main")])
    return InlineKeyboardMarkup(btns)

def catalog_home() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Поиск", callback_data="cat:search")],
        [InlineKeyboardButton("🧠 По доказательности", callback_data="cat:mode:evidence")],
        [InlineKeyboardButton("🎯 По цели/классу", callback_data="cat:mode:class")],
        [InlineKeyboardButton("💊 Мои БАДы", callback_data="menu:my")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")],
    ])

def catalog_search_results(
    rows: list[dict],
    *,
    page: int,
    has_prev: bool,
    has_next: bool,
    return_page: int,
) -> InlineKeyboardMarkup:
    btns: list[list[InlineKeyboardButton]] = []
    for item in rows:
        title = item.get("name_ru") or item.get("name_en") or item.get("name") or "Без названия"
        btns.append([InlineKeyboardButton(f"• {title}", callback_data=f"cat:sopen:{item['id']}:{page}")])
    nav: list[InlineKeyboardButton] = []
    if has_prev:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"cat:sres:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"cat:sres:{page + 1}"))
    if nav:
        btns.append(nav)
    btns.append(
        [
            InlineKeyboardButton("🔎 Новый поиск", callback_data="cat:search"),
            InlineKeyboardButton("⬅️ В каталог", callback_data=f"cat:page:{return_page}"),
        ]
    )
    return InlineKeyboardMarkup(btns)

def catalog_list(rows, has_prev: bool, has_next: bool) -> InlineKeyboardMarkup:
    btns = []
    for r in rows:
        title = r["name_ru"] or r["name_en"] or r["name"]
        btns.append([InlineKeyboardButton(f"💊 {title}", callback_data=f"cat:open:{r['id']}")])
    nav: list[InlineKeyboardButton] = []
    if has_prev:
        nav.append(InlineKeyboardButton("◀️", callback_data="cat:page:prev"))
    if has_next:
        nav.append(InlineKeyboardButton("▶️", callback_data="cat:page:next"))
    if nav:
        btns.append(nav)
    btns.append([InlineKeyboardButton("⬅️ Назад", callback_data="cat:home")])
    return InlineKeyboardMarkup(btns)

def catalog_class_picker(classes: list[dict], *, page: int, has_prev: bool, has_next: bool) -> InlineKeyboardMarkup:
    btns: list[list[InlineKeyboardButton]] = []
    for item in classes:
        btns.append([InlineKeyboardButton(item["title_ru"], callback_data=f"cat:class:pick:{item['key']}")])
    nav: list[InlineKeyboardButton] = []
    if has_prev:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"cat:class:page:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"cat:class:page:{page + 1}"))
    if nav:
        btns.append(nav)
    btns.append([InlineKeyboardButton("⬅️ Назад", callback_data="cat:home")])
    return InlineKeyboardMarkup(btns)

def back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")]])

def my_list_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="mysupp:list:0")]])

def my_hub_back(supplement_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"mysupp:open:{supplement_id}")]])

def pick_search_results(rows, prefix: str) -> InlineKeyboardMarkup:
    btns = []
    for r in rows:
        btns.append([InlineKeyboardButton(r["name"], callback_data=f"{prefix}:{r['slug']}")])
    btns.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")])
    return InlineKeyboardMarkup(btns)

def my_supplements_list(rows, page: int, has_prev: bool, has_next: bool, *, show_clear: bool) -> InlineKeyboardMarkup:
    btns = []
    for r in rows:
        status = r["today_status"] if "today_status" in r.keys() else None
        if status == "taken":
            marker = "✅"
        elif status == "skipped":
            marker = "❌"
        else:
            marker = "…"
        label = f"{r['name']} {marker}"
        btns.append([InlineKeyboardButton(label, callback_data=f"mysupp:open:{r['supplement_id']}")])
    btns.append([InlineKeyboardButton("➕ Добавить", callback_data="mysupp:add")])
    nav = []
    if has_prev:
        nav.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"mysupp:list:{page - 1}"))
    nav.append(InlineKeyboardButton("⬅️ Назад", callback_data="menu:home"))
    if has_next:
        nav.append(InlineKeyboardButton("➡️ Далее", callback_data=f"mysupp:list:{page + 1}"))
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
        btns.append([InlineKeyboardButton("✅ Уже в моих — Управлять", callback_data=f"mysupp:open:{supplement_id}")])
    else:
        btns.append([InlineKeyboardButton("➕ Добавить в Мои БАДы", callback_data=f"mysupp:add_direct:{supplement_id}")])
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

def supp_tabs_kb(
    supplement_id: int,
    tab: str,
    return_mode: str,
    return_page: int,
    *,
    page: int = 0,
    pages: int = 1,
    has_links: bool = False,
    linked_items: list[dict] | None = None,
) -> InlineKeyboardMarkup:
    btns: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("📌 Коротко", callback_data=f"supp:tab:{supplement_id}:brief"),
            InlineKeyboardButton("🕒 Когда", callback_data=f"supp:tab:{supplement_id}:time"),
            InlineKeyboardButton("💊 Дозы", callback_data=f"supp:tab:{supplement_id}:dose"),
        ],
        [
            InlineKeyboardButton("🤝 Совместимо", callback_data=f"supp:tab:{supplement_id}:synergy"),
            InlineKeyboardButton("⚠️ Несовместимо", callback_data=f"supp:tab:{supplement_id}:antag"),
        ],
    ]

    if tab in {"synergy", "antag"}:
        for item in linked_items or []:
            title = item.get("name_ru") or item.get("name_en") or "Без названия"
            btns.append([InlineKeyboardButton(f"• {title}", callback_data=f"supp:open:{item['id']}")])
        if has_links or pages > 1:
            nav: list[InlineKeyboardButton] = []
            if page > 0:
                nav.append(InlineKeyboardButton("⬅️", callback_data=f"supp:page:{supplement_id}:{tab}:{page - 1}"))
            nav.append(InlineKeyboardButton(f"Стр {page + 1}/{max(pages, 1)}", callback_data="noop"))
            if page < pages - 1:
                nav.append(InlineKeyboardButton("➡️", callback_data=f"supp:page:{supplement_id}:{tab}:{page + 1}"))
            btns.append(nav)

    if return_mode == "search":
        add_cb = f"mysupp:add:{supplement_id}:retsearch:{return_page}"
        back_cb = f"cat:sres:{return_page}"
    elif return_mode == "supp_back":
        add_cb = f"mysupp:add:{supplement_id}:retback"
        back_cb = "supp:back"
    else:
        add_cb = f"mysupp:add:{supplement_id}:retcat:{return_page}"
        back_cb = f"cat:page:{return_page}"

    btns.append(
        [
            InlineKeyboardButton("➕ В мои", callback_data=add_cb),
            InlineKeyboardButton("⬅️ Назад", callback_data=back_cb),
            InlineKeyboardButton("🏠 Меню", callback_data="main"),
        ]
    )
    return InlineKeyboardMarkup(btns)

def my_supplement_hub(
    supplement_id: int,
) -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton("✅ Отметить: выпил сегодня", callback_data=f"mysupp:mark:taken:{supplement_id}")],
        [InlineKeyboardButton("✖️ Отметить: не буду сегодня", callback_data=f"mysupp:mark:skip:{supplement_id}")],
        [InlineKeyboardButton("⏰ Напоминания", callback_data=f"mysupp:rem:{supplement_id}")],
        [InlineKeyboardButton("🧾 Доза", callback_data=f"mysupp:set_dose:{supplement_id}")],
        [InlineKeyboardButton("🗑 Удалить из моих", callback_data=f"mysupp:remove:{supplement_id}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="mysupp:list:0")],
    ]
    return InlineKeyboardMarkup(btns)

def my_supplement_reminders(
    supplement_id: int,
    reminders: list[str],
) -> InlineKeyboardMarkup:
    btns: list[list[InlineKeyboardButton]] = []
    for time_local in reminders:
        key = time_local.replace(":", "-")
        btns.append([InlineKeyboardButton(f"🗑 {time_local}", callback_data=f"mysupp:rem:del:{supplement_id}:{key}")])
    btns.append([InlineKeyboardButton("➕ Добавить время", callback_data=f"mysupp:rem:add:{supplement_id}")])
    btns.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"mysupp:open:{supplement_id}")])
    return InlineKeyboardMarkup(btns)

def timezone_picker(
    page: int,
    *,
    per_page: int = 8,
) -> InlineKeyboardMarkup:
    offsets = tz.catalog()
    page = max(page, 0)
    start = page * per_page
    end = start + per_page
    sliced = offsets[start:end]
    btns: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for offset in sliced:
        label = tz.format_offset_label(offset)
        tag = tz.format_offset_tag(offset)
        row.append(InlineKeyboardButton(label, callback_data=f"tz:set:{tag}"))
        if len(row) == 2:
            btns.append(row)
            row = []
    if row:
        btns.append(row)
    nav = []
    if start > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"tz:page:{page - 1}"))
    if end < len(offsets):
        nav.append(InlineKeyboardButton("➡️", callback_data=f"tz:page:{page + 1}"))
    if nav:
        btns.append(nav)
    btns.append([InlineKeyboardButton("⬅️ Назад", callback_data="tz:back")])
    return InlineKeyboardMarkup(btns)

def reminder_actions(supplement_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Выпил", callback_data=f"rem:done:{supplement_id}")],
        [InlineKeyboardButton("⏱ Через 15 минут", callback_data=f"rem:snooze:{supplement_id}:15")],
        [InlineKeyboardButton("✖️ Сегодня не буду", callback_data=f"rem:skip:{supplement_id}")],
    ])

def settings_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🕒 Часовой пояс", callback_data="settings:tz")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")],
    ])
