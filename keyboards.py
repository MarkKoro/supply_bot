from __future__ import annotations
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

import tz

def main_menu(
    *,
    is_admin: bool = False,
    show_complexes: bool = False,
    show_ai_checkin: bool = True,
) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📅 Сегодня", callback_data="menu:today")],
        [InlineKeyboardButton("💊 Мои БАДы", callback_data="menu:my")],
        [InlineKeyboardButton("📚 Каталог БАДов", callback_data="menu:catalog")],
    ]
    if show_ai_checkin:
        rows.append([InlineKeyboardButton("📝 Ежедневный опрос", callback_data="menu:checkin_ai")])
    if is_admin:
        rows.append([InlineKeyboardButton("🛠 Админ-меню", callback_data="admin:home")])
    rows.append([InlineKeyboardButton("⚙️ Настройки", callback_data="menu:settings")])
    return InlineKeyboardMarkup(rows)


def today_home_kb(slots: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for slot in slots:
        slot_id = str(slot.get("slot_ref") or slot.get("slot_id") or "")
        title = str(slot.get("title") or "Слот")
        time_local = str(slot.get("time") or "--:--")
        pending = int(slot.get("pending") or 0)
        rows.append([InlineKeyboardButton(f"{title} · {time_local} · осталось: {pending}", callback_data=f"today:slot:{slot_id}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


def today_slots_kb(slots: list[dict], date_local: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for slot in slots:
        title = str(slot.get("title") or slot.get("title_ru") or "Слот")
        time_local = str(slot.get("time") or slot.get("time_local") or "--:--")
        slot_id = str(slot.get("slot_ref") or slot.get("slot_id") or "")
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
        [InlineKeyboardButton("⬅️ Назад", callback_data="today:home")],
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
        title = str(item.get("picker_title") or item.get("ru_name") or item.get("name_ru") or item.get("name") or item.get("slug") or f"ID {supplement_id}")
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
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="today:home")])
    return InlineKeyboardMarkup(rows)

def ai_checkin_entry_kb(target_day: str = "today") -> InlineKeyboardMarkup:
    is_tomorrow = target_day == "tomorrow"
    day_label = "Завтра" if is_tomorrow else "Сегодня"
    toggle_label = "Переключить на: Сегодня" if is_tomorrow else "Переключить на: Завтра"
    toggle_target = "today" if is_tomorrow else "tomorrow"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"🎯 Рекомендация на: {day_label}", callback_data="noop")],
            [InlineKeyboardButton(toggle_label, callback_data=f"aicheck:target:{toggle_target}")],
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

