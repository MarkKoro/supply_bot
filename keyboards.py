from __future__ import annotations
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import tz

def main_menu(*, is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📅 Сегодня", callback_data="menu:today")],
        [InlineKeyboardButton("💊 Мои БАДы", callback_data="menu:my")],
        [InlineKeyboardButton("📚 Каталог БАДов", callback_data="menu:catalog")],
        [InlineKeyboardButton("📝 Ежедневный опрос", callback_data="menu:checkin_ai")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("📊 Статистика", callback_data="admin:stats")])
    rows.append([InlineKeyboardButton("⚙️ Настройки", callback_data="menu:settings")])
    return InlineKeyboardMarkup(rows)


def today_home_kb(slots: list[dict]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Отметить приём", callback_data="today:slots")],
        [InlineKeyboardButton("⏰ Напоминания", callback_data="rem:home")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")],
    ])


def today_slots_kb(slots: list[dict], date_local: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for slot in slots:
        title = str(slot.get("title") or slot.get("title_ru") or "Слот")
        time_local = str(slot.get("time") or slot.get("time_local") or "--:--")
        slot_id = str(slot.get("slot_id") or "")
        taken = int(slot.get("taken") or 0)
        skipped = int(slot.get("skipped") or 0)
        total = int(slot.get("total") or 0)
        rows.append([InlineKeyboardButton(f"{title} · {time_local}", callback_data=f"today:slot:{slot_id}")])
        rows.append([InlineKeyboardButton(f"✅ {taken}/{total} · ⛔ {skipped}", callback_data=f"today:slot:{slot_id}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="today:home")])
    return InlineKeyboardMarkup(rows)


def slot_intake_kb(slot_id: str, taken_count: int, total_count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Выпил всё", callback_data=f"intake:all:{slot_id}"),
            InlineKeyboardButton("🧾 Выбрать", callback_data=f"intake:pick:{slot_id}"),
        ],
        [InlineKeyboardButton("🙅 Пропустить", callback_data=f"intake:skip:{slot_id}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="today:slots")],
    ])


def intake_pick_kb(slot_id: str, items: list[dict], selected_set: set[str], page: int) -> InlineKeyboardMarkup:
    page_size = 10
    safe_page = max(0, page)
    start = safe_page * page_size
    end = start + page_size
    page_items = items[start:end]
    rows: list[list[InlineKeyboardButton]] = []
    for item in page_items:
        supplement_id = int(item.get("supplement_id") or 0)
        title = str(item.get("ru_name") or item.get("name_ru") or item.get("name") or item.get("slug") or f"ID {supplement_id}")
        mark = "☑️" if str(supplement_id) in selected_set else "⬜"
        rows.append([InlineKeyboardButton(f"{mark} {title}", callback_data=f"intake:toggle:{slot_id}:{supplement_id}:{safe_page}")])

    nav: list[InlineKeyboardButton] = []
    if safe_page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"intake:pick:{slot_id}:{safe_page - 1}"))
    if end < len(items):
        nav.append(InlineKeyboardButton("▶️", callback_data=f"intake:pick:{slot_id}:{safe_page + 1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton("✅ Сохранить", callback_data=f"intake:save:{slot_id}:{safe_page}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="today:slots")])
    return InlineKeyboardMarkup(rows)

def ai_checkin_entry_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🟦 Быстрый опрос (кнопки)", callback_data="aicheck:quick")],
            [InlineKeyboardButton("✍️ Свободный текст", callback_data="aicheck:text")],
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
    btns.append([InlineKeyboardButton("🧠 Готовые стеки", callback_data="menu:curated")])
    btns.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")])
    return InlineKeyboardMarkup(btns)

def catalog_home(*, is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("🔎 Поиск", callback_data="cat:search"),
            InlineKeyboardButton("🧠 Доказательность", callback_data="cat:mode:evidence"),
        ],
        [
            InlineKeyboardButton("🎯 По целям/влиянию", callback_data="cat:mode:class"),
            InlineKeyboardButton("🧪 По типу", callback_data="cat:mode:type"),
        ],
        [
            InlineKeyboardButton("🫀 Куда влияет", callback_data="cat:mode:body:benefit"),
            InlineKeyboardButton("⚠️ Нагрузка", callback_data="cat:mode:body:burden"),
        ],
        [
            InlineKeyboardButton("🧩 По группе", callback_data="cat:mode:group"),
            InlineKeyboardButton("📄 Список", callback_data="cat:mode:list"),
        ],
        [
            InlineKeyboardButton("💊 Мои БАДы", callback_data="menu:my"),
            InlineKeyboardButton("🧩 Готовые стеки", callback_data="menu:curated"),
        ],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("➕ Стеки (админ)", callback_data="admin:stacks")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)

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
        grade = (r.get("evidence_grade") or "?").strip() if isinstance(r, dict) else "?"
        btns.append([InlineKeyboardButton(f"• {title} — {grade}", callback_data=f"cat:open:{r['id']}")])
    nav: list[InlineKeyboardButton] = []
    if has_prev:
        nav.append(InlineKeyboardButton("◀️", callback_data="cat:page:prev"))
    if has_next:
        nav.append(InlineKeyboardButton("▶️", callback_data="cat:page:next"))
    if nav:
        btns.append(nav)
    btns.append([InlineKeyboardButton("⬅️ Назад", callback_data="cat:home")])
    return InlineKeyboardMarkup(btns)