def consent_kb(offer_url: str, privacy_url: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if offer_url:
        rows.append([InlineKeyboardButton("📄 Оферта", url=offer_url)])
    if privacy_url:
        rows.append([InlineKeyboardButton("🔒 Политика конфиденциальности", url=privacy_url)])
    rows.append([InlineKeyboardButton("✅ Я согласен(на)", callback_data="consent:accept")])
    rows.append([InlineKeyboardButton("❌ Не согласен(на)", callback_data="consent:decline")])
    return InlineKeyboardMarkup(rows)

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
        [InlineKeyboardButton("🔎 Поиск", callback_data="cat:search")],
        [InlineKeyboardButton("🎯 По цели", callback_data="cat:goal")],
        [InlineKeyboardButton("🫀 По органу / системе", callback_data="cat:organ")],
        [InlineKeyboardButton("📃 Весь каталог A–Я", callback_data="cat:mode:list")],
        [InlineKeyboardButton("🧩 Готовые стеки", callback_data="menu:curated")],
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

def catalog_list(
    rows,
    has_prev: bool,
    has_next: bool,
    *,
    back_cb: str = "cat:home",
    mode_switch_body_token: str = "",
    mode_switch_current: str = "",
) -> InlineKeyboardMarkup:
    btns = []
    for r in rows:
        title = r["name_ru"] or r["name_en"] or r["name"]
        last_taken_day = str(r.get("last_taken_day") or "").strip()
        if last_taken_day:
            try:
                last_taken_day = datetime.strptime(last_taken_day, "%Y-%m-%d").strftime("%d.%m.%Y")
            except Exception:
                pass
            title = f"{title} ({last_taken_day})"
        btns.append([InlineKeyboardButton(f"• {title}", callback_data=f"cat:open:{r['id']}")])
    if mode_switch_body_token:
        support_mark = "✅ " if mode_switch_current == "body_benefit" else ""
        burden_mark = "✅ " if mode_switch_current == "body_burden" else ""
        btns.append(
            [
                InlineKeyboardButton(f"{support_mark}Поддержка", callback_data=f"cat:body:benefit:{mode_switch_body_token}"),
                InlineKeyboardButton(f"{burden_mark}Нагрузка", callback_data=f"cat:body:burden:{mode_switch_body_token}"),
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if has_prev:
        nav.append(InlineKeyboardButton("◀️", callback_data="cat:page:prev"))
    if has_next:
        nav.append(InlineKeyboardButton("▶️", callback_data="cat:page:next"))
    if nav:
        btns.append(nav)
    btns.append([InlineKeyboardButton("⬅️ Назад", callback_data=back_cb)])
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


def catalog_body_kb(mode: str = "benefit") -> InlineKeyboardMarkup:
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
    btns = [[InlineKeyboardButton(label, callback_data=f"cat:organ:pick:{token}")] for label, token in rows]
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
    def _row_val(row, key, default=None):
        if isinstance(row, dict):
            return row.get(key, default)
        try:
            return row[key]
        except Exception:
            return default
    for r in rows:
        label = str(_row_val(r, 'name') or 'Добавка')
        sid = int(_row_val(r, 'supplement_id') or 0)
        if sid <= 0:
            continue
        btns.append([InlineKeyboardButton(label, callback_data=f"mysupp:open:{sid}")])
    btns.append([InlineKeyboardButton("➕ Добавить", callback_data="mysupp:add")])
    btns.append([InlineKeyboardButton("🧩 Комплексы", callback_data=f"mysupp:complexes:{page}")])
    nav = []
    if has_prev:
        nav.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"mysupp:list:{page - 1}"))
    nav.append(InlineKeyboardButton("⬅️ Назад", callback_data="menu:home"))
    if has_next:
        nav.append(InlineKeyboardButton("➡️ Далее", callback_data=f"mysupp:list:{page + 1}"))
    btns.append(nav)
    return InlineKeyboardMarkup(btns)



def my_supplements_overview_kb(base_rows, page: int, has_prev: bool, has_next: bool) -> InlineKeyboardMarkup:
    btns = []
    def _row_val(row, key, default=None):
        if isinstance(row, dict):
            return row.get(key, default)
        try:
            return row[key]
        except Exception:
            return default
    for r in base_rows:
        label = str(_row_val(r, 'name') or 'Добавка')
        sid = int(_row_val(r, 'supplement_id') or 0)
        if sid <= 0:
            continue
        btns.append([InlineKeyboardButton(label, callback_data=f"mysupp:open:{sid}")])

    btns.append([InlineKeyboardButton("➕ Добавить", callback_data="mysupp:add")])
    btns.append([InlineKeyboardButton("🧩 Комплексы", callback_data=f"mysupp:complexes:{page}")])
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
            [InlineKeyboardButton("🗑 Удалить стек", callback_data=f"admin:stack:delete_confirm:{stack_id}")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="admin:stacks")],
        ]
    )