def catalog_type_kb() -> InlineKeyboardMarkup:
    rows = [
        ("Витамины", "vitamin"),
        ("Минералы", "mineral"),
        ("Аминокислоты", "amino_acid"),
        ("Жирные кислоты", "fatty_acid"),
        ("Пробиотики", "probiotic"),
        ("Пребиотики/клетчатка", "prebiotic_fiber"),
        ("Ферменты", "enzyme"),
        ("Экстракты растений", "botanical_extract"),
        ("Грибы", "mushroom"),
        ("Ноотропы", "nootropic"),
        ("Спортивные", "ergogenic"),
        ("Другое", "other"),
    ]
    btns = [[InlineKeyboardButton(label, callback_data=f"cat:type:{token}")] for label, token in rows]
    btns.append([InlineKeyboardButton("⬅️ Назад", callback_data="cat:home")])
    return InlineKeyboardMarkup(btns)


def catalog_body_kb(mode: str) -> InlineKeyboardMarkup:
    rows = [
        ("Мозг/ЦНС", "brain_cns"),
        ("Сердце", "heart_cv"),
        ("Сосуды/кровь", "vessels_blood"),
        ("Печень", "liver"),
        ("Почки", "kidney"),
        ("ЖКТ", "gut"),
        ("Глюкоза/инсулин", "pancreas_glucose"),
        ("Щитовидка", "thyroid"),
        ("Иммунитет", "immune"),
        ("Суставы", "joints"),
        ("Кости", "bone"),
        ("Мышцы", "muscle"),
        ("Кожа/волосы", "skin_hair"),
        ("Гормоны/репродукция", "reproductive_hormones"),
    ]
    btns = [[InlineKeyboardButton(label, callback_data=f"cat:body:{mode}:{token}")] for label, token in rows]
    btns.append([InlineKeyboardButton("⬅️ Назад", callback_data="cat:home")])
    return InlineKeyboardMarkup(btns)


def catalog_group_kb() -> InlineKeyboardMarkup:
    rows = [
        ("Адаптогены", "adaptogen"),
        ("Стимуляторы", "stimulant"),
        ("Успокаивающее", "calming"),
        ("Сон", "sleep_aid"),
        ("Фокус/ноотропы", "nootropic_focus"),
        ("Стресс/настроение", "mood_stress"),
        ("Противовоспалительное", "anti_inflammatory"),
        ("Антиоксиданты", "antioxidant"),
        ("Сердце", "cardio_support"),
        ("Глюкоза/метаболизм", "metabolic_glucose"),
        ("ЖКТ", "gut_support"),
        ("Иммунитет", "immune_support"),
        ("Суставы", "joint_support"),
        ("Кости", "bone_support"),
        ("Выносливость", "performance_endurance"),
        ("Восстановление", "recovery_muscle"),
    ]
    btns = [[InlineKeyboardButton(label, callback_data=f"cat:group:{token}")] for label, token in rows]
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
        btns.append(
            [
                InlineKeyboardButton(label, callback_data=f"mysupp:open:{r['supplement_id']}"),
                InlineKeyboardButton("✅", callback_data=f"mysupp:quick:taken:{r['supplement_id']}:{page}"),
                InlineKeyboardButton("❌", callback_data=f"mysupp:quick:skip:{r['supplement_id']}:{page}"),
            ]
        )
    btns.append([InlineKeyboardButton("➕ Добавить", callback_data="mysupp:add")])
    nav = []
    if has_prev:
        nav.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"mysupp:list:{page - 1}"))
    nav.append(InlineKeyboardButton("⬅️ Назад", callback_data="menu:home"))
    if has_next:
        nav.append(InlineKeyboardButton("➡️ Далее", callback_data=f"mysupp:list:{page + 1}"))
    btns.append(nav)
    return InlineKeyboardMarkup(btns)



def my_supplements_overview_kb(base_rows, ai_rows, page: int, has_prev: bool, has_next: bool) -> InlineKeyboardMarkup:
    btns = []
    for r in base_rows:
        status = r["today_status"] if "today_status" in r.keys() else None
        if status == "taken":
            marker = "✅"
        elif status == "skipped":
            marker = "❌"
        else:
            marker = "…"
        label = f"{r['name']} {marker}"
        btns.append(
            [
                InlineKeyboardButton(label, callback_data=f"mysupp:open:{r['supplement_id']}"),
                InlineKeyboardButton("✅", callback_data=f"mysupp:quick:taken:{r['supplement_id']}:{page}"),
                InlineKeyboardButton("❌", callback_data=f"mysupp:quick:skip:{r['supplement_id']}:{page}"),
            ]
        )

    if ai_rows:
        btns.append([InlineKeyboardButton("🤖 AI-добавления на сегодня", callback_data="noop")])
        for item in ai_rows:
            status = item.get("today_status")
            if status == "taken":
                marker = "✅"
            elif status == "skipped":
                marker = "❌"
            else:
                marker = "…"
            name = (item.get("name_ru") or item.get("name_en") or item.get("name") or "Добавка").strip()
            sid = int(item.get("supplement_id") or 0)
            btns.append(
                [
                    InlineKeyboardButton(f"🤖 {name} {marker}", callback_data=f"supp:open:{sid}"),
                    InlineKeyboardButton("✅", callback_data=f"mysupp:aiquick:taken:{sid}:{page}"),
                    InlineKeyboardButton("❌", callback_data=f"mysupp:aiquick:skip:{sid}:{page}"),
                ]
            )

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

def curated_stacks_list(rows, page: int, has_prev: bool, has_next: bool, *, is_admin: bool = False) -> InlineKeyboardMarkup:
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
    if is_admin:
        btns.append([InlineKeyboardButton("➕ Стеки (админ)", callback_data="admin:stacks")])
    return InlineKeyboardMarkup(btns)


def admin_stats_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")]])


def admin_stacks_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="cat:home")]])


def admin_stacks_list_kb(stacks, page: int, has_prev: bool, has_next: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [[InlineKeyboardButton("➕ Новый стек", callback_data="admin:stack:new")]]
    for stack in stacks:
        title = str(stack.get("title_ru") or "Без названия")
        dot = "🟢" if int(stack.get("is_active") or 0) == 1 else "⚪"
        rows.append([InlineKeyboardButton(f"{dot} {title}", callback_data=f"admin:stack:open:{int(stack['id'])}")])
    nav: list[InlineKeyboardButton] = []
    if has_prev:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"admin:stacks:page:{max(0, page - 1)}"))
    if has_next:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"admin:stacks:page:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="cat:home")])
    return InlineKeyboardMarkup(rows)


def admin_stack_editor_kb(stack_id: int, is_active: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🟢 Опубликован" if int(is_active) else "⚪ Черновик", callback_data=f"admin:stack:toggle:{stack_id}")],
            [
                InlineKeyboardButton("✏️ Название", callback_data=f"admin:stack:edit:{stack_id}:title_ru"),
                InlineKeyboardButton("👤 Автор", callback_data=f"admin:stack:edit:{stack_id}:author_ru"),
            ],
            [InlineKeyboardButton("📝 Описание", callback_data=f"admin:stack:edit:{stack_id}:description_ru")],
            [
                InlineKeyboardButton("🔗 Источник", callback_data=f"admin:stack:edit:{stack_id}:source_url"),
                InlineKeyboardButton("🏷 Теги", callback_data=f"admin:stack:edit:{stack_id}:tags"),
            ],
            [InlineKeyboardButton("📦 Состав", callback_data=f"admin:stack:items:{stack_id}")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="admin:stacks")],
        ]
    )