def admin_stack_delete_confirm_kb(stack_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🗑 Да, удалить", callback_data=f"admin:stack:delete:{stack_id}")],
            [InlineKeyboardButton("✖️ Отмена", callback_data=f"admin:stack:open:{stack_id}")],
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



def shop_url_button(url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton("🛒 Перейти в магазин", url=url)


def admin_hub_kb(*, perms: dict[str, bool] | None = None) -> InlineKeyboardMarkup:
    perms = perms or {}
    rows: list[list[InlineKeyboardButton]] = []
    if perms.get("content_edit", False):
        rows.append([InlineKeyboardButton("🔎 Контент: найти БАД", callback_data="admin:supp_search")])
    if perms.get("content_bulk", False):
        rows.append([InlineKeyboardButton("📦 Bulk операции", callback_data="admin:bulk_stub")])
        rows.append([InlineKeyboardButton("➕ Дополнить базу", callback_data="admin:seed_patch")])
    if perms.get("users_view", False) or perms.get("users_ban", False):
        rows.append([InlineKeyboardButton("👤 Пользователи", callback_data="admin:users_stub")])
    if perms.get("payments_view", False):
        rows.append([InlineKeyboardButton("💳 Чеки", callback_data="admin:paychecks")])
    if perms.get("roles_manage", False):
        rows.append([InlineKeyboardButton("🛡 Роли", callback_data="admin:roles")])
    if perms.get("system_diag", False):
        rows.append([InlineKeyboardButton("🧰 Диагностика", callback_data="admin:diag")])
    rows.append([InlineKeyboardButton("↩️ Назад", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


def admin_supp_results_kb(rows: list[dict], *, offset: int, has_prev: bool, has_next: bool) -> InlineKeyboardMarkup:
    btns: list[list[InlineKeyboardButton]] = []
    for item in rows:
        title = (item.get("name_ru") or item.get("name_en") or item.get("slug") or "Без названия").strip()
        grade = (item.get("evidence_grade") or "?").strip() or "?"
        mark = "🔗" if item.get("has_link") else "—"
        btns.append([InlineKeyboardButton(f"{title} ({grade}) {mark}", callback_data=f"admin:supp_open:{int(item['id'])}")])
    nav: list[InlineKeyboardButton] = []
    if has_prev:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"admin:supp_list:{max(0, offset - 10)}"))
    if has_next:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"admin:supp_list:{offset + 10}"))
    if nav:
        btns.append(nav)
    btns.append([InlineKeyboardButton("🔎 Новый поиск", callback_data="admin:supp_search")])
    btns.append([InlineKeyboardButton("↩️ В админ-меню", callback_data="admin:home")])
    return InlineKeyboardMarkup(btns)


def admin_supp_card_kb(supplement_id: int, purchase_url: str) -> InlineKeyboardMarkup:
    has_link = bool((purchase_url or "").strip())
    rows: list[list[InlineKeyboardButton]] = []
    if has_link:
        rows.append([InlineKeyboardButton("✏️ Изменить ссылку", callback_data=f"admin:supp_set_link:{supplement_id}")])
        rows.append([InlineKeyboardButton("🗑 Удалить ссылку", callback_data=f"admin:supp_clear_link:{supplement_id}")])
        rows.append([InlineKeyboardButton("🛒 Открыть ссылку", url=purchase_url)])
    else:
        rows.append([InlineKeyboardButton("➕ Добавить ссылку", callback_data=f"admin:supp_set_link:{supplement_id}")])
    rows.append([InlineKeyboardButton("✏️ Редактировать поля", callback_data=f"admin:supp_edit:{supplement_id}")])
    rows.append([InlineKeyboardButton("🧾 История правок", callback_data=f"admin:supp_audit:{supplement_id}")])
    rows.append([InlineKeyboardButton("🚫 Скрыть/Показать в каталоге", callback_data=f"admin:supp_toggle:{supplement_id}")])
    rows.append([InlineKeyboardButton("↩️ Назад к списку", callback_data="admin:back")])
    return InlineKeyboardMarkup(rows)


def admin_supp_field_picker_kb(supplement_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Название RU", callback_data=f"admin:supp_edit_field:{supplement_id}:name_ru")],
        [InlineKeyboardButton("Название EN", callback_data=f"admin:supp_edit_field:{supplement_id}:name_en")],
        [InlineKeyboardButton("Summary (verified)", callback_data=f"admin:supp_edit_field:{supplement_id}:summary_ru_verified")],
        [InlineKeyboardButton("Summary (theoretical)", callback_data=f"admin:supp_edit_field:{supplement_id}:summary_ru_theoretical")],
        [InlineKeyboardButton("Dose", callback_data=f"admin:supp_edit_field:{supplement_id}:dose_ru")],
        [InlineKeyboardButton("Timing", callback_data=f"admin:supp_edit_field:{supplement_id}:timing_ru")],
        [InlineKeyboardButton("Safety", callback_data=f"admin:supp_edit_field:{supplement_id}:safety_ru")],
        [InlineKeyboardButton("Evidence grade", callback_data=f"admin:supp_edit_field:{supplement_id}:evidence_grade")],
        [InlineKeyboardButton("Confidence", callback_data=f"admin:supp_edit_field:{supplement_id}:confidence")],
        [InlineKeyboardButton("Tags", callback_data=f"admin:supp_edit_field:{supplement_id}:tags_json")],
        [InlineKeyboardButton("Targets", callback_data=f"admin:supp_edit_field:{supplement_id}:body_targets_json")],
        [InlineKeyboardButton("Burdens", callback_data=f"admin:supp_edit_field:{supplement_id}:body_burdens_json")],
        [InlineKeyboardButton("Purchase URL", callback_data=f"admin:supp_edit_field:{supplement_id}:purchase_url")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"admin:supp_open:{supplement_id}")],
    ])


def admin_supp_edit_wait_kb(supplement_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧹 Очистить поле", callback_data=f"admin:supp_edit_clear:{supplement_id}")],
        [InlineKeyboardButton("✖️ Отмена", callback_data=f"admin:supp_open:{supplement_id}")],
    ])


def admin_supp_toggle_confirm_kb(supplement_id: int, to_active: int) -> InlineKeyboardMarkup:
    action = "Показать" if int(to_active) else "Скрыть"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ {action}", callback_data=f"admin:supp_toggle_confirm:{supplement_id}:{int(to_active)}")],
        [InlineKeyboardButton("✖️ Отмена", callback_data=f"admin:supp_open:{supplement_id}")],
    ])


def admin_supp_audit_back_kb(supplement_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"admin:supp_open:{supplement_id}")]])

def curated_stack_card(
    slug: str,
    *,
    source_url: str | None,
    back_page: int,
    item_buttons: list[dict] | None = None,
) -> InlineKeyboardMarkup:
    btns = []
    for item in list(item_buttons or [])[:12]:
        label = str(item.get("label") or "Добавка")
        supp_id = int(item.get("supplement_id") or 0)
        if supp_id > 0:
            btns.append([InlineKeyboardButton(label, callback_data=f"supp:open:{supp_id}")])
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
        btns.append([shop_url_button(show_buy)])
    btns.append([InlineKeyboardButton("⬅️ Назад", callback_data=back_route)])
    btns.append([InlineKeyboardButton("🏠 Меню", callback_data="menu:home")])
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
    purchase_url: str | None = None,
    in_stack: bool = False,
) -> InlineKeyboardMarkup:
    btns: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("🧠 Как действует", callback_data=f"supp:tab:{supplement_id}:brief"),
            InlineKeyboardButton("💊 Как принимать", callback_data=f"supp:tab:{supplement_id}:dose"),
        ],
        [
            InlineKeyboardButton("🤝 Совместимость", callback_data=f"supp:tab:{supplement_id}:synergy"),
            InlineKeyboardButton("📊 Доказательность", callback_data=f"supp:tab:{supplement_id}:time"),
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
    elif return_mode == "onboard_back":
        add_cb = f"mysupp:add:{supplement_id}:ret_onboard"
        back_cb = "onboard:results"
    else:
        add_cb = f"mysupp:add:{supplement_id}:retcat:{return_page}"
        back_cb = f"cat:page:{return_page}"

    if return_mode == "onboard_back":
        if in_stack:
            btns.append([InlineKeyboardButton("✅ Уже добавлено", callback_data="noop")])
        else:
            btns.append([InlineKeyboardButton("🟢 Добавить в мои БАДы", callback_data=add_cb)])
        btns.append([InlineKeyboardButton("⬅️ Назад", callback_data=back_cb)])
    else:
        primary_label = "⚙️ Управлять" if in_stack else "🟢 Добавить в мои БАДы"
        primary_cb = f"mysupp:open:{supplement_id}" if in_stack else add_cb
        btns.append([InlineKeyboardButton(primary_label, callback_data=primary_cb)])
        btns.append([InlineKeyboardButton("⬅️ Назад", callback_data=back_cb)])
        btns.append([InlineKeyboardButton("🏠 Меню", callback_data="menu:home")])
    if purchase_url:
        btns.append([InlineKeyboardButton("🛒 Где купить", url=purchase_url)])
    return InlineKeyboardMarkup(btns)

def my_supplement_hub(
    supplement_id: int,
) -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton("🧾 Доза", callback_data=f"mysupp:set_dose:{supplement_id}")],
        [InlineKeyboardButton("⏰ Время приёма", callback_data=f"supplement:set_time:{supplement_id}")],
        [InlineKeyboardButton("📊 История приёма", callback_data=f"mysupp:hist:{supplement_id}:0")],
        [InlineKeyboardButton("➗ Разделить приём", callback_data=f"mysupp:split:{supplement_id}")],
        [InlineKeyboardButton("🗑 Удалить из моих", callback_data=f"mysupp:remove:{supplement_id}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="mysupp:list:0")],
    ]
    return InlineKeyboardMarkup(btns)


def my_supplement_history_kb(supplement_id: int, month_offset: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    rows.append(
        [
            InlineKeyboardButton("◀️", callback_data=f"mysupp:hist:{supplement_id}:{month_offset - 1}"),
            InlineKeyboardButton("▶️", callback_data=f"mysupp:hist:{supplement_id}:{month_offset + 1}"),
        ]
    )
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"mysupp:open:{supplement_id}")])
    return InlineKeyboardMarkup(rows)

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