def admin_stack_items_kb(stack_id: int, items) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [[InlineKeyboardButton("➕ Добавить", callback_data=f"admin:stack:item:add:{stack_id}")]]
    for idx, item in enumerate(items[:10], start=1):
        label = str(item.get("supplement_name") or item.get("supplement_slug") or "Без названия")
        rows.append([InlineKeyboardButton(f"{idx}. {label}", callback_data=f"admin:stack:item:open:{stack_id}:{int(item['id'])}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"admin:stack:open:{stack_id}")])
    return InlineKeyboardMarkup(rows)


def admin_stack_item_editor_kb(stack_id: int, item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✍️ Правка дозы/времени", callback_data=f"admin:stack:item:edit:{stack_id}:{item_id}")],
            [
                InlineKeyboardButton("⬆️", callback_data=f"admin:stack:item:move:{stack_id}:{item_id}:up"),
                InlineKeyboardButton("⬇️", callback_data=f"admin:stack:item:move:{stack_id}:{item_id}:down"),
            ],
            [InlineKeyboardButton("🗑 Удалить", callback_data=f"admin:stack:item:del:{stack_id}:{item_id}")],
            [InlineKeyboardButton("⬅️ Назад", callback_data=f"admin:stack:items:{stack_id}")],
        ]
    )


def admin_stack_search_kb(stack_id: int, rows_data, page: int, has_prev: bool, has_next: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for row in rows_data:
        title = row.get("name_ru") or row.get("name_en") or row.get("slug") or "Без названия"
        rows.append([InlineKeyboardButton(str(title), callback_data=f"admin:stack:item:pick:{stack_id}:{row['slug']}")])
    nav: list[InlineKeyboardButton] = []
    if has_prev:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"admin:stack:item:search_page:{stack_id}:{max(0, page - 1)}"))
    if has_next:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"admin:stack:item:search_page:{stack_id}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"admin:stack:items:{stack_id}")])
    return InlineKeyboardMarkup(rows)

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
        btns.append([InlineKeyboardButton("🟢➕ Добавить в Мои БАДы", callback_data=f"mysupp:add_direct:{supplement_id}")])
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
            InlineKeyboardButton("🕒 Время приёма", callback_data=f"supp:tab:{supplement_id}:time"),
            InlineKeyboardButton("💊 Дозировка", callback_data=f"supp:tab:{supplement_id}:dose"),
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
    elif return_mode == "ai_back":
        add_cb = f"mysupp:add:{supplement_id}:retai"
        back_cb = "menu:ai"
    else:
        add_cb = f"mysupp:add:{supplement_id}:retcat:{return_page}"
        back_cb = f"cat:page:{return_page}"

    btns.append(
        [
            InlineKeyboardButton("🟢➕ В мои", callback_data=add_cb),
            InlineKeyboardButton("⬅️ Назад", callback_data=back_cb),
            InlineKeyboardButton("🏠 Меню", callback_data="menu:home"),
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
        [InlineKeyboardButton("⏰ Время приёма", callback_data=f"supplement:set_time:{supplement_id}")],
        [InlineKeyboardButton("🗑 Удалить из моих", callback_data=f"mysupp:remove:{supplement_id}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="mysupp:list:0")],
    ]
    return InlineKeyboardMarkup(btns)

def my_supplement_reminders(
    supplement_id: int,
    reminders: list[str],
    suggested_times: list[str],
    page: int,
    *,
    per_page: int = 8,
) -> InlineKeyboardMarkup:
    btns: list[list[InlineKeyboardButton]] = []
    page = max(page, 0)
    btns.append([InlineKeyboardButton("➕ Добавить своё время", callback_data=f"mysupp:rem:add:{supplement_id}")])
    for time_local in reminders:
        key = time_local.replace(":", "-")
        btns.append([InlineKeyboardButton(f"🗑 {time_local}", callback_data=f"mysupp:rem:del:{supplement_id}:{key}")])
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
        [
            InlineKeyboardButton("✅ Выпил", callback_data=f"rem:done:{supplement_id}"),
            InlineKeyboardButton("❌ Не выпил", callback_data=f"rem:skip:{supplement_id}"),
        ],
        [
            InlineKeyboardButton("⏱ 15 мин", callback_data=f"rem:snooze:{supplement_id}:15"),
            InlineKeyboardButton("⏱ 30 мин", callback_data=f"rem:snooze:{supplement_id}:30"),
            InlineKeyboardButton("⏱ 60 мин", callback_data=f"rem:snooze:{supplement_id}:60"),
        ],
    ])

def settings_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🕒 Часовой пояс", callback_data="settings:tz")],
        [InlineKeyboardButton("✨ Начальный подбор", callback_data="settings:initial_pick")],
        [InlineKeyboardButton("⏰ Расписание", callback_data="settings:schedule")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")],
    ])


def schedule_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Изменить время слота", callback_data="schedule:slot_pick")],
        [InlineKeyboardButton("➕ Добавить время", callback_data="schedule:add_custom")],
        [InlineKeyboardButton("Назначить время добавкам", callback_data="menu:my")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu:settings")],
    ])


def schedule_slots_pick_kb(slots: list[dict], prefix: str = "schedule:slot_time_set") -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"[{slot.get('time_local','--:--')}] {slot.get('title_ru','Слот')}", callback_data=f"{prefix}:{slot.get('slot_id','')}")] for slot in slots]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="settings:schedule")])
    return InlineKeyboardMarkup(rows)


def supplement_time_kb(supplement_id: int, slots: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for slot in slots:
        rows.append([InlineKeyboardButton(f"Слот: {slot.get('title_ru','Слот')} ({slot.get('time_local','--:--')})", callback_data=f"supplement:set_slot:{supplement_id}:{slot.get('slot_id','')}")])
    rows.append([InlineKeyboardButton(f"Поставить своё время", callback_data=f"supplement:set_override:{supplement_id}")])
    rows.append([InlineKeyboardButton(f"Следовать слоту", callback_data=f"supplement:clear_override:{supplement_id}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"mysupp:open:{supplement_id}")])
    return InlineKeyboardMarkup(rows)