def settings_menu(*, show_starter_activation: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🕒 Часовой пояс", callback_data="settings:tz")],
        [InlineKeyboardButton("🔔 Уведомления", callback_data="settings:notifications")],
        [InlineKeyboardButton("📈 Статистика", callback_data="menu:stats")],
        [InlineKeyboardButton("⭐ Премиум", callback_data="menu:premium")],
        [InlineKeyboardButton("🧩 Комплексы", callback_data="settings:complexes")],
        [InlineKeyboardButton("🎁 Подарочный код", callback_data="settings:gift")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")],
    ]
    if show_starter_activation:
        rows.insert(5, [InlineKeyboardButton("🎁 Активировать 3 дня", callback_data="starter:activate")])
    return InlineKeyboardMarkup(rows)


def gift_user_kb(*, is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if is_admin:
        rows.append([InlineKeyboardButton("➕ Создать новый код", callback_data="gift:admin:create")])
        rows.append([InlineKeyboardButton("📋 Коды", callback_data="gift:admin:codes")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu:settings")])
    return InlineKeyboardMarkup(rows)



def settings_notifications_kb(*, reminders_enabled: bool, slots: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    rows.append([InlineKeyboardButton(
        "🔕 Отключить все уведомления" if reminders_enabled else "🔔 Включить все уведомления",
        callback_data=f"settings:notifications:global:{0 if reminders_enabled else 1}",
    )])
    for slot in slots:
        slot_id = str(slot.get("slot_id") or "")
        title = str(slot.get("title_ru") or "Слот")
        state = "✅" if bool(slot.get("is_enabled", True)) else "⛔"
        rows.append([InlineKeyboardButton(f"{state} {title}", callback_data=f"settings:notifications:slot:{slot_id}")])
    rows.append([InlineKeyboardButton("⏰ Изменить время слотов", callback_data="settings:schedule")])
    rows.append([InlineKeyboardButton("💊 Точечные напоминания БАДов", callback_data="menu:my")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu:settings")])
    return InlineKeyboardMarkup(rows)


def complexes_hub_kb(items: list[dict], *, back_cb: str = "menu:settings") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for item in items[:20]:
        cid = int(item.get("id") or 0)
        name = str(item.get("name") or "Комплекс")
        count = int(item.get("items_count") or 0)
        rows.append([InlineKeyboardButton(f"🧩 {name} ({count})", callback_data=f"cx:open:{cid}")])
    rows.append([InlineKeyboardButton("➕ Создать комплекс", callback_data="cx:create")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=back_cb)])
    return InlineKeyboardMarkup(rows)


def complex_open_kb(complex_id: int, *, list_back_cb: str = "cx:list") -> InlineKeyboardMarkup:
    cid = int(complex_id)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить БАД", callback_data=f"cx:add:{cid}")],
        [InlineKeyboardButton("⏱ Расписание", callback_data=f"cx:schedule:{cid}")],
        [InlineKeyboardButton("✏️ Переименовать", callback_data=f"cx:rename:{cid}")],
        [InlineKeyboardButton("🗑 Удалить", callback_data=f"cx:delete:{cid}")],
        [InlineKeyboardButton("⬅️ К списку комплексов", callback_data=list_back_cb)],
    ])


def complex_add_menu_kb(complex_id: int) -> InlineKeyboardMarkup:
    cid = int(complex_id)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Выбрать из моих БАДов", callback_data=f"cx:add:mine:{cid}:0")],
        [InlineKeyboardButton("📚 Перейти в каталог", callback_data=f"cx:add:catalog:{cid}")],
        [InlineKeyboardButton("⬅️ К комплексу", callback_data=f"cx:open:{cid}")],
    ])


def complex_add_mine_kb(
    complex_id: int,
    rows: list[dict],
    page: int,
    *,
    has_prev: bool,
    has_next: bool,
) -> InlineKeyboardMarkup:
    cid = int(complex_id)
    btns: list[list[InlineKeyboardButton]] = []
    def _row_val(row, key, default=None):
        if isinstance(row, dict):
            return row.get(key, default)
        try:
            return row[key]
        except Exception:
            return default
    for item in rows:
        sid = int(_row_val(item, "supplement_id") or _row_val(item, "id") or 0)
        title = str(_row_val(item, "name") or _row_val(item, "name_ru") or _row_val(item, "name_en") or f"ID {sid}")
        btns.append([InlineKeyboardButton(f"• {title}", callback_data=f"cx:add_pick:{cid}:{sid}")])
    nav: list[InlineKeyboardButton] = []
    if has_prev:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"cx:add:mine:{cid}:{max(page - 1, 0)}"))
    if has_next:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"cx:add:mine:{cid}:{page + 1}"))
    if nav:
        btns.append(nav)
    btns.append([InlineKeyboardButton("📚 Перейти в каталог", callback_data=f"cx:add:catalog:{cid}")])
    btns.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"cx:add:{cid}")])
    return InlineKeyboardMarkup(btns)


def complex_add_results_kb(complex_id: int, rows: list[dict]) -> InlineKeyboardMarkup:
    cid = int(complex_id)
    btns: list[list[InlineKeyboardButton]] = []
    for item in rows[:15]:
        sid = int(item.get("id") or 0)
        title = item.get("name_ru") or item.get("name_en") or item.get("name") or f"ID {sid}"
        btns.append([InlineKeyboardButton(f"• {title}", callback_data=f"cx:add_pick:{cid}:{sid}")])
    btns.append([InlineKeyboardButton("🔎 Новый поиск", callback_data=f"cx:add:{cid}")])
    btns.append([InlineKeyboardButton("⬅️ К комплексу", callback_data=f"cx:open:{cid}")])
    return InlineKeyboardMarkup(btns)


def complex_item_actions_kb(complex_id: int, supplement_id: int) -> InlineKeyboardMarkup:
    cid = int(complex_id)
    sid = int(supplement_id)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Изменить дозу", callback_data=f"cx:item:dose:{cid}:{sid}")],
        [InlineKeyboardButton("➖ Убрать из комплекса", callback_data=f"cx:item:remove:{cid}:{sid}")],
        [InlineKeyboardButton("⬅️ К комплексу", callback_data=f"cx:open:{cid}")],
    ])


def complex_schedule_kb(complex_id: int, slots: list[dict]) -> InlineKeyboardMarkup:
    cid = int(complex_id)
    rows: list[list[InlineKeyboardButton]] = []
    for slot in slots:
        slot_id = str(slot.get("slot_id") or "")
        title = str(slot.get("title_ru") or "Слот")
        time_local = str(slot.get("time_local") or "--:--")
        rows.append([InlineKeyboardButton(f"{title} · {time_local}", callback_data=f"cx:schedule:set:{cid}:{slot_id}")])
    rows.append([InlineKeyboardButton("🧹 Убрать расписание", callback_data=f"cx:schedule:clear:{cid}")])
    rows.append([InlineKeyboardButton("⬅️ К комплексу", callback_data=f"cx:open:{cid}")])
    return InlineKeyboardMarkup(rows)


def schedule_menu_kb(*, has_custom_slots: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("Изменить время слота", callback_data="schedule:slot_pick")],
        [InlineKeyboardButton("➕ Добавить время", callback_data="schedule:add_custom")],
    ]
    if has_custom_slots:
        rows.append([InlineKeyboardButton("🗑 Удалить слот", callback_data="schedule:delete_pick")])
    rows.extend(
        [
            [InlineKeyboardButton("Назначить время добавкам", callback_data="menu:my")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="settings:notifications")],
        ]
    )
    return InlineKeyboardMarkup(rows)


def schedule_slots_pick_kb(slots: list[dict], prefix: str = "schedule:slot_time_set") -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"[{slot.get('time_local','--:--')}] {slot.get('title_ru','Слот')}", callback_data=f"{prefix}:{slot.get('slot_id','')}")] for slot in slots]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="settings:notifications")])
    return InlineKeyboardMarkup(rows)


def add_to_stack_slot_kb(supplement_id: int, slots: list[dict], back_cb: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    ordered = sorted(
        list(slots or []),
        key=lambda slot: (str(slot.get("time_local") or "99:99"), str(slot.get("slot_id") or "")),
    )
    for slot in ordered:
        slot_id = str(slot.get("slot_id") or "").strip()
        if not slot_id:
            continue
        title = str(slot.get("title_ru") or "Слот").strip() or "Слот"
        time_local = str(slot.get("time_local") or "--:--")
        rows.append([InlineKeyboardButton(f"{title} · {time_local}", callback_data=f"mysupp:addslot:pick:{supplement_id}:{slot_id}")])
    rows.append([InlineKeyboardButton("➕ Добавить слот", callback_data=f"mysupp:addslot:newtime:{supplement_id}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=back_cb)])
    return InlineKeyboardMarkup(rows)


def supplement_time_kb(
    supplement_id: int,
    slots: list[dict],
    *,
    back_cb: str | None = None,
    back_label: str = "⬅️ Назад",
    extra_rows: list[list[InlineKeyboardButton]] | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    rows.append([InlineKeyboardButton("✍️ Назначить своё время", callback_data=f"supplement:set_override:{supplement_id}")])
    for slot in slots:
        rows.append([InlineKeyboardButton(f"Слот: {slot.get('title_ru','Слот')} ({slot.get('time_local','--:--')})", callback_data=f"supplement:set_slot:{supplement_id}:{slot.get('slot_id','')}")])
    if extra_rows:
        rows.extend(extra_rows)
    rows.append([InlineKeyboardButton("⚙️ Настроить слоты", callback_data="settings:schedule")])
    rows.append([InlineKeyboardButton(back_label, callback_data=back_cb or f"mysupp:open:{supplement_id}")])
    return InlineKeyboardMarkup(rows)


def premium_plans_kb(
    plans: list[dict],
    webapp_base_url: str,
    *,
    back_cb: str = "menu:home",
    show_about: bool = True,
) -> InlineKeyboardMarkup:
    clean = str(webapp_base_url or "").rstrip("/")
    rows: list[list[InlineKeyboardButton]] = []
    for plan in plans:
        plan_id = str(plan.get("plan_id") or "")
        title = str(plan.get("title") or plan_id)
        if clean:
            url = f"{clean}/?screen=payment&kind=subscription&plan_id={plan_id}&source=bot_premium"
            rows.append([InlineKeyboardButton(title, web_app=WebAppInfo(url=url))])
        else:
            rows.append([InlineKeyboardButton(title, callback_data=f"premium:buy:{plan_id}:yookassa")])
    rows.append([InlineKeyboardButton("⭐ Купить в звездах", callback_data="premium:stars_menu")])
    if show_about:
        rows.append([InlineKeyboardButton("📦 Что входит", callback_data="premium:about")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=back_cb)])
    return InlineKeyboardMarkup(rows)


def premium_stars_kb(plans: list[dict], back_cb: str = "menu:premium") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for plan in plans:
        plan_id = str(plan.get("plan_id") or "")
        stars = int(plan.get("stars") or 0)
        if stars <= 0:
            continue
        title = str(plan.get("stars_title") or f"{plan_id} · {stars} ⭐")
        rows.append([InlineKeyboardButton(title, callback_data=f"premium:buy:{plan_id}:stars")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=back_cb)])
    return InlineKeyboardMarkup(rows)


def premium_webapp_checkout_kb(payment_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Открыть оплату", web_app=WebAppInfo(url=payment_url))],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu:premium")],
    ])
def premium_upsell_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐ Купить Premium", callback_data="menu:premium")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")],
    ])


def admin_diag_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Проверить", callback_data="admin:diag:dry")],
        [InlineKeyboardButton("Починить", callback_data="admin:diag:apply")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="admin:home")],
    ])


def admin_diag_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да, починить", callback_data="admin:diag:apply:confirm")],
        [InlineKeyboardButton("⬅️ Отмена", callback_data="admin:diag")],
    ])


def admin_paychecks_kb(*, index: int, total: int, filter_code: str, current_receipt_status: str = "pending") -> InlineKeyboardMarkup:
    safe_filter = str(filter_code or "all")
    rows: list[list[InlineKeyboardButton]] = []
    filters = [
        ("Все", "all"),
        ("Без email", "missing_email"),
        ("С email", "has_email"),
        ("Чек не отправлен", "receipt_pending"),
        ("Чек отправлен", "receipt_sent"),
        ("Хочет чека", "wants_receipt"),
    ]
    for label, code in filters:
        mark = "✅ " if safe_filter == code else ""
        rows.append([InlineKeyboardButton(f"{mark}{label}", callback_data=f"admin:paychecks:filter:{code}")])

    nav = [
        InlineKeyboardButton("⏮", callback_data="admin:paychecks:nav:first"),
        InlineKeyboardButton("◀️", callback_data="admin:paychecks:nav:prev"),
        InlineKeyboardButton(f"{max(0, index) + 1}/{max(1, total)}", callback_data="noop"),
        InlineKeyboardButton("▶️", callback_data="admin:paychecks:nav:next"),
        InlineKeyboardButton("⏭", callback_data="admin:paychecks:nav:last"),
    ]
    rows.append(nav)

    if str(current_receipt_status or "pending") == "sent":
        rows.append([InlineKeyboardButton("↩️ Сбросить статус чека", callback_data="admin:paychecks:mark_pending")])
    else:
        rows.append([InlineKeyboardButton("✅ Пометить чек отправленным", callback_data="admin:paychecks:mark_sent")])
    rows.append([InlineKeyboardButton("↩️ В админ-меню", callback_data="admin:home")])
    return InlineKeyboardMarkup(rows)
