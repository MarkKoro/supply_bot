from __future__ import annotations
import asyncio
import traceback
import calendar
import html
import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update, WebAppInfo
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler, PreCheckoutQueryHandler, ContextTypes, filters
)

from settings import get_settings, is_admin as settings_is_admin
import db
import engine
import seed_import
import rules
from ux import show
from keyboards import is_http_url
from keyboards import (
    main_menu,
    back,
    my_list_back,
    my_hub_back,
    pick_search_results,
    my_supplements_list,
    my_supplements_overview_kb,
    catalog_kb,
    catalog_home,
    catalog_list,
    catalog_search_results,
    catalog_class_picker,
    catalog_type_kb,
    catalog_body_kb,
    catalog_group_kb,
    curated_stacks_list,
    curated_stack_card,
    supplement_page,
    supplement_compat_list,
    supp_tabs_kb,
    my_supplement_hub,
    my_supplement_history_kb,
    my_supplement_reminders,
    timezone_picker,
    settings_menu,
    gift_user_kb,
    settings_notifications_kb,
    schedule_menu_kb,
    schedule_slots_pick_kb,
    add_to_stack_slot_kb,
    supplement_time_kb,
    ai_checkin_entry_kb,
    ai_checkin_voice_kb,
    today_home_kb,
    slot_intake_kb,
    premium_plans_kb,
    premium_stars_kb,
    premium_upsell_kb,
    premium_webapp_checkout_kb,
    intake_pick_kb,
    admin_stats_kb,
    admin_stacks_kb,
    admin_stacks_list_kb,
    admin_stack_editor_kb,
    admin_stack_delete_confirm_kb,
    admin_stack_items_kb,
    admin_stack_item_editor_kb,
    admin_stack_search_kb,
    admin_hub_kb,
    admin_supp_results_kb,
    admin_supp_card_kb,
    admin_supp_field_picker_kb,
    admin_supp_edit_wait_kb,
    admin_supp_toggle_confirm_kb,
    admin_supp_audit_back_kb,
    admin_diag_kb,
    admin_diag_confirm_kb,
    admin_paychecks_kb,
    complexes_hub_kb,
    complex_open_kb,
    complex_add_menu_kb,
    complex_add_mine_kb,
    complex_add_results_kb,
    complex_item_actions_kb,
    complex_schedule_kb,
    consent_kb,
)
import tz
from openai_client import (
    OpenAIConfig,
    OpenAITranscribeConfig,
    suggest_stack_v1,
    transcribe_voice_v1,
    transcribe_audio,
    ai_checkin_json,
    extract_checkin_from_text,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("supply-bot")

WAITING_KEY = "waiting"
CATALOG_PAGE_SIZE = 8
CATALOG_CLASS_PAGE_SIZE = 8
MY_PAGE_SIZE = 10
CURATED_PAGE_SIZE = 10

def _waiting_from_saved_form(route: str | None) -> str | None:
    route = str(route or "")
    literal = {"menu:add": "add_query", "menu:compat": "compat_query", "mysupp:add": "mysupp_add_query", "schedule:add_custom": "schedule:add_custom", "cx:create": "cx:create"}
    if route in literal:
        return literal[route]
    if re.fullmatch(r"mysupp:rem:add:\d+", route):
        return f"mysupp:rem_add:{route.split(':')[-1]}"
    if re.fullmatch(r"cx:add:catalog:\d+", route):
        return f"cx:add_query:{route.split(':')[-1]}"
    if re.fullmatch(r"mysupp:set_dose:\d+|supplement:set_override:\d+|mysupp:addslot:newtime:\d+|cx:rename:\d+|cx:item:dose:\d+:\d+|(?:schedule:slot_time_set|rem:set):[^:]+", route):
        return route
    return None

AI_CHECKIN_ROUTE = "menu:checkin_ai"
AI_CHECKIN_STATE = "ai_checkin_state"
AI_CHECKIN_NOTE_WAIT = "ai_checkin_note"
AI_CHECKIN_TEXT_WAIT = "ai_checkin_text"
AI_CHECKIN_VOICE_ROUTE = "checkin_voice"
COMPLEXES_BACK_ROUTE_KEY = "complexes_back_route"

GOAL_OPTIONS = [
    ("focus", "🧠 Концентрация"),
    ("energy", "⚡ Энергия"),
    ("training", "💪 Тренировка"),
    ("sleep", "😴 Сон"),
    ("calm", "🧘 Спокойствие"),
    ("recovery", "🩺 Восстановление"),
    ("gut", "🧻 ЖКТ"),
    ("mood", "🙂 Настроение"),
]

SLEEP_OPTIONS = ["4–5", "5–6", "6–7", "7–8", "8+"]
ENERGY_OPTIONS = [
    (1, "😵 Низкая"),
    (2, "🙂 Средняя"),
    (3, "⚡ Высокая"),
    (4, "🚀 Очень высокая"),
]
STRESS_OPTIONS = [
    (1, "🧊 Низкий"),
    (2, "😬 Средний"),
    (3, "🔥 Высокий"),
]

_settings_cache = None


def _parse_iso_dt(raw: str | None) -> datetime | None:
    txt = str(raw or "").strip()
    if not txt:
        return None
    try:
        return datetime.fromisoformat(txt.replace("Z", "+00:00"))
    except Exception:
        return None


def _format_start_date_and_duration(raw: str | None) -> tuple[str, str]:
    dt = _parse_iso_dt(raw)
    if not dt:
        return ("—", "—")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    start_date = dt.date()
    days = max((datetime.now(timezone.utc).date() - start_date).days, 0) + 1
    if days < 7:
        approx = f"примерно {days} дн."
    elif days < 30:
        approx = f"примерно {max(1, round(days / 7))} нед."
    elif days < 365:
        approx = f"примерно {max(1, round(days / 30))} мес."
    else:
        approx = f"примерно {max(1, round(days / 365))} г."
    return (start_date.strftime("%d.%m.%Y"), approx)


def _is_admin(tg_user_id: int) -> bool:
    global _settings_cache
    if _settings_cache is None:
        _settings_cache = get_settings()
    return settings_is_admin(tg_user_id) or tg_user_id in _settings_cache.ADMIN_IDS or tg_user_id in getattr(_settings_cache, "ADMIN_TG_IDS", set())


async def _get_role_perms_ctx(context: ContextTypes.DEFAULT_TYPE, tg_user_id: int) -> tuple[str, dict[str, bool]]:
    settings = context.application.bot_data["settings"]
    conn = await db.connect(settings.DB_PATH)
    try:
        role = await db.get_role(conn, int(tg_user_id))
    finally:
        await conn.close()
    return role, db.role_perms(role)


async def _has_perm_ctx(context: ContextTypes.DEFAULT_TYPE, tg_user_id: int, perm: str) -> bool:
    role, perms = await _get_role_perms_ctx(context, tg_user_id)
    return bool(perms.get(perm, False))


def _slugify_ascii(value: str) -> str:
    src = (value or "").strip().lower()
    src = re.sub(r"\s+", "_", src)
    src = re.sub(r"[^a-z0-9_]", "", src)
    src = re.sub(r"_+", "_", src)
    return src.strip("_")


class AdminSeedPayloadError(ValueError):
    def __init__(self, message: str, *, incomplete: bool = False):
        super().__init__(message)
        self.incomplete = incomplete


def _escape_control_chars_in_json_strings(raw_text: str) -> str:
    """
    Telegram paste can contain raw control chars (especially real newlines) inside
    JSON string values. Strict JSON parser rejects them, so we normalize by escaping
    control chars only while we are inside a JSON string token.
    """
    if not raw_text:
        return raw_text
    out: list[str] = []
    in_string = False
    escaped = False
    for ch in raw_text:
        if in_string:
            if escaped:
                out.append(ch)
                escaped = False
                continue
            if ch == "\\":
                out.append(ch)
                escaped = True
                continue
            if ch == '"':
                out.append(ch)
                in_string = False
                continue
            code = ord(ch)
            if code < 0x20:
                if ch == "\n":
                    out.append("\\n")
                elif ch == "\r":
                    out.append("\\r")
                elif ch == "\t":
                    out.append("\\t")
                elif ch == "\b":
                    out.append("\\b")
                elif ch == "\f":
                    out.append("\\f")
                else:
                    out.append(f"\\u{code:04x}")
                continue
            out.append(ch)
            continue
        out.append(ch)
        if ch == '"':
            in_string = True
            escaped = False
    return "".join(out)


def _admin_seed_error_hint(raw_text: str, pos: int) -> str:
    if not raw_text:
        return ""
    idx = max(0, min(len(raw_text) - 1, int(pos)))
    frag_start = max(0, idx - 40)
    frag_end = min(len(raw_text), idx + 40)
    fragment = raw_text[frag_start:frag_end].replace("\n", "\\n")
    pointer = " " * (idx - frag_start) + "^"
    ch = raw_text[idx]
    ch_code = f"U+{ord(ch):04X}"
    return (
        "Подсказка:\n"
        f"<code>{html.escape(fragment)}</code>\n"
        f"<code>{html.escape(pointer)}</code>\n"
        f"Символ: <code>{html.escape(repr(ch))}</code> ({ch_code})"
    )


def _admin_seed_helper_text() -> str:
    return (
        "🛠 <b>JSON-помощник (импорт в базу)</b>\n"
        "1) Допустим один объект или несколько объектов подряд.\n"
        "2) Обязательно поле <code>slug</code> в каждом объекте.\n"
        "3) Если Telegram разрежет длинный JSON на 2+ сообщений — просто отправляй части подряд: бот склеит их без потери символов.\n"
        "4) Можно вставлять многострочные поля: бот экранирует «сырые» переносы строк внутри текста.\n"
        "5) Без markdown-обёрток (<code>```json</code> ...).\n\n"
        "Мини-пример:\n"
        "<code>{\"slug\":\"magnesium_glycinate\",\"name_ru\":\"Магний глицинат\"}</code>"
    )


def _looks_like_incomplete_json(text: str, err_pos: int, err_msg: str) -> bool:
    msg = (err_msg or "").lower()
    if not text:
        return False
    if err_pos >= max(0, len(text) - 8):
        return True
    if any(x in msg for x in ("unterminated", "unexpected end", "invalid \\u", "invalid \\x")):
        return True
    if text.rstrip().endswith((",", ":", "\\", "{", "[")):
        return True
    brace_balance = 0
    bracket_balance = 0
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            brace_balance += 1
        elif ch == "}":
            brace_balance -= 1
        elif ch == "[":
            bracket_balance += 1
        elif ch == "]":
            bracket_balance -= 1
    return in_string or brace_balance > 0 or bracket_balance > 0


def _parse_admin_seed_payload(raw_text: str) -> list[dict]:
    text = _escape_control_chars_in_json_strings((raw_text or "").strip())
    if not text:
        raise AdminSeedPayloadError("Пустое сообщение.", incomplete=False)

    if text[:1] == text[-1:] and text[:1] in {'"', "'"}:
        try:
            unwrapped = json.loads(text)
            if isinstance(unwrapped, str):
                text = unwrapped.strip()
        except json.JSONDecodeError:
            pass

    decoder = json.JSONDecoder()
    pos = 0
    items: list[dict] = []
    while pos < len(text):
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            break
        try:
            obj, end_pos = decoder.raw_decode(text, pos)
        except json.JSONDecodeError as exc:
            msg = str(exc.msg or "").lower()
            looks_incomplete = (
                _looks_like_incomplete_json(text, exc.pos, msg)
                or "unterminated" in msg
                or "expecting value" in msg
                or "expecting ',' delimiter" in msg
                or "expecting property name enclosed in double quotes" in msg
            )
            hint = _admin_seed_error_hint(text, exc.pos)
            raise AdminSeedPayloadError(
                (
                    f"Не удалось прочитать JSON рядом с символом {exc.pos + 1}: {exc.msg}"
                    + (f"\n\n{hint}" if hint else "")
                ),
                incomplete=looks_incomplete,
            ) from exc
        if not isinstance(obj, dict):
            raise AdminSeedPayloadError("Каждый JSON-объект должен быть словарём.", incomplete=False)
        slug = str(obj.get("slug") or "").strip()
        if not slug:
            raise AdminSeedPayloadError("У одного из объектов отсутствует поле slug.", incomplete=False)
        items.append(obj)
        pos = end_pos

    if not items:
        raise AdminSeedPayloadError("Не найдено ни одного JSON-объекта.", incomplete=False)
    return items


async def _admin_import_seed_objects(items: list[dict]) -> list[str]:
    fd, path = tempfile.mkstemp(prefix="admin_seed_patch_", suffix=".jsonl")
    temp_path = Path(path)
    os.close(fd)
    try:
        payload = "\n".join(
            json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            for item in items
        ) + "\n"
        temp_path.write_text(payload, encoding="utf-8")
        await seed_import._import(
            supp_path=None,
            inter_path=None,
            rich_supp_path=str(temp_path),
            curated_stacks_path=None,
            curated_items_path=None,
        )
        return [str(item.get("slug") or "").strip() for item in items]
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass

CHECKIN_VOICE_MAX_SECONDS = 90
CHECKIN_VOICE_MAX_BYTES = 10 * 1024 * 1024

def _is_tz_unset(tz_name: str | None) -> bool:
    return not tz_name or str(tz_name).strip().upper() in {"UTC", "GMT"}


def _parse_user_time_input(raw: str) -> str | None:
    value = (raw or "").strip()
    if not value:
        return None
    compact = re.sub(r"\s+", "", value)
    for sep in (":", "."):
        if sep in compact:
            left, right = compact.split(sep, 1)
            if left.isdigit() and right.isdigit() and len(right) <= 2:
                hours = int(left)
                minutes = int(right)
                if 0 <= hours <= 23 and 0 <= minutes <= 59:
                    return f"{hours:02d}:{minutes:02d}"
            return None
    spaced = re.findall(r"\d+", value)
    if len(spaced) == 2:
        hours = int(spaced[0])
        minutes = int(spaced[1])
        if 0 <= hours <= 23 and 0 <= minutes <= 59:
            return f"{hours:02d}:{minutes:02d}"
        return None
    if compact.isdigit() and len(compact) in {3, 4}:
        hours = int(compact[:-2])
        minutes = int(compact[-2:])
        if 0 <= hours <= 23 and 0 <= minutes <= 59:
            return f"{hours:02d}:{minutes:02d}"
    return None

def _today_date_for_user(tz_name: str | None) -> str:
    return db.get_user_local_date(tz_name or None)


def _extract_profile(update: Update) -> tuple[str | None, str | None, str | None]:
    user = update.effective_user
    if not user:
        return None, None, None
    return user.username, user.first_name, user.last_name


async def guard_not_banned(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return True
    conn = await db.connect(context.application.bot_data["settings"].DB_PATH)
    try:
        username, first_name, last_name = _extract_profile(update)
        await db.touch_user(conn, int(user.id), username, first_name, last_name)
        status = await db.get_user_status(conn, int(user.id))
    finally:
        await conn.close()
    if int(status.get("is_banned") or 0) or str(status.get("deleted_at_utc") or "").strip():
        text = "Доступ ограничен. Если это ошибка — напишите в поддержку."
        if update.message:
            await update.message.reply_text(text)
        elif update.callback_query:
            try:
                await update.callback_query.answer("Доступ ограничен", show_alert=True)
            except Exception:
                pass
            await update.callback_query.message.reply_text(text)
        return False
    return True


async def _today_slots_payload(conn, tg_uid: int) -> tuple[str, list[dict]]:
    tz_name = await db.get_user_tz(conn, tg_uid)
    date_local = _today_date_for_user(tz_name)
    groups = await db.get_today_plan_groups(conn, tg_uid, date_local)
    status_map = await db.get_day_state_map(conn, tg_uid, date_local)
    slots_payload: list[dict] = []
    for group in groups:
        items = list(group.get("items") or [])
        complexes = list(group.get("complexes") or [])
        taken = 0
        skipped = 0
        for item in items:
            sid = int(item.get("supplement_id") or 0)
            st = status_map.get(sid, "none")
            if st == "taken":
                taken += 1
            elif st == "skipped":
                skipped += 1
        for complex_row in complexes:
            for item in list(complex_row.get("items") or []):
                sid = int(item.get("supplement_id") or 0)
                st = status_map.get(sid, "none")
                if st == "taken":
                    taken += 1
                elif st == "skipped":
                    skipped += 1
        slots_payload.append(
            {
                "slot_ref": f"{str(group.get('slot_id') or '')}|{str(group.get('time') or '').replace(':', '')}",
                "slot_id": str(group.get("slot_id") or ""),
                "title": str(group.get("title") or "Слот"),
                "time": str(group.get("time") or "--:--"),
                "items": items,
                "complexes": complexes,
                "total": len(items) + sum(len(list(c.get("items") or [])) for c in complexes),
                "taken": taken,
                "skipped": skipped,
            }
        )
    return date_local, slots_payload




def _fmt_adherence_block(label: str, block: dict) -> str:
    expected = int((block or {}).get("expected") or 0)
    taken = int((block or {}).get("taken") or 0)
    adherence = (block or {}).get("adherence")
    if expected <= 0 or adherence is None:
        return f"{label}: нет плана"
    return f"{label}: {taken}/{expected} ({int(adherence)}%)"


async def _show_stats_screen(update: Update, context: ContextTypes.DEFAULT_TYPE, conn, tg_uid: int) -> None:
    tz_name = await db.get_user_tz(conn, tg_uid)
    date_local = _today_date_for_user(tz_name)
    summary = await db.build_stats_compact(conn, tg_uid, date_local)
    misses = await db.get_top_misses(conn, tg_uid, date_local, 7, limit=5)
    stack_ids = await db.get_user_stack_supplement_ids(conn, tg_uid)
    streaks = await db.get_streaks(conn, tg_uid, date_local, stack_ids, max_back=90)

    marks_total = int((summary.get("today") or {}).get("taken") or 0) + int((summary.get("last7") or {}).get("taken") or 0) + int((summary.get("last30") or {}).get("taken") or 0)
    miss_lines = []
    for idx, row in enumerate(misses[:5], start=1):
        name = str(row.get("name_ru") or row.get("slug") or f"ID {row.get('supplement_id')}")
        miss_lines.append(f"{idx}) {name} — {int(row.get('missed') or 0)}")
    if not miss_lines:
        miss_lines = ["пока нет пропусков"] if marks_total > 0 else ["пока нет данных"]

    streak_lines = []
    for idx, row in enumerate(streaks[:5], start=1):
        name = str(row.get("name_ru") or row.get("slug") or f"ID {row.get('supplement_id')}")
        streak_lines.append(f"{idx}) {name} — {int(row.get('streak_days') or 0)} дн")
    if not streak_lines:
        streak_lines = ["пока нет активных стриков"] if marks_total > 0 else ["сначала отметьте первые приёмы"]

    text = "\n".join([
        "📈 <b>Статистика</b>",
        "",
        _fmt_adherence_block("Сегодня", summary.get("today") or {}),
        _fmt_adherence_block("7 дней", summary.get("last7") or {}),
        _fmt_adherence_block("30 дней", summary.get("last30") or {}),
        "",
        "<b>Топ пропусков (7д)</b>",
        *miss_lines,
        "",
        "<b>Топ стриков</b>",
        *streak_lines,
    ])
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Обновить", callback_data="menu:stats")],
        [InlineKeyboardButton("↩️ Назад", callback_data="menu:home")],
    ])
    await show(update, context, text, kb, route="menu:stats")


async def _show_today_home(update: Update, context: ContextTypes.DEFAULT_TYPE, conn, tg_uid: int) -> None:
    tz_name = await db.get_user_tz(conn, tg_uid)
    now_local = db.local_now(tz_name or None)
    date_local, slots = await _today_slots_payload(conn, tg_uid)
    pending_slots: list[dict] = []
    for slot in slots:
        total = int(slot.get("total") or 0)
        taken = int(slot.get("taken") or 0)
        skipped = int(slot.get("skipped") or 0)
        pending = max(0, total - taken - skipped)
        if pending <= 0:
            continue
        time_local = str(slot.get("time") or "--:--")
        minute_of_day = 24 * 60 + 1
        if re.fullmatch(r"\d{2}:\d{2}", time_local):
            hh, mm = time_local.split(":")
            minute_of_day = int(hh) * 60 + int(mm)
        pending_slots.append({**slot, "pending": pending, "minute_of_day": minute_of_day})

    pending_slots.sort(key=lambda row: (int(row.get("minute_of_day") or 0), str(row.get("slot_id") or "")))
    next_two = pending_slots[:2]
    now_hhmm = now_local.strftime("%H:%M")
    lines = [
        f"📅 <b>Сегодня · {_format_day_ru(date_local)}</b>",
        f"Сейчас у тебя {now_hhmm}.",
        "",
    ]
    if next_two:
        lines.append("<b>Ближайшие приёмы:</b>")
        for idx, slot in enumerate(next_two, start=1):
            title = str(slot.get("title") or "Слот")
            time_local = str(slot.get("time") or "--:--")
            pending = int(slot.get("pending") or 0)
            lines.append(f"{idx}) {title} — до {time_local} · осталось {pending}")
        remaining = len(pending_slots) - len(next_two)
        if remaining > 0:
            lines.append("")
            lines.append(f"После этого ожидается ещё {remaining} приём(а).")
    else:
        lines.append("На сегодня уже всё принято.")

    ai_rows = await db.list_day_plan_items_with_status(conn, tg_uid, date_local)
    ai_lines: list[str] = []
    for item in ai_rows:
        action = str(item.get("action") or "").strip().lower()
        if action not in {"add", "remove", "adjust"}:
            continue
        name = str(item.get("name_ru") or item.get("name_en") or item.get("name") or "Добавка").strip()
        time_local = db._normalize_hhmm(str(item.get("time_local") or item.get("time_hint") or "").strip())
        time_part = f"в {time_local}" if time_local else "без времени"
        action_label = {"add": "добавить", "remove": "убрать", "adjust": "изменить"}.get(action, action)
        ai_lines.append(f"• {name} — {action_label}, {time_part}")
    if ai_lines:
        lines.extend(["", "<b>🤖 Предложения AI на сегодня:</b>", *ai_lines[:6]])
        if len(ai_lines) > 6:
            lines.append(f"…и ещё {len(ai_lines) - 6}.")
    await show(
        update,
        context,
        "\n".join(lines),
        today_home_kb(next_two),
        route="today:home",
    )


async def _show_today_slots(update: Update, context: ContextTypes.DEFAULT_TYPE, conn, tg_uid: int) -> None:
    await _show_today_home(update, context, conn, tg_uid)


def _slot_intake_items(slot: dict | None) -> list[dict]:
    if not slot:
        return []
    intake_items: list[dict] = []
    seen_ids: set[int] = set()
    for item in list((slot or {}).get("items") or []):
        sid = int(item.get("supplement_id") or 0)
        if sid <= 0 or sid in seen_ids:
            continue
        seen_ids.add(sid)
        intake_items.append(item)
    for complex_row in list((slot or {}).get("complexes") or []):
        complex_name = str(complex_row.get("name") or "Комплект").strip()
        for item in list(complex_row.get("items") or []):
            sid = int(item.get("supplement_id") or 0)
            if sid <= 0 or sid in seen_ids:
                continue
            seen_ids.add(sid)
            enriched = dict(item)
            base_title = str(
                item.get("ru_name")
                or item.get("name_ru")
                or item.get("name")
                or item.get("slug")
                or f"ID {sid}"
            ).strip()
            enriched["picker_title"] = f"{base_title} · 🧩 {complex_name}"
            intake_items.append(enriched)
    return intake_items


def _find_today_slot(slots: list[dict], slot_ref_or_id: str) -> dict | None:
    key = str(slot_ref_or_id or "").strip()
    if not key:
        return None
    slot = next((s for s in slots if str(s.get("slot_ref") or "") == key), None)
    if slot:
        return slot
    return next((s for s in slots if str(s.get("slot_id") or "") == key), None)


async def _show_slot_screen(update: Update, context: ContextTypes.DEFAULT_TYPE, conn, tg_uid: int, slot_id: str) -> None:
    date_local, slots = await _today_slots_payload(conn, tg_uid)
    slot = _find_today_slot(slots, slot_id)
    if not slot:
        await show(update, context, "Слот не найден.", back("today:home"), route="today:home")
        return
    title = str(slot.get("title") or "Слот")
    time_local = str(slot.get("time") or "--:--")
    taken = int(slot.get("taken") or 0)
    total = int(slot.get("total") or 0)
    skipped = int(slot.get("skipped") or 0)
    complexes = list(slot.get("complexes") or [])
    cx_line = ""
    if complexes:
        cx_line = "\n🧩 Комплексы:\n" + "\n".join(
            [f"• {str(c.get('name') or '')}: {int(c.get('taken_count') or 0)}/{int(c.get('total_count') or 0)}" for c in complexes]
        )
    text = f"🕒 <b>{title} · {time_local}</b>\n📅 {_format_day_ru(date_local)}\n\n✅ Принято: {taken}/{total}\n⛔ Пропущено: {skipped}{cx_line}"
    await show(update, context, text, slot_intake_kb(slot_id, taken, total), route=f"today:slot:{slot_id}")


async def _show_intake_picker(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    tg_uid: int,
    slot_id: str,
    page: int,
) -> None:
    date_local, slots = await _today_slots_payload(conn, tg_uid)
    slot = _find_today_slot(slots, slot_id)
    if not slot:
        await show(update, context, "Слот не найден.", back("today:home"), route="today:home")
        return
    state_map = await db.get_day_state_map(conn, tg_uid, date_local)
    items = _slot_intake_items(slot)
    selected = {str(int(item.get("supplement_id") or 0)) for item in items if state_map.get(int(item.get("supplement_id") or 0)) == "taken"}
    title = str(slot.get("title") or "Слот")
    time_local = str(slot.get("time") or "--:--")
    await show(
        update,
        context,
        f"🧾 <b>Выбор приёма</b>\n{title} · {time_local}",
        intake_pick_kb(slot_id, items, selected, page),
        route=f"intake:pick:{slot_id}:{max(0, page)}",
    )


async def _show_reminders_home(update: Update, context: ContextTypes.DEFAULT_TYPE, conn, tg_uid: int) -> None:
    user_id = await db.get_or_create_user(conn, tg_uid, tz_name="")
    slots = await db.get_user_slots(conn, user_id)
    lines = ["⏰ <b>Напоминания</b>"]
    for slot in slots:
        state = "вкл" if slot.get("is_enabled", True) else "выкл"
        lines.append(f"• {html.escape(str(slot.get('title_ru', 'Слот')))} — {slot.get('time_local', '--:--')} ({state})")
    text = "\n".join(lines)
    rows = [
        [InlineKeyboardButton("Изменить время", callback_data="rem:edit")],
    ]
    for slot in slots:
        desired = 0 if bool(slot.get("is_enabled", True)) else 1
        rows.append([InlineKeyboardButton(f"Вкл/Выкл: {slot.get('title_ru','Слот')}", callback_data=f"rem:toggle:{slot.get('slot_id','')}:{desired}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="today:home")])
    await show(update, context, text, InlineKeyboardMarkup(rows), route="rem:home")




async def _show_schedule_home(update: Update, context: ContextTypes.DEFAULT_TYPE, conn, tg_uid: int) -> None:
    user_id = await db.get_or_create_user(conn, tg_uid, tz_name="")
    slots = await db.get_user_slots(conn, user_id)
    default_slot_ids = {str(item.get("slot_id") or "") for item in db.default_user_slots()}
    has_custom_slots = any(str(slot.get("slot_id") or "") not in default_slot_ids for slot in slots)
    lines = ["⏰ <b>Расписание</b>"]
    for slot in slots:
        if not slot.get("is_enabled", True):
            continue
        lines.append(f"• [{slot.get('time_local','--:--')}] {html.escape(str(slot.get('title_ru','Слот')))}")
    await show(update, context, "\n".join(lines), schedule_menu_kb(has_custom_slots=has_custom_slots), route="settings:schedule")


async def _show_notifications_settings(update: Update, context: ContextTypes.DEFAULT_TYPE, conn, tg_uid: int) -> None:
    user_id = await db.get_or_create_user(conn, tg_uid, tz_name="")
    slots = await db.get_user_slots(conn, user_id)
    reminders_enabled = await db.get_user_notifications_enabled(conn, tg_uid)
    text = "\n".join([
        "🔔 <b>Уведомления</b>",
        "",
        f"Общий переключатель: {'включены' if reminders_enabled else 'выключены'}",
        "Можно отключить всё сразу, слоты или управлять отдельными БАДами через «Мои БАДы».",
    ])
    await show(
        update,
        context,
        text,
        settings_notifications_kb(reminders_enabled=reminders_enabled, slots=slots),
        route="settings:notifications",
    )


async def _save_bot_supplement_schedule(conn, user_id: int, supplement_id: int, *, slot_id: str | None = None, time_override: str | None = None, set_override: bool = False) -> None:
    async with db.transaction(conn) as tx:
        member = await db.fetchone(tx, "SELECT 1 FROM user_supplements WHERE user_id=? AND supplement_id=? AND is_active=1", (int(user_id), int(supplement_id)))
        if not member:
            raise ValueError('supplement_not_found')
        if slot_id is not None:
            slots = await db.get_user_slots(tx, user_id)
            if not any(str(slot.get('slot_id') or '') == str(slot_id) for slot in slots):
                raise ValueError('slot_not_found')
            await db.set_schedule_slot(tx, user_id, supplement_id, slot_id)
        if set_override:
            await db.set_schedule_time_override(tx, user_id, supplement_id, time_override)


async def _show_supplement_time_screen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    tg_uid: int,
    supplement_id: int,
    *,
    split_origin_id: int | None = None,
) -> None:
    user_id = await db.get_or_create_user(conn, tg_uid, tz_name="")
    supplement = await db.get_user_supplement(conn, tg_uid, supplement_id)
    if not supplement:
        await show(update, context, "Добавка не найдена.", my_list_back(), route="mysupp:list:0")
        return
    slots = await db.get_user_slots(conn, user_id)
    schedule_map = await db.get_user_schedule_map(conn, user_id)
    current = schedule_map.get(supplement_id, {"slot_id": "morning", "time_hhmm": None})
    slot = next((s for s in slots if str(s.get("slot_id") or "") == str(current.get("slot_id") or "")), None)
    slot_title = html.escape(str(slot.get("title_ru") or "Утро")) if slot else "Утро"
    slot_time = str(slot.get("time_local") or "--:--") if slot else "--:--"
    reminder_profile = await db.get_effective_reminders_for_supplement(conn, tg_uid, supplement_id)
    reminder_mode = str(reminder_profile.get("mode") or "slot").strip()
    reminder_times = [str(t).strip() for t in list(reminder_profile.get("times") or []) if str(t).strip()]
    override = str(current.get("time_hhmm") or "").strip()
    title = html.escape((supplement["name_ru"] or supplement["name"] or "").strip())
    if reminder_mode == "custom" and reminder_times:
        active_time = ", ".join(reminder_times)
        time_block = (
            f"Режим: своё время\n"
            f"Текущее время: {active_time}\n"
            "Напоминание ставится по этому времени.\n\n"
            "Можно назначить новое своё время или вернуться к слоту."
        )
    else:
        effective_time = override or slot_time
        time_block = (
            f"Режим: слот\n"
            f"Слот: {slot_title} · {slot_time}\n"
            f"Текущее время: {effective_time}\n"
            "Напоминание ставится автоматически по времени слота.\n\n"
            "Можно выбрать другой слот или назначить своё время."
        )
    intro_block = ""
    extra_rows = None
    back_cb = None
    back_label = "⬅️ Назад"
    if split_origin_id:
        intro_block = (
            "✅ <b>Создан второй приём (копия)</b>\n"
            "Сейчас ты настраиваешь время напоминания именно для нового приёма.\n"
            "Выбери слот или поставь своё время.\n\n"
        )
        back_cb = f"mysupp:split:cancel:{supplement_id}:{int(split_origin_id)}"
        back_label = "✖️ Отменить второй приём"
        extra_rows = [[InlineKeyboardButton("✅ Оставить второй приём", callback_data=f"mysupp:split:apply:{supplement_id}")]]
    text = (
        f"⏰ <b>Время приёма</b>\n\n"
        f"{intro_block}"
        f"💊 {title}\n"
        f"{time_block}"
    )
    await show(
        update,
        context,
        text,
        supplement_time_kb(
            supplement_id,
            slots,
            back_cb=back_cb,
            back_label=back_label,
            extra_rows=extra_rows,
        ),
        route=f"supplement:set_time:{supplement_id}",
    )


CATALOG_TYPE_TITLES = {
    "vitamin": "Витамины",
    "mineral": "Минералы",
    "amino_acid": "Аминокислоты",
    "fatty_acid": "Жирные кислоты",
    "probiotic": "Пробиотики",
    "prebiotic_fiber": "Пребиотики/клетчатка",
    "enzyme": "Ферменты",
    "botanical_extract": "Экстракты растений",
    "mushroom": "Грибы",
    "nootropic": "Ноотропы",
    "ergogenic": "Спортивные",
    "other": "Другое",
}

CATALOG_BODY_TITLES = {
    "brain_cns": "Мозг/ЦНС",
    "heart_cv": "Сердце",
    "vessels_blood": "Сосуды/кровь",
    "liver": "Печень",
    "kidney": "Почки",
    "gut": "ЖКТ",
    "pancreas_glucose": "Глюкоза/инсулин",
    "thyroid": "Щитовидка",
    "immune": "Иммунитет",
    "joints": "Суставы",
    "bone": "Кости",
    "muscle": "Мышцы",
    "skin_hair": "Кожа/волосы",
    "reproductive_hormones": "Гормоны/репродукция",
}

CATALOG_GROUP_TITLES = {
    "adaptogen": "Адаптогены",
    "stimulant": "Стимуляторы",
    "calming": "Успокаивающее",
    "sleep_aid": "Сон",
    "nootropic_focus": "Фокус/ноотропы",
    "mood_stress": "Стресс/настроение",
    "anti_inflammatory": "Противовоспалительное",
    "antioxidant": "Антиоксиданты",
    "cardio_support": "Сердце",
    "metabolic_glucose": "Глюкоза/метаболизм",
    "gut_support": "ЖКТ",
    "immune_support": "Иммунитет",
    "joint_support": "Суставы",
    "bone_support": "Кости",
    "performance_endurance": "Выносливость",
    "recovery_muscle": "Восстановление",
}

async def _safe_answer_callback(update: Update, text: str | None = None) -> None:
    q = update.callback_query
    if not q:
        return
    try:
        await q.answer(**({"text": text} if text else {}))
    except Exception as exc:
        log.warning("Failed to answer callback query: %s", exc)

async def _download_voice_bytes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[bytes, str]:
    voice = update.message.voice
    if ((voice.duration and voice.duration > CHECKIN_VOICE_MAX_SECONDS)
            or (voice.file_size and voice.file_size > CHECKIN_VOICE_MAX_BYTES)):
        raise ValueError("voice_too_large")
    file = await context.bot.get_file(voice.file_id)
    if file.file_size and file.file_size > CHECKIN_VOICE_MAX_BYTES:
        raise ValueError("voice_too_large")
    data = await file.download_as_bytearray()
    if len(data) > CHECKIN_VOICE_MAX_BYTES:
        raise ValueError("voice_too_large")
    filename = f"voice_{update.message.message_id}.ogg"
    return bytes(data), filename

async def download_telegram_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Path:
    voice = update.message.voice
    tg_uid = update.effective_user.id if update.effective_user else update.message.message_id
    timestamp = int(time.time() * 1000)
    tmp_dir = Path("data/tmp")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    path = tmp_dir / f"voice_{tg_uid}_{timestamp}.ogg"
    file = await context.bot.get_file(voice.file_id)
    await file.download_to_drive(custom_path=str(path))
    return path

def _format_day_ru(day: str) -> str:
    try:
        return datetime.strptime(day, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return day


def _format_until_ru(iso_value: str | None) -> str:
    raw = str(iso_value or "").strip()
    if not raw:
        return "—"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return raw


async def _render_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, conn, tg_uid: int) -> None:
    user_id = await db.get_or_create_user(conn, tg_uid)
    tz_name = await db.get_user_tz(conn, tg_uid) or "UTC"
    reminders_enabled = await db.get_user_notifications_enabled(conn, tg_uid)
    slots = await db.get_user_slots(conn, user_id)
    enabled_slots = [f"{str(s.get('title_ru') or 'Слот')} ({str(s.get('time_local') or '--:--')})" for s in slots if bool(s.get("is_enabled", False))]
    if reminders_enabled and enabled_slots:
        notif_line = " · ".join(enabled_slots[:3])
        if len(enabled_slots) > 3:
            notif_line += f" и ещё {len(enabled_slots) - 3}"
    else:
        notif_line = "сейчас включённых уведомлений нет"

    summary = await db.build_stats_compact(conn, tg_uid, _today_date_for_user(tz_name))
    stats_available = sum(1 for key in ("today", "last7", "last30") if int((summary.get(key) or {}).get("expected") or 0) > 0)

    premium_active = await db.is_premium_active(conn, tg_uid)
    premium_until = await db.get_premium_until(conn, tg_uid) if premium_active else None
    premium_line = f"активен до {_format_until_ru(premium_until)}" if premium_active else "сейчас активной подписки нет — можно купить"
    starter_active = await db.is_starter_trial_active(conn, tg_uid)
    starter_row = await db.fetchone(
        conn,
        "SELECT starter_trial_used FROM users WHERE tg_user_id=?",
        (int(tg_uid),),
    )
    starter_used = int(starter_row["starter_trial_used"] or 0) if starter_row else 0

    complexes = await db.list_complexes(conn, tg_uid)
    complexes_count = len(complexes)
    complexes_line = f"доступны к редактированию · создано: {complexes_count}"

    text = (
        "⚙️ <b>Настройки</b>\n\n"
        f"🕒 Часовой пояс: <b>{tz_name}</b>\n"
        f"🔔 Уведомления: {notif_line}\n"
        f"📈 Статистика: доступно элементов — {stats_available}/3\n"
        f"⭐ Премиум: {premium_line}\n"
        f"🧩 Комплексы: {complexes_line}"
    )
    await show(
        update,
        context,
        text,
        settings_menu(show_starter_activation=(not premium_active and not starter_active and starter_used == 0)),
        route="menu:settings",
    )


async def _show_gift_hub(update: Update, context: ContextTypes.DEFAULT_TYPE, tg_uid: int) -> None:
    is_admin = _is_admin(tg_uid)
    text = (
        "🎁 <b>Подарочный код</b>\n\n"
        "Отправьте код отдельным сообщением, чтобы активировать Premium.\n"
        "Мы удалим сообщение с кодом автоматически."
    )
    if is_admin:
        text += "\n\nДля администратора доступны создание и управление кодами."
    await show(update, context, text, gift_user_kb(is_admin=is_admin), route="settings:gift")


def _format_link_names(items: list[dict]) -> str:
    if not items:
        return "Пока нет данных."
    labels = []
    for item in items:
        label = item.get("name_ru") or item.get("name_en") or "Без названия"
        labels.append(f"• {html.escape(str(label))}")
    return "\n".join(labels)

async def ensure_timezone_or_redirect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    s = context.application.bot_data.get("settings")
    if not s:
        return False
    tg_user = update.effective_user
    if not tg_user:
        return False
    conn = await db.connect(s.DB_PATH)
    try:
        await db.get_or_create_user(conn, tg_user.id, tz_name="")
        tz_name = await db.get_user_tz(conn, tg_user.id)
        if _is_tz_unset(tz_name):
            context.user_data.pop("tz_back_data", None)
            context.user_data["onboarding"] = "tz"
            await _show_timezone_picker(update, context, 0)
            return False
        return True
    finally:
        await conn.close()

def _get_ai_checkin_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    state = context.user_data.get(AI_CHECKIN_STATE)
    if isinstance(state, dict):
        return state
    state = {
        "step": 1,
        "target_day": "today",
        "answers": {
            "sleep_range": "",
            "energy": None,
            "stress": None,
            "training": None,
            "goals": [],
            "note": "",
        },
        "note_pending": False,
        "last_ai_actions_json": "",
        "selected_action_ids": [],
    }
    context.user_data[AI_CHECKIN_STATE] = state
    return state

def _set_ai_checkin_state(context: ContextTypes.DEFAULT_TYPE, state: dict) -> None:
    context.user_data[AI_CHECKIN_STATE] = state

def _goal_label(code: str) -> str:
    for key, label in GOAL_OPTIONS:
        if key == code:
            return label
    return code

def _ai_checkin_step_title(step: int) -> str:
    return f"📝 <b>Ежедневный опрос</b>\n\nШаг {step}/5"

def _ai_checkin_actions_payload(state: dict) -> dict:
    raw = state.get("last_ai_actions_json")
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}

def _ai_checkin_entry_payload() -> dict:
    return {"screen": "entry"}

def _ai_checkin_target_day_label(target_day: str) -> str:
    return "завтра" if target_day == "tomorrow" else "сегодня"


def _get_ai_checkin_target_day(context: ContextTypes.DEFAULT_TYPE) -> str:
    raw = str(context.user_data.get("ai_checkin_target_day") or "today").strip().lower()
    if raw not in {"today", "tomorrow"}:
        raw = "today"
    context.user_data["ai_checkin_target_day"] = raw
    return raw


def _set_ai_checkin_target_day(context: ContextTypes.DEFAULT_TYPE, target_day: str) -> str:
    normalized = "tomorrow" if str(target_day).strip().lower() == "tomorrow" else "today"
    context.user_data["ai_checkin_target_day"] = normalized
    return normalized

def _ai_checkin_voice_payload() -> dict:
    return {"note_pending": True, "return_screen": "main", "date_local": ""}

async def _render_ai_checkin_entry(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    target_day = _get_ai_checkin_target_day(context)
    target_label = _ai_checkin_target_day_label(target_day)
    text = (
        "📝 <b>Ежедневный опрос</b>\n\n"
        "Расскажи, как прошло утро и вчерашний день — это поможет подобрать изменения "
        f"в стеке на {target_label}: усилить эффект, убрать лишнее или предложить эксперимент.\n\n"
        "Мы уже учитываем, что ты вчера выпил(а) и что пропустил(а), а также все изменения, "
        "которые ты делал(а) в стеке.\n\n"
        "Выбери формат:\n"
        "• Быстрый опрос (кнопки)\n"
        "• Свободный текст — просто напиши одним сообщением\n"
        "• Опрос голосом — отправь голосовое"
    )
    await show(
        update,
        context,
        text,
        ai_checkin_entry_kb(target_day),
        route=AI_CHECKIN_ROUTE,
        payload=_ai_checkin_entry_payload(),
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )

async def _render_ai_checkin_voice_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    text = (
        "🎙 Запиши голосом одним сообщением:\n"
        "— как спал(а)\n"
        "— энергия/стресс\n"
        "— будет ли тренировка\n"
        "— цель на сегодня (концентрация/энергия/сон/жкт/настроение/восстановление)\n"
        "Можно говорить свободно. Я сам(а) разберу.\n"
        "Важно: это не медсовет."
    )
    await show(
        update,
        context,
        text,
        ai_checkin_voice_kb(),
        route=AI_CHECKIN_VOICE_ROUTE,
        payload=_ai_checkin_voice_payload(),
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )

async def _render_ai_checkin_text_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    text = (
        "✍️ Напиши одним сообщением, как прошло утро и вчерашний день: самочувствие, "
        "энергия, стресс, тренировка, любые симптомы или планы. Я разберу текст и подготовлю рекомендации."
    )
    await show(
        update,
        context,
        text,
        InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="aicheck:entry")]]),
        route=AI_CHECKIN_ROUTE,
        payload=_ai_checkin_entry_payload(),
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )

def _build_adherence_payload(
    stack: list[dict],
    status_rows: list[dict],
    days: list[str],
) -> list[dict]:
    status_map: dict[int, dict[str, str]] = {}
    for row in status_rows:
        supplement_id = int(row["supplement_id"])
        status_map.setdefault(supplement_id, {})[str(row["day"])] = str(row["status"] or "")

    payload: list[dict] = []
    for item in stack:
        supplement_id = int(item.get("supplement_id") or 0)
        day_statuses = status_map.get(supplement_id, {})
        compact = ""
        for day in days:
            status = day_statuses.get(day)
            if status == "taken":
                compact += "T"
            elif status == "skipped":
                compact += "S"
            else:
                compact += "-"
        payload.append(
            {
                "supplement_id": supplement_id,
                "slug": item.get("slug"),
                "ru_name": item.get("ru_name"),
                "dose_text": item.get("dose_text") or "",
                "time_local": item.get("time_local") or "",
                "days": days,
                "status_compact": compact,
            }
        )
    return payload

async def _render_ai_checkin_wizard(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    state: dict | None = None,
) -> None:
    state = state or _get_ai_checkin_state(context)
    step = int(state.get("step") or 1)
    answers = state.get("answers", {}) if isinstance(state.get("answers"), dict) else {}

    if step == 1:
        text = f"{_ai_checkin_step_title(1)}\n\nСколько часов спал(а)?"
        rows = []
        for option in SLEEP_OPTIONS:
            selected = answers.get("sleep_range") == option
            label = f"✅ {option}" if selected else option
            rows.append([InlineKeyboardButton(label, callback_data=f"aicheck:sleep:{option}")])
        next_cb = "aicheck:next" if answers.get("sleep_range") else "noop"
        rows.append([InlineKeyboardButton("Далее", callback_data=next_cb)])
        rows.append([InlineKeyboardButton("🏠 Меню", callback_data="menu:home")])
        await show(
            update,
            context,
            text,
            InlineKeyboardMarkup(rows),
            route=AI_CHECKIN_ROUTE,
            payload={"ai_checkin": state},
        )
        return

    if step == 2:
        text = f"{_ai_checkin_step_title(2)}\n\nЭнергия сейчас?"
        rows = []
        for value, label in ENERGY_OPTIONS:
            selected = answers.get("energy") == value
            title = f"✅ {label}" if selected else label
            rows.append([InlineKeyboardButton(title, callback_data=f"aicheck:energy:{value}")])
        next_cb = "aicheck:next" if answers.get("energy") else "noop"
        rows.append(
            [
                InlineKeyboardButton("⬅️ Назад", callback_data="aicheck:back"),
                InlineKeyboardButton("Далее", callback_data=next_cb),
            ]
        )
        rows.append([InlineKeyboardButton("🏠 Меню", callback_data="menu:home")])
        await show(
            update,
            context,
            text,
            InlineKeyboardMarkup(rows),
            route=AI_CHECKIN_ROUTE,
            payload={"ai_checkin": state},
        )
        return

    if step == 3:
        text = f"{_ai_checkin_step_title(3)}\n\nСтресс вчера?"
        rows = []
        for value, label in STRESS_OPTIONS:
            selected = answers.get("stress") == value
            title = f"✅ {label}" if selected else label
            rows.append([InlineKeyboardButton(title, callback_data=f"aicheck:stress:{value}")])
        next_cb = "aicheck:next" if answers.get("stress") else "noop"
        rows.append(
            [
                InlineKeyboardButton("⬅️ Назад", callback_data="aicheck:back"),
                InlineKeyboardButton("Далее", callback_data=next_cb),
            ]
        )
        rows.append([InlineKeyboardButton("🏠 Меню", callback_data="menu:home")])
        await show(
            update,
            context,
            text,
            InlineKeyboardMarkup(rows),
            route=AI_CHECKIN_ROUTE,
            payload={"ai_checkin": state},
        )
        return

    if step == 4:
        text = f"{_ai_checkin_step_title(4)}\n\nТренировка сегодня?"
        rows = []
        selected_value = answers.get("training")
        for value, label in [(True, "✅ Да"), (False, "❌ Нет")]:
            selected = selected_value is value
            title = f"✅ {label}" if selected else label
            rows.append([InlineKeyboardButton(title, callback_data=f"aicheck:training:{int(value)}")])
        next_cb = "aicheck:next" if answers.get("training") is not None else "noop"
        rows.append(
            [
                InlineKeyboardButton("⬅️ Назад", callback_data="aicheck:back"),
                InlineKeyboardButton("Далее", callback_data=next_cb),
            ]
        )
        rows.append([InlineKeyboardButton("🏠 Меню", callback_data="menu:home")])
        await show(
            update,
            context,
            text,
            InlineKeyboardMarkup(rows),
            route=AI_CHECKIN_ROUTE,
            payload={"ai_checkin": state},
        )
        return

    goals = answers.get("goals", [])
    note_text = str(answers.get("note") or "").strip()
    text_lines = [
        _ai_checkin_step_title(5),
        "",
        "Цель на сегодня (можно несколько):",
    ]
    if note_text:
        snippet = note_text if len(note_text) <= 120 else f"{note_text[:117]}..."
        text_lines.extend(["", f"Заметка: <i>{snippet}</i>"])
    text = "\n".join(text_lines)

    rows = []
    row: list[InlineKeyboardButton] = []
    for code, label in GOAL_OPTIONS:
        selected = code in goals
        mark = "✅" if selected else "⬜"
        row.append(InlineKeyboardButton(f"{mark} {label}", callback_data=f"aicheck:goal:{code}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    note_label = "✍️ Изменить заметку" if note_text else "✍️ Добавить заметку"
    rows.append([InlineKeyboardButton(note_label, callback_data="aicheck:note")])
    next_cb = "aicheck:finish" if goals or note_text else "noop"
    rows.append(
        [
            InlineKeyboardButton("⬅️ Назад", callback_data="aicheck:back"),
            InlineKeyboardButton("Далее", callback_data=next_cb),
        ]
    )
    rows.append([InlineKeyboardButton("🏠 Меню", callback_data="menu:home")])
    await show(
        update,
        context,
        text,
        InlineKeyboardMarkup(rows),
        route=AI_CHECKIN_ROUTE,
        payload={"ai_checkin": state},
    )

def build_ai_checkin_prompt(
    user: dict,
    stack: list[dict],
    checkin: dict,
    candidates: list[dict],
    adherence_payload: list[dict],
    recent_checkins_payload: list[dict],
    ai_plan_history_payload: list[dict],
    target_day_state_payload: list[dict],
) -> list[dict]:
    target_day = str(checkin.get("target_day") or "today")
    target_day_ru = "завтра" if target_day == "tomorrow" else "сегодня"
    system_msg = (
        "Ты — осторожный помощник по добавкам. "
        "Никаких диагнозов и назначений. "
        "Рекомендации только по предоставленным данным. "
        "Тебе передан полный контекст: текущий чек-ин, история приема за 14 дней, "
        "история AI-предложений и ответы в прошлых чек-инах. "
        "Обязательно анализируй динамику в прогрессии, что помогало и что не соблюдалось, "
        f"и на этой основе давай адаптированные предложения только на {target_day_ru}. "
        "Добавки из текущего стека (base_stack) можно только remove или adjust, но не add. "
        "Для action=add разрешены только новые добавки из catalog_candidates, которых нет в base_stack. "
        "Для action=remove и action=adjust разрешены только добавки из base_stack. "
        "Если в today_checkin.target_day_state есть статус taken, значит добавка уже принята за целевой день: "
        "для таких добавок запрещены remove/adjust. "
        "Нельзя предлагать добавки вне списка кандидатов и текущего стека. "
        "Если пользователь явно просит рекомендации (особенно по энергии/силе/тренировке) "
        "и нет противопоказаний в контексте, обязательно дай минимум 1 безопасное действие add из catalog_candidates. "
        "Не предлагай рецептурные препараты. "
        "Будь консервативным, максимум 6 действий. "
        "Если уверенность низкая — явно скажи. "
        "Для каждого действия suggested_time обязателен и строго в формате HH:MM. "
        "prefer_existing_time всегда true: выбирай из уже существующих слотов пользователя, если возможно. "
        "Верни строго JSON без Markdown."
    )

    context_payload = {
        "user": {"tz_name": user.get("tz_name")},
        "today_checkin": checkin,
        "base_stack": stack,
        "adherence_14d": adherence_payload,
        "ai_plan_14d": ai_plan_history_payload,
        "recent_checkins_14d": recent_checkins_payload,
        "target_day_state": target_day_state_payload,
        "catalog_candidates": candidates,
    }
    context_json = json.dumps(context_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    schema = """{
  "actions": [
    {
      "id": "a1",
      "type": "add" | "remove" | "adjust",
      "slug": "caffeine",
      "dose_text": "200 mg",
      "suggested_time": "08:00",
      "prefer_existing_time": true,
      "confidence": 0.0,
      "reason_short_ru": "коротко почему",
      "safety_ru": "короткая фраза о рисках"
    }
  ],
  "summary_ru": "резюме на 2-4 строки",
  "disclaimer_ru": "Не медсовет..."
}"""

    user_msg = "\n".join(
        [
            "Используй контекст JSON ниже. Учитывай как самочувствие, так и реальную приверженность (выпил/не выпил).",
            "КОНТЕКСТ_JSON:",
            context_json,
            "",
            "ФОРМАТ ОТВЕТА (строго JSON):",
            schema,
        ]
    )
    return [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_msg},
    ]

def _extract_json_content(raw: str) -> str:
    content = str(raw or "").strip()
    if not content.startswith("```"):
        return content
    lines = content.splitlines()
    if not lines:
        return ""
    first = lines[0].strip().lower()
    if not first.startswith("```"):
        return content
    body = lines[1:]
    if body and body[-1].strip().startswith("```"):
        body = body[:-1]
    return "\n".join(body).strip()


def _parse_ai_json(raw: str) -> dict | None:
    if not raw:
        return None
    content = _extract_json_content(raw)

    def _decode_json_object(payload: str) -> dict | None:
        text = str(payload or "").strip()
        if not text:
            return None

        # 1) Базовый путь: корректный JSON-объект.
        try:
            parsed_local = json.loads(text)
            if isinstance(parsed_local, dict):
                return parsed_local
            if isinstance(parsed_local, str):
                nested = _decode_json_object(parsed_local)
                if nested is not None:
                    return nested
        except json.JSONDecodeError:
            pass

        # 2) Иногда модель возвращает JSON c мусором до/после.
        decoder = json.JSONDecoder()
        for idx, ch in enumerate(text):
            if ch != "{":
                continue
            try:
                parsed_local, end_idx = decoder.raw_decode(text[idx:])
            except json.JSONDecodeError:
                continue
            tail = text[idx + end_idx :].strip().strip('"').strip()
            if isinstance(parsed_local, dict) and not tail:
                return parsed_local
            if isinstance(parsed_local, dict) and tail in {"```", "```json"}:
                return parsed_local

        # 3) Последний шанс: вырезаем самый внешний блок {...}.
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return None
        try:
            parsed_local = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        return parsed_local if isinstance(parsed_local, dict) else None

    return _decode_json_object(content)


def _parse_onboarding_ai_json(raw: str) -> dict | None:
    parsed = _parse_ai_json(raw)
    if not isinstance(parsed, dict):
        return None
    payload = dict(parsed)
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        payload["candidates"] = []
    warnings = payload.get("warnings_ru")
    if not isinstance(warnings, list):
        payload["warnings_ru"] = []
    questions = payload.get("questions_ru")
    if not isinstance(questions, list):
        payload["questions_ru"] = []
    return payload




def _extract_goal_fragments(goal_text: str) -> list[str]:
    text = str(goal_text or '').strip()
    if not text:
        return []
    lowered = re.sub(r"[.!?]+", ",", text.lower())
    lowered = lowered.replace(" и ", ",")
    parts = [p.strip(" ,;:-") for p in lowered.split(",")]
    stop = {"хочу", "нужно", "надо", "чтобы", "по чуть чуть", "в целом", "вообще"}
    out: list[str] = []
    for part in parts:
        if not part or len(part) < 3:
            continue
        if part in stop:
            continue
        if part not in out:
            out.append(part)
    return out[:6]

def _normalize_onboarding_goals(data: dict | None, fallback_goal: str) -> list[dict]:
    payload = data or {}
    goals = payload.get("goals")
    normalized_goals: list[dict] = []
    pending_without_candidates: list[str] = []

    if isinstance(goals, list):
        for item in goals:
            if not isinstance(item, dict):
                continue
            goal_name = str(item.get("goal_ru") or item.get("goal") or "").strip()
            if not goal_name:
                goal_name = fallback_goal
            raw_candidates = item.get("candidates")
            if isinstance(raw_candidates, list):
                normalized_goals.append({"goal_ru": goal_name, "candidates": raw_candidates})
            else:
                pending_without_candidates.append(goal_name)

    if normalized_goals and pending_without_candidates:
        existing = {str(item.get("goal_ru") or "").strip().lower() for item in normalized_goals}
        for goal_name in pending_without_candidates:
            key = goal_name.strip().lower()
            if not key or key in existing:
                continue
            normalized_goals.append({"goal_ru": goal_name, "candidates": []})
            existing.add(key)

    if not normalized_goals:
        legacy_candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
        normalized_goals = [{"goal_ru": fallback_goal, "candidates": legacy_candidates}]

    # Если модель пропустила часть целей, добавляем их из исходного текста как отдельные блоки.
    extracted = _extract_goal_fragments(fallback_goal)
    existing = {str(item.get("goal_ru") or "").strip().lower() for item in normalized_goals}
    for frag in extracted:
        if frag in existing:
            continue
        normalized_goals.append({"goal_ru": frag, "candidates": []})

    return normalized_goals


async def _resolve_onboarding_goal_candidates(conn, data: dict | None, fallback_goal: str) -> tuple[list[dict], list[str]]:
    goal_blocks = _normalize_onboarding_goals(data, fallback_goal)
    grouped_found: list[dict] = []
    missing: list[str] = []
    seen_ids: set[int] = set()

    for block in goal_blocks:
        goal_name = str(block.get("goal_ru") or fallback_goal).strip() or fallback_goal
        candidates = block.get("candidates") if isinstance(block.get("candidates"), list) else []
        found: list[dict] = []
        for cand in candidates:
            if not isinstance(cand, dict):
                continue
            query_variants = []
            name = str(cand.get("name") or "").strip()
            name_ru = str(cand.get("name_ru") or "").strip()
            for variant in [name, name_ru]:
                if variant and variant not in query_variants:
                    query_variants.append(variant)
            if not query_variants:
                continue

            matched_row = None
            for query in query_variants:
                rows, _ = await db.search_supplements(conn, query, page=0, page_size=1)
                if rows:
                    matched_row = rows[0]
                    break

            if matched_row:
                supp_id = int(matched_row["id"])
                if supp_id in seen_ids:
                    continue
                seen_ids.add(supp_id)
                found.append({"row": matched_row, "cand": cand, "goal_ru": goal_name})
            else:
                missing_name = name_ru or name
                if missing_name:
                    missing.append(missing_name)

        # Добираем кандидатов по цели из каталога, если модель выдала мало или пусто.
        if len(found) < 4:
            fallback_rows, _ = await db.search_supplements(conn, goal_name, page=0, page_size=8)
            for row in fallback_rows:
                supp_id = int(row["id"])
                if supp_id in seen_ids:
                    continue
                seen_ids.add(supp_id)
                found.append(
                    {
                        "row": row,
                        "cand": {
                            "why_ru": "Подобрано по цели из каталога.",
                            "dose_ru": "Уточни дозировку в карточке перед началом.",
                            "timing_ru": "Выбери время по карточке и переносимости.",
                        },
                        "goal_ru": goal_name,
                    }
                )
                if len(found) >= 4:
                    break

        if found:
            grouped_found.append({"goal_ru": goal_name, "items": found})

    return grouped_found, missing


async def _render_ai_checkin_results(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    date_local: str,
    payload: dict,
    missing_count: int = 0,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    summary = str(payload.get("summary_ru") or "").strip() or "Короткое резюме недоступно."
    disclaimer = str(payload.get("disclaimer_ru") or "").strip()
    actions = payload.get("actions") or []
    state = _get_ai_checkin_state(context)
    target_day_label = _ai_checkin_target_day_label(str(state.get("target_day") or "today"))
    lines = [
        f"🤖 AI-рекомендации на {target_day_label} ({_format_day_ru(date_local)})",
        "",
        summary,
        "",
    ]
    if actions:
        for idx, action in enumerate(actions, start=1):
            action_type = action.get("type")
            ru_name = action.get("ru_name") or action.get("slug") or "добавка"
            slug = action.get("slug") or ""
            type_map = {"add": "ДОБАВИТЬ", "remove": "УБРАТЬ", "adjust": "ИЗМЕНИТЬ"}
            label = type_map.get(action_type, "ИЗМЕНИТЬ")
            details = []
            dose_text = str(action.get("dose_text") or "").strip()
            time_hint = str(action.get("suggested_time") or action.get("time_hint") or "").strip()
            if dose_text:
                details.append(dose_text)
            if time_hint:
                details.append(time_hint)
            detail_text = f" — {', '.join(details)}" if details else ""
            confidence = action.get("confidence")
            confidence_text = ""
            if isinstance(confidence, (float, int)):
                confidence_text = f" (уверенность {confidence:.2f})"
            lines.append(f"{idx}) {label}: {ru_name} ({slug}){detail_text}{confidence_text}")
            reason = str(action.get("reason_short_ru") or "").strip()
            safety = str(action.get("safety_ru") or "").strip()
            if reason:
                lines.append(f"   {reason}")
            if safety:
                lines.append(f"   ⚠️ {safety}")
    else:
        lines.append("Нет предложенных действий.")

    if missing_count:
        lines.append("")
        lines.append(f"Пропущено действий: {missing_count} — не найдено в каталоге.")

    if disclaimer:
        lines.extend(["", disclaimer])

    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Применить всё", callback_data="aicheck:apply:all")],
            [InlineKeyboardButton("🎯 Выбрать точечно", callback_data="aicheck:select")],
            [InlineKeyboardButton("❌ Не применять", callback_data="aicheck:deny")],
        ]
    )
    await show(
        update,
        context,
        "\n".join(lines),
        kb,
        route=AI_CHECKIN_ROUTE,
        payload={"ai_checkin": _get_ai_checkin_state(context)},
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )

async def _render_ai_checkin_selection(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    payload: dict,
    selected_ids: set[str],
    date_local: str,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    actions = payload.get("actions") or []
    lines = [f"🎯 Выбор действий ({_format_day_ru(date_local)})", ""]
    rows = []
    if actions:
        for idx, action in enumerate(actions, start=1):
            action_id = str(action.get("id") or "")
            ru_name = action.get("ru_name") or action.get("slug") or "добавка"
            mark = "✅" if action_id in selected_ids else "⬜"
            rows.append(
                [InlineKeyboardButton(f"{mark} {idx}) {ru_name}", callback_data=f"aicheck:toggle:{action_id}")]
            )
        rows.append(
            [
                InlineKeyboardButton("✅ Применить выбранное", callback_data="aicheck:apply:selected"),
            ]
        )
    else:
        lines.append("Нет действий для выбора.")
    rows.append(
        [
            InlineKeyboardButton("⬅️ Назад", callback_data="aicheck:back:results"),
            InlineKeyboardButton("🏠 Меню", callback_data="menu:home"),
        ]
    )
    await show(
        update,
        context,
        "\n".join(lines),
        InlineKeyboardMarkup(rows),
        route=AI_CHECKIN_ROUTE,
        payload={"ai_checkin": _get_ai_checkin_state(context)},
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )

async def _render_ai_suggestions(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    goal_text: str,
    raw: str | None,
    *,
    prefix_line: str | None = None,
    save_to_onboarding: bool = True,
) -> bool:
    data = None
    fallback_mode = False
    if raw:
        data = _parse_onboarding_ai_json(raw)
        if data is None:
            fallback_mode = True
    else:
        fallback_mode = True

    if fallback_mode:
        extracted_goals = _extract_goal_fragments(goal_text)
        limited_goals = extracted_goals[:6] if extracted_goals else [goal_text]
        data = {
            "goals": [
                {"goal_ru": goal_name, "candidates": []}
                for goal_name in limited_goals
                if str(goal_name).strip()
            ],
            "candidates": [],
            "warnings_ru": [
                "AI-ответ пришёл в неполном формате, поэтому подбор восстановлен из каталога.",
                "Проверь переносимость и взаимодействия по карточкам добавок перед стартом.",
            ],
            "questions_ru": [],
        }

    if isinstance(data, dict):
        grouped_found, missing = await _resolve_onboarding_goal_candidates(conn, data, goal_text)
        if not grouped_found:
            fallback_rows, _ = await db.search_supplements(conn, goal_text, page=0, page_size=8)
            if fallback_rows:
                grouped_found = [{
                    "goal_ru": goal_text,
                    "items": [
                        {
                            "row": row,
                            "cand": {
                                "why_ru": "Подобрано по запросу из каталога.",
                                "dose_ru": "Смотри рекомендованную дозировку в карточке.",
                                "timing_ru": "Подбери время по описанию и самочувствию.",
                            },
                        }
                        for row in fallback_rows
                    ],
                }]

        lines = [
            "✨ <b>Начальный подбор</b>",
            "",
        ]
        if fallback_mode:
            lines.append("⚠️ Ответ AI восстановлен из каталога (резервный режим).")
            lines.append("")

        all_found_items: list[dict] = []
        if grouped_found:
            lines.append("Подобрал по целям:")
            for block in grouped_found:
                goal_name = str(block.get("goal_ru") or goal_text).strip() or goal_text
                items = block.get("items") if isinstance(block.get("items"), list) else []
                if not items:
                    continue
                lines.append("")
                lines.append(f"🎯 <b>{goal_name}</b>")
                for idx, item in enumerate(items, start=1):
                    row = item["row"]
                    cand = item["cand"]
                    all_found_items.append(item)
                    dose = str(cand.get("dose_ru") or "").strip()
                    timing = str(cand.get("timing_ru") or "").strip()
                    why = str(cand.get("why_ru") or "").strip()
                    confidence = cand.get("confidence")
                    try:
                        confidence = int(confidence)
                    except (TypeError, ValueError):
                        confidence = None
                    evidence_grade = str(row.get("evidence_grade") or "").strip().upper()
                    evidence_line = f"Доказательность: {evidence_grade}" if evidence_grade else "Доказательность: не указана"
                    detail_bits = []
                    if dose:
                        detail_bits.append(f"доза: {dose}")
                    if timing:
                        detail_bits.append(f"время: {timing}")
                    detail = " · ".join(detail_bits)
                    line = f"{idx}) <b>{row['name']}</b>"
                    if detail:
                        line = f"{line} — {detail}"
                    if why:
                        line = f"{line}\n{why}"
                    line = f"{line}\n{evidence_line}"
                    if confidence is not None:
                        line = f"{line}\nУверенность: {confidence}%"
                    lines.append(line)
        else:
            lines.append("Найдено в каталоге: ничего.")

        if missing:
            lines.append("")
            lines.append("Не нашёл в каталоге:")
            lines.append(", ".join(missing))

        lines.append("")
        lines.append("Открой карточки БАДов кнопками ниже, чтобы посмотреть детали и добавить в стек.")

        buttons = []
        for item in all_found_items:
            row = item["row"]
            buttons.append([InlineKeyboardButton(f"🔎 {row['name']}", callback_data=f"supp:openai:{row['id']}")])
        initial_ids = await db.get_user_initial_stack_ids(conn, update.effective_user.id)
        if initial_ids:
            buttons.append([InlineKeyboardButton("✅ Завершить подбор", callback_data="onboard:finish")])
        else:
            buttons.append([InlineKeyboardButton("⏭️ Пропустить", callback_data="onboard:skip")])
        buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="tz:back")])
        if save_to_onboarding:
            await db.save_onboarding_results(
                conn,
                update.effective_user.id,
                goal_text=goal_text,
                raw_json=raw or "",
                prefix_line=prefix_line or "",
            )
        await show(
            update,
            context,
            "\n".join(lines),
            InlineKeyboardMarkup(buttons),
            route="onboard:results",
            payload={
                "onboard_results": True,
                "goal_text": goal_text,
                "raw": raw or "",
                "prefix_line": prefix_line or "",
            },
        )
        return True

    await show(update, context, "Начальный подбор временно недоступен. Попробуй ещё раз.", back(), route="onboard:goal")
    return False


async def _render_onboarding_results(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    *,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> bool:
    state = await db.get_onboarding_state(conn, update.effective_user.id)
    status = str(state.get("onboarding_status") or "")
    raw = str(state.get("onboarding_raw_json") or "")
    if status not in {"active", "results"} or not raw:
        return False
    return await _render_ai_suggestions(
        update,
        context,
        conn,
        str(state.get("onboarding_goal_text") or "текущая цель"),
        raw,
        prefix_line=str(state.get("onboarding_prefix_line") or "") or None,
        save_to_onboarding=False,
    )


async def _show_ai_used_upsell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = [
        "✨ <b>Стартовый подбор уже использован</b>",
        "",
        "Ты уже получил бесплатные стартовые рекомендации. Дальше можно пользоваться каталогом бесплатно или открыть Premium для AI-сценариев и WebApp.",
    ]
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⭐ Premium", callback_data="menu:premium")],
            [InlineKeyboardButton("💊 Мои БАДы", callback_data="menu:my")],
            [InlineKeyboardButton("📚 Каталог", callback_data="menu:catalog")],
            [InlineKeyboardButton("🏠 Меню", callback_data="menu:home")],
        ]
    )
    await show(update, context, "\n".join(lines), kb, route="menu:ai")

def _truncate_text(value: str | None, limit: int = 180) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit - 3]}..."

def _normalize_ai_checkin_extract(raw: dict, transcription: str) -> dict:
    sleep_range = raw.get("sleep_range")
    if sleep_range not in SLEEP_OPTIONS and sleep_range != "unknown":
        sleep_range = "unknown"

    energy = raw.get("energy")
    if not isinstance(energy, int) or energy not in {1, 2, 3, 4}:
        energy = None

    stress = raw.get("stress")
    if not isinstance(stress, int) or stress not in {1, 2, 3}:
        stress = None

    training_today = raw.get("training_today")
    if not isinstance(training_today, bool):
        training_today = None

    raw_goals = raw.get("goals") if isinstance(raw.get("goals"), list) else []
    goal_map = {
        "focus": "focus",
        "energy": "energy",
        "sleep": "sleep",
        "gut": "gut",
        "mood": "mood",
        "recovery": "recovery",
        "calm": "calm",
        "workout": "training",
        "training": "training",
    }
    allowed_goals = {code for code, _ in GOAL_OPTIONS}
    goals: list[str] = []
    for item in raw_goals:
        if not isinstance(item, str):
            continue
        mapped = goal_map.get(item.strip().lower())
        if mapped and mapped in allowed_goals and mapped not in goals:
            goals.append(mapped)

    note_text = str(raw.get("note_text") or "").strip()
    if not note_text:
        note_text = transcription.strip()
    note_text = _truncate_text(note_text, 250)

    return {
        "sleep_range": sleep_range,
        "energy": energy,
        "stress": stress,
        "training": training_today,
        "goals": goals,
        "note": note_text,
    }

async def _finalize_ai_checkin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    *,
    tg_uid: int,
    user_id: int,
    tz_name: str,
    state: dict,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    target_day = str(state.get("target_day") or "today")
    if target_day not in {"today", "tomorrow"}:
        target_day = "today"
    state["target_day"] = target_day
    day = await db.ensure_daily_rollover(conn, tg_uid)
    if target_day == "tomorrow":
        try:
            day = (datetime.strptime(day, "%Y-%m-%d").date() + timedelta(days=1)).isoformat()
        except ValueError:
            pass
    answers = state.get("answers", {}) if isinstance(state.get("answers"), dict) else {}
    goals = list(answers.get("goals") or [])
    goals_ru = [_goal_label(goal) for goal in goals]
    checkin_payload = {
        "sleep_range": answers.get("sleep_range"),
        "energy": answers.get("energy"),
        "stress": answers.get("stress"),
        "training": answers.get("training"),
        "goals": goals,
        "goals_ru": goals_ru,
        "note": _truncate_text(answers.get("note")),
        "date_local": day,
        "target_day": state.get("target_day") or "today",
    }
    await db.upsert_daily_checkin(
        conn,
        tg_uid,
        day,
        {
            "sleep_range": answers.get("sleep_range"),
            "energy": answers.get("energy"),
            "stress": answers.get("stress"),
            "training": answers.get("training"),
            "goals": goals,
            "note": _truncate_text(answers.get("note"), 500),
        },
    )

    s = context.application.bot_data["settings"]
    if not s.OPENAI_API_KEY:
        await show(
            update,
            context,
            "AI отключен: добавь OPENAI_API_KEY в .env.",
            back(),
            route=AI_CHECKIN_ROUTE,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return

    stack = await db.get_user_base_stack(conn, user_id)
    supplement_ids = [int(item.get("supplement_id") or 0) for item in stack if item.get("supplement_id")]
    days_range = []
    try:
        day_value = datetime.strptime(day, "%Y-%m-%d").date()
        days_range = [(day_value - timedelta(days=delta)).isoformat() for delta in range(6, -1, -1)]
    except ValueError:
        days_range = [day]
    status_rows = []
    if supplement_ids:
        status_rows = await db.list_supplement_day_states(
            conn,
            tg_uid,
            supplement_ids,
            days_range[0],
            days_range[-1],
        )
    adherence_payload = _build_adherence_payload(stack, status_rows, days_range)
    target_day_status_rows = []
    if supplement_ids:
        target_day_status_rows = await db.list_supplement_day_states(conn, tg_uid, supplement_ids, day, day)
    target_day_state_payload = []
    locked_taken_slugs: set[str] = set()
    supplement_slug_by_id: dict[int, str] = {}
    for item in stack:
        supplement_id = int(item.get("supplement_id") or 0)
        slug = str(item.get("slug") or "").strip()
        if supplement_id and slug:
            supplement_slug_by_id[supplement_id] = slug
    for row in target_day_status_rows:
        supplement_id = int(row["supplement_id"] or 0)
        status = str(row["status"] or "").strip().lower()
        slug = supplement_slug_by_id.get(supplement_id, "")
        if not slug:
            continue
        target_day_state_payload.append({"slug": slug, "status": status})
        if status == "taken":
            locked_taken_slugs.add(slug)
    start_14d = days_range[0]
    end_14d = days_range[-1]
    recent_checkins_rows = await db.list_recent_checkins_window(conn, user_id, start_14d, end_14d, limit=14)
    ai_plan_history_rows = await db.list_day_plan_history(conn, user_id, start_14d, end_14d, limit=120)

    recent_checkins_payload = []
    for row in recent_checkins_rows:
        raw_answers = str(row["answers_json"] or "{}").strip()
        try:
            answers = json.loads(raw_answers) if raw_answers else {}
        except json.JSONDecodeError:
            answers = {}
        recent_checkins_payload.append({"date_local": str(row["date_local"]), "answers": answers if isinstance(answers, dict) else {}})

    ai_plan_history_payload = []
    for row in ai_plan_history_rows:
        ai_plan_history_payload.append(
            {
                "date_local": str(row["date_local"]),
                "supplement_id": int(row["supplement_id"]),
                "slug": str(row["slug"] or ""),
                "ru_name": str(row["name_ru"] or row["name_en"] or row["name"] or ""),
                "action": str(row["action"] or ""),
                "dose_text": str(row["dose_text"] or ""),
                "time_hint": str(row["time_hint"] or ""),
                "status": str(row["status"] or ""),
            }
        )

    inferred_goals = db.infer_goals_from_text(str(answers.get("note") or ""))
    merged_goals: list[str] = []
    for goal in goals + inferred_goals:
        if goal and goal not in merged_goals:
            merged_goals.append(goal)
    candidates = await db.get_catalog_candidates(conn, merged_goals, limit=60)
    messages = build_ai_checkin_prompt(
        {"tz_name": tz_name},
        stack,
        checkin_payload,
        candidates,
        adherence_payload,
        recent_checkins_payload,
        ai_plan_history_payload,
        target_day_state_payload,
    )
    cfg = OpenAIConfig(api_key=s.OPENAI_API_KEY, model=s.OPENAI_MODEL)
    await show(
        update,
        context,
        "🤖 Думаю… (AI)",
        None,
        route=AI_CHECKIN_ROUTE,
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )
    raw = None
    try:
        raw = await ai_checkin_json(cfg, messages, max_output_tokens=900, timeout_s=30)
    except Exception:
        log.exception("OpenAI checkin request failed")
        await show(
            update,
            context,
            "AI временно недоступен.",
            back(),
            route=AI_CHECKIN_ROUTE,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return

    data = _parse_ai_json(raw or "")
    if not isinstance(data, dict):
        await show(
            update,
            context,
            "AI вернул неожиданный формат. Попробуй ещё раз.",
            back(),
            route=AI_CHECKIN_ROUTE,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return

    stack_slugs = {str(item.get("slug") or "").strip() for item in stack}
    stack_slugs = {slug for slug in stack_slugs if slug}
    candidate_slugs = {str(item.get("slug") or "").strip() for item in candidates}
    candidate_slugs = {slug for slug in candidate_slugs if slug}
    actions = data.get("actions") if isinstance(data.get("actions"), list) else []
    clean_actions = []
    seen_action_keys: set[tuple[str, str]] = set()
    missing_count = 0
    for idx, action in enumerate(actions, start=1):
        if len(clean_actions) >= 6:
            break
        if not isinstance(action, dict):
            continue
        action_type = str(action.get("type") or "").strip()
        if action_type not in {"add", "remove", "adjust"}:
            continue
        raw_slug = str(action.get("slug") or "").strip()
        raw_name = str(action.get("ru_name") or action.get("name_ru") or action.get("name_en") or "").strip()
        if not raw_slug and not raw_name:
            missing_count += 1
            continue
        row = await db.find_supplement_by_slug_or_name(conn, raw_slug or None, raw_name or None)
        if not row:
            missing_count += 1
            continue
        slug = str(row["slug"] or "").strip()
        if not slug:
            missing_count += 1
            continue
        is_valid_action = False
        if action_type == "add":
            is_valid_action = slug not in stack_slugs
        elif action_type in {"remove", "adjust"}:
            is_valid_action = slug in stack_slugs
            if is_valid_action and slug in locked_taken_slugs:
                is_valid_action = False
        if not is_valid_action:
            missing_count += 1
            continue
        dedup_key = (action_type, slug)
        if dedup_key in seen_action_keys:
            continue
        ru_name = (row["name_ru"] or row["name_en"] or row["name"] or "").strip()
        action_id = str(action.get("id") or f"a{idx}").strip()
        confidence = action.get("confidence")
        if not isinstance(confidence, (float, int)) or confidence < 0 or confidence > 1:
            confidence = None
        clean_actions.append(
            {
                "id": action_id,
                "type": action_type,
                "slug": slug,
                "supplement_id": int(row["id"]),
                "ru_name": ru_name,
                "dose_text": _truncate_text(action.get("dose_text"), 80),
                "suggested_time": db._normalize_hhmm(str(action.get("suggested_time") or "").strip()),
                "prefer_existing_time": True,
                "time_hint": _truncate_text(action.get("time_hint"), 60),
                "confidence": confidence,
                "reason_short_ru": _truncate_text(action.get("reason_short_ru"), 200),
                "safety_ru": _truncate_text(action.get("safety_ru"), 160),
            }
        )
        seen_action_keys.add(dedup_key)

    payload = {
        "actions": clean_actions,
        "summary_ru": _truncate_text(data.get("summary_ru"), 600) or "Короткое резюме недоступно.",
        "disclaimer_ru": _truncate_text(data.get("disclaimer_ru"), 300),
    }
    if not any(str(action.get("type") or "") == "add" for action in clean_actions):
        now_hhmm = db.local_now(tz_name or None).strftime("%H:00")
        fallback_adds = await db.build_goal_based_fallback_adds(
            conn,
            base_stack_slugs=stack_slugs,
            goals=merged_goals,
            note_text=str(answers.get("note") or ""),
            now_hhmm=now_hhmm,
            limit=2,
        )
        for item in fallback_adds:
            if len(clean_actions) >= 6:
                break
            dedup_key = ("add", str(item.get("slug") or ""))
            if dedup_key in seen_action_keys:
                continue
            clean_actions.append(
                {
                    "id": f"fb_{len(clean_actions) + 1}",
                    "type": "add",
                    "slug": str(item.get("slug") or ""),
                    "supplement_id": int(item.get("supplement_id") or 0),
                    "ru_name": str(item.get("name_ru") or ""),
                    "dose_text": "",
                    "suggested_time": str(item.get("suggested_time") or "12:00"),
                    "prefer_existing_time": True,
                    "time_hint": "",
                    "confidence": 0.55,
                    "reason_short_ru": str(item.get("reason") or "Подобрано fallback по цели из чек-ина"),
                    "safety_ru": "Начинай с базовой дозы и оцени самочувствие.",
                }
            )
            seen_action_keys.add(dedup_key)
        payload["actions"] = clean_actions
    state["last_ai_actions_json"] = json.dumps(payload, ensure_ascii=False)
    state["selected_action_ids"] = []
    state["last_date_local"] = day
    _set_ai_checkin_state(context, state)
    await _render_ai_checkin_results(
        update,
        context,
        date_local=day,
        payload=payload,
        missing_count=missing_count,
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )

async def _show_today_plan(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    tg_uid: int,
) -> None:
    user_id = await db.get_or_create_user(conn, tg_uid)
    day = await db.ensure_daily_rollover(conn, tg_uid)
    stack_rows = await db.list_user_stack_details(conn, tg_uid, local_date=day)
    day_items = await db.list_day_plan_items(conn, user_id, day)

    lines = [f"📅 <b>План дня</b> ({_format_day_ru(day)})", ""]
    if stack_rows:
        lines.append("Базовый стек:")
        for idx, row in enumerate(stack_rows, start=1):
            dose = row["dose_text"] or ""
            timing = row["time_hhmm"] or ""
            bits = []
            if dose:
                bits.append(f"доза: {dose}")
            if timing:
                bits.append(f"время: {timing}")
            detail = " · ".join(bits)
            line = f"{idx}) <b>{row['name']}</b>"
            if detail:
                line = f"{line} — {detail}"
            lines.append(line)
    else:
        lines.append("Базовый стек: пусто.")

    if day_items:
        lines.append("")
        lines.append("AI-изменения на сегодня:")
        for idx, item in enumerate(day_items, start=1):
            ru_name = item.get("name_ru") or item.get("name_en") or item.get("name") or "добавка"
            action_type = item.get("action")
            type_map = {"add": "ДОБАВИТЬ", "remove": "УБРАТЬ", "adjust": "ИЗМЕНИТЬ"}
            label = type_map.get(action_type, "ИЗМЕНИТЬ")
            details = []
            dose_text = str(item.get("dose_text") or "").strip()
            time_hint = str(item.get("time_hint") or "").strip()
            if dose_text:
                details.append(dose_text)
            if time_hint:
                details.append(time_hint)
            detail_text = f" — {', '.join(details)}" if details else ""
            lines.append(f"{idx}) {label}: <b>{ru_name}</b>{detail_text}")
    else:
        lines.append("")
        lines.append("AI-изменения на сегодня: нет.")

    lines.append("")
    lines.append("Это план только на сегодня. Завтра вернётся базовый стек, если не изменишь вручную.")

    await show(
        update,
        context,
        "\n".join(lines),
        InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="menu:home")]]),
        route="menu:todayplan",
    )

async def home(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    user_id = update.effective_user.id if update.effective_user else 0
    is_admin = (await _get_role_perms_ctx(context, user_id))[0] != db.ROLE_NONE
    show_complexes = False
    onboarding_state = {}
    starter_active = False
    ai_checkin_access = False
    show_next_step_hint = True
    if user_id:
        settings = context.application.bot_data["settings"]
        conn = await db.connect(settings.DB_PATH)
        try:
            if user_id in settings.ADMIN_IDS:
                show_complexes = True
            else:
                show_complexes = await db.has_any_webapp_access(conn, user_id)
            onboarding_state = await db.get_onboarding_state(conn, user_id)
            starter_active = await db.is_starter_trial_active(conn, user_id)
            ai_checkin_access = (user_id in settings.ADMIN_IDS) or await db.has_any_webapp_access(conn, user_id)
            created_row = await db.fetchone(conn, "SELECT created_at_utc FROM users WHERE tg_user_id=?", (int(user_id),))
            created_at_utc = str(created_row["created_at_utc"] if created_row and "created_at_utc" in created_row.keys() else "").strip()
            created_dt = db._parse_utc_iso_dt(created_at_utc)
            if created_dt is not None:
                show_next_step_hint = (datetime.now(timezone.utc) - created_dt) <= timedelta(days=2)
        finally:
            await conn.close()

    status = str(onboarding_state.get("onboarding_status") or "")
    raw = str(onboarding_state.get("onboarding_raw_json") or "")
    used = int(onboarding_state.get("onboarding_free_pick_used") or 0)
    has_unfinished = status in {"active", "results"} and bool(raw)
    entry_reason = str(context.user_data.pop("home_entry_reason", "") or "")
    entry_extra = str(context.user_data.pop("home_entry_extra", "") or "")

    text = _home_text(
        has_unfinished_onboarding=has_unfinished,
        onboarding_done=(used == 1 and status == "done"),
        starter_active=starter_active,
        entry_reason=entry_reason,
        show_next_step_hint=show_next_step_hint,
        entry_extra=entry_extra,
    )

    base_menu = main_menu(
        is_admin=is_admin,
        show_complexes=show_complexes,
        show_ai_checkin=ai_checkin_access,
    )
    rows = [list(row) for row in base_menu.inline_keyboard]
    if has_unfinished:
        rows.insert(3, [InlineKeyboardButton("✨ Продолжить подбор", callback_data="onboard:results")])

    await show(
        update, context,
        text,
        InlineKeyboardMarkup(rows),
        route="menu:home",
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )

def _stars_amount_for_plan(s, plan_id: str) -> int:
    price_minor = int(s.PREMIUM_PRICES_RUB.get(plan_id, 0) or 0)
    if price_minor <= 0:
        return 0
    rub = round(price_minor / 100)
    return max(1, round(rub / 1.5))


def _premium_plans_payload(s) -> list[dict]:
    labels = {"m1": "1 месяц", "m3": "3 месяца", "m6": "6 месяцев", "m12": "12 месяцев"}
    plans: list[dict] = []
    for plan_id, months in s.PREMIUM_PLANS.items():
        rub_kop = int(s.PREMIUM_PRICES_RUB.get(plan_id, 0) or 0)
        stars = _stars_amount_for_plan(s, plan_id)
        period = labels.get(plan_id, f"{int(months)} мес.")
        rub_title = f"{period} · {rub_kop / 100:.0f} ₽" if rub_kop > 0 else period
        stars_title = f"{period} · {stars} ⭐" if stars > 0 else period
        plans.append({
            "plan_id": plan_id,
            "months": int(months),
            "title": rub_title,
            "stars_title": stars_title,
            "rub": rub_kop,
            "stars": stars,
        })
    return plans




async def _show_complexes_hub(update: Update, context: ContextTypes.DEFAULT_TYPE, conn, tg_uid: int, page: int = 0, *, target_chat_id: int | None = None, target_message_id: int | None = None) -> None:
    active = tg_uid in context.application.bot_data["settings"].ADMIN_IDS or await db.has_any_webapp_access(conn, tg_uid)
    if not active:
        await show(update, context, "Комплексы доступны при активном Premium или Starter.", premium_upsell_kb(), route="menu:premium", target_chat_id=target_chat_id, target_message_id=target_message_id)
        return
    back_cb = str(context.user_data.get(COMPLEXES_BACK_ROUTE_KEY) or "menu:settings")
    items = await db.list_complexes(conn, tg_uid)
    page = min(max(0, int(page)), max(0, (len(items)-1)//10))
    text = "🧩 <b>Комплексы</b>\n\nСоздавай, наполняй и настраивай расписание прямо в боте."
    if len(items) > 10:
        text += f"\n\nСтраница {page+1}/{(len(items)+9)//10} · Комплексов: {len(items)}"
    await show(update, context, text, complexes_hub_kb(items, back_cb=back_cb, page=page), route=f"cx:list:{page}", target_chat_id=target_chat_id, target_message_id=target_message_id)


async def _show_complex_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, conn, tg_uid: int, complex_id: int, page: int = 0, *, target_chat_id: int | None = None, target_message_id: int | None = None) -> None:
    items = await db.list_complexes(conn, tg_uid)
    target = next((x for x in items if int(x.get("id") or 0) == int(complex_id)), None)
    if not target:
        await _show_complexes_hub(update, context, conn, tg_uid, target_chat_id=target_chat_id, target_message_id=target_message_id)
        return
    c_items = await db.list_complex_items(conn, tg_uid, int(complex_id))
    schedules = [x for x in await db.list_complex_schedules(conn, tg_uid) if int(x.get("complex_id") or 0) == int(complex_id)]
    created_at, duration = _format_start_date_and_duration(target.get("created_at_utc"))
    lines = [
        f"🧩 <b>{html.escape(str(target.get('name') or 'Комплекс'))}</b>",
        f"Позиций: {len(c_items)}",
        f"Дата начала: {created_at}",
        f"Уже принимается: {duration}",
    ]
    if schedules:
        sched_txt = ", ".join([f"{str(x.get('slot_id') or '')}:{str(x.get('time_local') or '--:--')}" for x in schedules])
        lines.append(f"Расписание: {sched_txt}")
    else:
        lines.append("Расписание: не назначено")
    if c_items:
        lines.append("")
        lines.append("Состав:")
    entries = [{"title": f"{idx}) {item.get('name_ru') or item.get('slug') or item.get('supplement_id')}", "reason_ru": f"доза: {item['dose_override']}" if item.get('dose_override') else "", "supplement_id": item['supplement_id'], "label": str(item.get('name_ru') or item.get('slug') or item['supplement_id'])} for idx, item in enumerate(c_items, start=1)]
    pages = _compatibility_pages(entries)
    page = min(max(0, int(page)), len(pages) - 1)
    lines.extend(entry['text'] for entry in pages[page])
    page_items = {entry['item']['supplement_id']: entry['item'] for entry in pages[page]}
    kb_rows = [[InlineKeyboardButton(f"⚙️ {item['label'][:100]}", callback_data=f"cx:item:open:{int(complex_id)}:{int(sid)}")] for sid, item in page_items.items()]
    if len(pages) > 1:
        lines.extend(["", f"Страница {page + 1}/{len(pages)}"])
        navigation = []
        if page > 0:
            navigation.append(InlineKeyboardButton("⬅️", callback_data=f"cx:open:{int(complex_id)}:{page-1}"))
        if page + 1 < len(pages):
            navigation.append(InlineKeyboardButton("➡️", callback_data=f"cx:open:{int(complex_id)}:{page+1}"))
        kb_rows.append(navigation)
    kb_rows.extend(complex_open_kb(int(complex_id), list_back_cb="cx:list").inline_keyboard)
    await show(update, context, "\n".join(lines), InlineKeyboardMarkup(kb_rows), route=f"cx:open:{int(complex_id)}:{page}", target_chat_id=target_chat_id, target_message_id=target_message_id)


async def _show_complex_item(update: Update, context: ContextTypes.DEFAULT_TYPE, conn, tg_uid: int, complex_id: int, supplement_id: int, *, target_chat_id: int | None = None, target_message_id: int | None = None) -> None:
    items = await db.list_complex_items(conn, tg_uid, complex_id)
    item = next((x for x in items if int(x.get("supplement_id") or 0) == supplement_id), None)
    if not item:
        await _show_complex_detail(update, context, conn, tg_uid, complex_id, target_chat_id=target_chat_id, target_message_id=target_message_id)
        return
    title = html.escape(str(item.get("name_ru") or item.get("slug") or supplement_id))
    dose = html.escape(str(item.get("dose_override") or "").strip() or "не задана")
    await show(update, context, f"БАД: <b>{title}</b>\nДоза: {dose}", complex_item_actions_kb(complex_id, supplement_id), route=f"cx:item:open:{complex_id}:{supplement_id}", target_chat_id=target_chat_id, target_message_id=target_message_id)


async def _show_premium_hub(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    back_cb: str = "menu:home",
    route: str = "menu:premium",
) -> None:
    s = context.application.bot_data["settings"]
    plans = _premium_plans_payload(s)
    text = (
        "⭐ <b>Premium</b>\n\n"
        "С Premium открывается полный WebApp: удобный Today, карта органов, AI check-in и гибкая настройка напоминаний.\n\n"
        "Выберите срок — кнопка сразу откроет оплату в WebApp."
    )
    await show(update, context, text, premium_plans_kb(plans, s.WEBAPP_URL, back_cb=back_cb), route=route)

async def _show_starter_trial_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = [
        "🎁 <b>3 дня полного доступа</b>",
        "",
        "Ты уже получил стартовый подбор.",
        "",
        "Теперь можно 3 дня бесплатно пользоваться WebApp: отмечать приём, смотреть карту, настраивать стек и проверить, как это ощущается в жизни.",
    ]
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎁 Активировать 3 дня", callback_data="starter:activate")],
            [InlineKeyboardButton("📚 Остаться в боте", callback_data="starter:stay")],
            [InlineKeyboardButton("⭐ Что даёт Premium", callback_data="starter:premium_about")],
        ]
    )
    await show(update, context, "\n".join(lines), kb, route="starter:offer")


async def _complete_onboarding_flow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    tg_uid: int,
) -> None:
    context.user_data.pop("initial_pick_back_to_tz", None)
    context.user_data.pop("onboarding", None)
    await db.mark_onboarding_done(conn, tg_uid)
    await db.mark_onboarding_used(conn, tg_uid)
    has_premium = await db.is_premium_active(conn, tg_uid)
    starter_used_row = await db.fetchone(
        conn,
        "SELECT starter_trial_used FROM users WHERE tg_user_id=?",
        (int(tg_uid),),
    )
    starter_used = int(starter_used_row["starter_trial_used"] or 0) if starter_used_row else 0
    if not has_premium and starter_used == 0:
        await _show_starter_trial_offer(update, context)
        return
    context.user_data["home_entry_reason"] = "onboarding_completed"
    await home(update, context)


async def _has_ai_checkin_access(context: ContextTypes.DEFAULT_TYPE, tg_uid: int) -> bool:
    if tg_uid <= 0:
        return False
    s = context.application.bot_data["settings"]
    if tg_uid in s.ADMIN_IDS:
        return True
    conn = await db.connect(s.DB_PATH)
    try:
        return await db.has_any_webapp_access(conn, tg_uid)
    finally:
        await conn.close()


async def require_premium_or_upsell(update: Update, context: ContextTypes.DEFAULT_TYPE, feature_name: str) -> bool:
    tg_uid = update.effective_user.id if update.effective_user else 0
    active = await _has_ai_checkin_access(context, tg_uid)
    if active:
        return True
    await show(
        update,
        context,
        f"🔒 <b>{feature_name}</b> доступно только с активным доступом (Premium или Starter).",
        premium_upsell_kb(),
        route="menu:premium_required",
    )
    return False




def build_webapp_payment_url(base_url: str, plan_id: str) -> str:
    clean = str(base_url or "").rstrip("/")
    return f"{clean}/?screen=payment&kind=subscription&plan_id={plan_id}"


def build_webapp_entry_url(base_url: str, source: str = "bot", tutorial: str = "") -> str:
    clean = str(base_url or "").rstrip("/")
    if not clean:
        return ""
    params = [f"source={source}"]
    if tutorial:
        params.append(f"tutorial={tutorial}")
    return f"{clean}/?{'&'.join(params)}"

async def _send_subscription_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE, plan_id: str, method: str) -> None:
    s = context.application.bot_data["settings"]
    chat_id = update.effective_chat.id if update.effective_chat else (update.effective_user.id if update.effective_user else 0)
    if plan_id not in s.PREMIUM_PLANS:
        await show(update, context, "Неизвестный тариф.", premium_upsell_kb(), route="menu:premium")
        return
    nonce = int(time.time())
    payload = f"sub:{plan_id}:{method}:{nonce}"
    if method == "stars":
        if not s.BILLING_ENABLED_STARS:
            await show(update, context, "Оплата Stars сейчас недоступна.", premium_upsell_kb(), route="menu:premium")
            return
        amount = _stars_amount_for_plan(s, plan_id)
        await context.bot.send_invoice(
            chat_id=chat_id,
            title="Premium подписка",
            description="Доступ к WebApp + AI + расширенным функциям",
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label="Premium", amount=amount)],
            need_shipping_address=False,
            is_flexible=False,
        )
        return
    if method == "yookassa":
        if not s.BILLING_ENABLED_RUB:
            await show(update, context, "Оплата картой временно недоступна.", premium_upsell_kb(), route="menu:premium")
            return
        if not s.WEBAPP_URL:
            await show(update, context, "WEBAPP_URL не настроен. Обратитесь в поддержку.", premium_upsell_kb(), route="menu:premium")
            return
        payment_url = build_webapp_payment_url(s.WEBAPP_URL, plan_id)
        await show(
            update,
            context,
            "Оплата в рублях откроется в WebApp, а затем переведёт на безопасную внешнюю страницу YooKassa.",
            premium_webapp_checkout_kb(payment_url),
            route="menu:premium",
        )
        return
    await show(update, context, "Неизвестный метод оплаты.", premium_upsell_kb(), route="menu:premium")


async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard_not_banned(update, context):
        return
    await _show_premium_hub(update, context)


async def on_precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard_not_banned(update, context):
        return
    query = update.pre_checkout_query
    if not query:
        return

    payload = str(query.invoice_payload or "")
    parts = payload.split(":")
    if len(parts) < 4 or parts[0] != "sub":
        await query.answer(ok=False, error_message="Некорректный payload")
        return

    plan_id = str(parts[1] or "")
    method = str(parts[2] or "").lower()
    s = context.application.bot_data["settings"]
    if plan_id not in s.PREMIUM_PLANS or method not in {"stars", "yookassa"}:
        await query.answer(ok=False, error_message="Некорректный тариф")
        return

    if method == "stars":
        expected_amount = _stars_amount_for_plan(s, plan_id)
        if str(query.currency or "") != "XTR" or int(query.total_amount or 0) != expected_amount:
            await query.answer(ok=False, error_message="Сумма или валюта не совпадает")
            return
    else:
        expected_amount = int(s.PREMIUM_PRICES_RUB.get(plan_id, 0) or 0)
        if str(query.currency or "") != "RUB" or int(query.total_amount or 0) != expected_amount:
            await query.answer(ok=False, error_message="Сумма или валюта не совпадает")
            return

    await query.answer(ok=True)


async def on_success_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard_not_banned(update, context):
        return
    msg = update.effective_message
    if not msg or not msg.successful_payment or not update.effective_user:
        return
    sp = msg.successful_payment
    payload = str(sp.invoice_payload or "")
    parts = payload.split(":")
    if len(parts) < 4 or parts[0] != "sub":
        return
    plan_id = parts[1]
    method = parts[2]
    s = context.application.bot_data["settings"]
    months = int(s.PREMIUM_PLANS.get(plan_id, 0) or 0)
    if months <= 0:
        await msg.reply_text("Не удалось определить тариф для активации.")
        return
    conn = await db.connect(s.DB_PATH)
    starter_active_before_purchase = False
    try:
        starter_active_before_purchase = await db.is_starter_trial_active(conn, update.effective_user.id)
        is_new, until_iso = await db.activate_subscription_payment(
            conn,
            update.effective_user.id,
            months,
            plan_id=plan_id,
            project_key=s.PROJECT_KEY,
            payment_kind="subscription",
            finalized_at_utc=db.utc_now_iso(),
            provider=method,
            currency=str(sp.currency or ""),
            amount=int(sp.total_amount or 0),
            payload=payload,
            telegram_payment_charge_id=sp.telegram_payment_charge_id,
            provider_payment_charge_id=sp.provider_payment_charge_id,
        )
        if not is_new:
            await msg.reply_text("Оплата уже учтена ✅")
            return
    finally:
        await conn.close()

    if starter_active_before_purchase:
        await msg.reply_text(
            "Оплата прошла успешно! Premium активирован сразу, "
            "а оставшиеся дни Starter добавлены к сроку.\n"
            f"Подписка активна до: {until_iso}"
        )
    else:
        await msg.reply_text(
            "Оплата прошла успешно!\n"
            f"Premium активен до: {until_iso}"
        )
    context.user_data["home_entry_reason"] = "premium_activated"
    await home(update, context)


def _home_text(
    *,
    has_unfinished_onboarding: bool = False,
    onboarding_done: bool = False,
    starter_active: bool = False,
    entry_reason: str = "",
    show_next_step_hint: bool = True,
    entry_extra: str = "",
) -> str:
    next_step = "Начни со стартового подбора или открой каталог."
    if has_unfinished_onboarding:
        next_step = "Продолжи стартовый подбор и добавь подходящие БАДы в свой список."
    elif onboarding_done:
        next_step = "Дальше: добавь БАДы в свой список и настрой время приёма."
    if starter_active:
        next_step = "Открой WebApp — там удобнее отмечать приём и смотреть план."
    contextual_lines: list[str] = []
    if entry_reason == "onboarding_completed":
        contextual_lines = [
            "",
            "✅ <b>Начальный подбор завершён.</b>",
            "Что можно делать дальше в бесплатной версии:",
            "• открыть «Каталог» и изучать карточки добавок;",
            "• перейти в «Мои БАДы», собрать стек и настроить напоминания;",
            "• продолжать отмечать приём и отслеживать прогресс в боте.",
        ]
    elif entry_reason == "premium_activated":
        contextual_lines = [
            "",
            "🎉 <b>Premium активирован.</b>",
            "Теперь тебе доступно:",
            "• полное веб-приложение с расширенным трекингом и планом на день;",
            "• ИИ-опрос и персональные подсказки;",
            "• расширенные функции подписки и автоматизация.",
        ]
    next_step_block = f"Следующий шаг: {next_step}" if show_next_step_hint else "Открой нужный раздел в меню ниже."
    return (
        "💊 <b>Supply Bot</b>\n\n"
        "• Каталог: быстро найди добавку и проверь совместимость.\n"
        "• Мои БАДы: собери свой список и настрой напоминания.\n"
        "• ИИ и веб-приложение: стартовый подбор и удобный ежедневный трекинг.\n\n"
        f"{next_step_block}"
        + ("\n".join(contextual_lines) if contextual_lines else "")
        + (f"\n\n{entry_extra}" if entry_extra else "")
    )


def _subscription_expired_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💳 Перейти к оплате", callback_data="premium:expired:pay")],
            [InlineKeyboardButton("🏠 Вернуться в меню", callback_data="menu:home")],
        ]
    )


def _subscription_expired_text() -> str:
    return (
        "⚠️ <b>Ваш доступ закончился</b>\n\n"
        "Пробный или платный доступ больше не активен.\n\n"
        "Что остаётся в базовой версии:\n"
        "• Каталог добавок и карточки;\n"
        "• Мои БАДы и ручные отметки;\n"
        "• Базовые разделы меню.\n\n"
        "Что временно недоступно без подписки:\n"
        "• ежедневный ИИ-опрос;\n"
        "• комплексы;\n"
        "• расширенные автоматические напоминания и Premium-функции.\n\n"
        "Продлите подписку, чтобы снова включить полный режим в веб-приложении.\n\n"
        "Бот и каталог внутри бота продолжают работать без подписки."
    )

def _initial_pick_prompt_text(prefix: str | None = None) -> str:
    base = (
        "✨ <b>Начальный подбор</b>\n\n"
        "Опиши, что хочешь получить или изменить. Можно рассказать о целях, самочувствии, режиме дня, "
        "ограничениях или о том, что уже пробовал — не ограничивай фантазию.\n"
        "Пример: «Хочу больше энергии утром и спокойнее засыпать, но без стимуляторов».\n\n"
        "Чем подробнее опишешь запрос, тем точнее будет подбор: я предложу список добавок с примерными "
        "дозировками и временем приёма для стартового стека.\n\n"
        "Можно написать текстом или отправить голосовое сообщение.\n\n"
        "<i>Обычно это занимает 1–3 минуты.</i>\n\n"
        "<i>Не медицинский совет.</i>"
    )
    if prefix:
        return f"{prefix}\n\n{base}"
    return base

async def _start_initial_pick(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    prefix: str | None = None,
    route: str = "onboard:goal",
    back_cb: str = "menu:home",
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    context.user_data[WAITING_KEY] = "initial_pick_goal"
    context.user_data["initial_pick_back_to_tz"] = back_cb == "tz:back"
    buttons = [[InlineKeyboardButton("⬅️ Назад", callback_data=back_cb)]]
    if back_cb == "tz:back":
        buttons.insert(0, [InlineKeyboardButton("⏭️ Пропустить", callback_data="onboard:skip")])
    await show(
        update,
        context,
        _initial_pick_prompt_text(prefix),
        InlineKeyboardMarkup(buttons),
        route=route,
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )


async def _render_consent_screen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    s = context.application.bot_data["settings"]
    text = (
        "🔒 <b>Перед началом нужно согласие</b>\n\n"
        "Продолжая, ты подтверждаешь согласие с офертой и политикой конфиденциальности."
    )
    await show(
        update,
        context,
        text,
        consent_kb(s.OFFER_URL, s.PRIVACY_URL),
        route="consent:screen",
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )


async def _continue_after_consent(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    tg_uid: int,
) -> None:
    tz = await db.get_user_tz(conn, tg_uid)
    if _is_tz_unset(tz):
        context.user_data.pop("tz_back_data", None)
        context.user_data["onboarding"] = "tz"
        await _show_timezone_picker(
            update,
            context,
            0,
            message_text=(
                "Выбери свой часовой пояс, чтобы уведомления приходили вовремя.\n"
                "Пример: Москва — UTC+03."
            ),
        )
        return
    await home(update, context)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard_not_banned(update, context):
        return
    s = context.application.bot_data["settings"]
    tg_uid = update.effective_user.id
    conn = await db.connect(s.DB_PATH)
    try:
        await db.get_or_create_user(conn, tg_uid, tz_name="")
        start_payload = str((context.args or [""])[0] or "").strip().lower()
        has_consent = await db.has_required_consent(
            conn,
            tg_uid,
            offer_version=s.OFFER_VERSION,
            privacy_version=s.PRIVACY_VERSION,
        )
        if not has_consent:
            await _render_consent_screen(update, context)
            return
        if start_payload in {"webapp_starter_gift", "starter_offer"}:
            onboarding_state = await db.get_onboarding_state(conn, tg_uid)
            onboarding_status = str(onboarding_state.get("onboarding_status") or "").strip().lower()
            starter_used_row = await db.fetchone(
                conn,
                "SELECT starter_trial_used FROM users WHERE tg_user_id=?",
                (int(tg_uid),),
            )
            starter_used = int(starter_used_row["starter_trial_used"] or 0) if starter_used_row else 0
            has_premium = await db.is_premium_active(conn, tg_uid)
            if onboarding_status == "done" and starter_used == 0 and not has_premium:
                await _show_starter_trial_offer(update, context)
                return
        await _continue_after_consent(update, context, conn, tg_uid)
    finally:
        await conn.close()

async def _show_timezone_picker(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    page: int,
    *,
    message_text: str | None = None,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    context.user_data["tz_page"] = page
    await show(
        update,
        context,
        message_text
        or "Выбери часовой пояс, чтобы уведомления приходили вовремя.",
        timezone_picker(page),
        route=f"tz:page:{page}",
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )

def _is_debug_user(settings, tg_uid: int) -> bool:
    return bool(settings.DEBUG or (tg_uid in settings.ADMIN_IDS))


async def payments_reconcile_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    from webapp_backend import reconcile_redirect_payments
    await reconcile_redirect_payments()


async def reminders_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    from telegram.error import Forbidden
    s = context.application.bot_data["settings"]
    conn = await db.connect(s.DB_PATH)
    try:
        due = await db.list_due_reminders(conn)
        due += await db.list_retryable_reminder_fires(conn)
        batches: dict[tuple[int,str,str], set[int]] = {}
        for item in due:
            tg_user_id, supplement_id = int(item['user_id']), int(item['supplement_id'])
            day, time_local = str(item['day']), str(item['time_local'])
            if await db.get_day_status(conn,tg_user_id,day,supplement_id) in {'taken','skipped'}:
                continue
            batches.setdefault((tg_user_id,time_local,day),set()).add(supplement_id)
        delivery_batches = (
            (key, [item['id'] for item in group])
            for key, ids in batches.items()
            for group in _reminder_groups([{'id': sid} for sid in sorted(ids)], key[2])
        )
        for (tg_user_id,time_local,day), supplement_ids in delivery_batches:
            claims = {}
            for sid in sorted(supplement_ids):
                token = await db.insert_reminder_fire(conn,tg_user_id,sid,day,time_local)
                if token:
                    claims[sid] = token
            if not claims:
                continue
            delivered, permanent = False, False
            try:
                await reminder_fire_batch(context,tg_user_id,list(claims))
                delivered = True
            except Forbidden:
                permanent = True
            except Exception as exc:
                log.warning('Reminder delivery postponed user=%s error=%s',tg_user_id,type(exc).__name__)
            async with db.transaction(conn) as tx:
                for sid,token in claims.items():
                    await db.finish_reminder_fire(tx,tg_user_id,sid,day,time_local,token,delivered=delivered,permanent=permanent)
    finally:
        await conn.close()


async def nutrient_lifecycle_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    s = context.application.bot_data["settings"]
    conn = await db.connect(s.DB_PATH)
    try:
        result = await db.process_due_nutrient_lifecycle(conn, limit=500)
        if int(result.get("changed") or 0) > 0:
            log.info("nutrient_lifecycle_tick applied: %s", result)
    finally:
        await conn.close()


async def daily_rollover_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    s = context.application.bot_data["settings"]
    expired_due: list[dict] = []
    conn = await db.connect(s.DB_PATH)
    try:
        rows = await db.fetchall(
            conn,
            "SELECT tg_user_id, tz_name, last_local_date FROM users",
        )
        updates = []
        for row in rows:
            tz_name = str(row["tz_name"] or "").strip()
            today_local = db.get_local_date_str(tz_name or None)
            last_local = str(row["last_local_date"] or "")
            if last_local != today_local:
                updates.append((today_local, int(row["tg_user_id"])))
        if updates:
            await conn.executemany(
                "UPDATE users SET last_local_date=? WHERE tg_user_id=?",
                updates,
            )
            await conn.commit()
        expired_due = await db.list_users_with_access_expired_notice_due(conn, limit=300)
    finally:
        await conn.close()

    for row in expired_due:
        tg_user_id = int(row.get("tg_user_id") or 0)
        if tg_user_id <= 0:
            continue
        last_chat_id = int(row.get("last_chat_id") or 0) or tg_user_id
        conn_u = await db.connect(s.DB_PATH)
        try:
            prev_mid, _prev_screen, _prev_payload = await db.get_ui_state(conn_u, tg_user_id)
        finally:
            await conn_u.close()
        if int(prev_mid or 0) > 0:
            try:
                await context.bot.delete_message(chat_id=last_chat_id, message_id=int(prev_mid))
            except Exception:
                pass
        try:
            msg = await context.bot.send_message(
                chat_id=last_chat_id,
                text=_subscription_expired_text(),
                reply_markup=_subscription_expired_kb(),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            continue
        conn_u = await db.connect(s.DB_PATH)
        try:
            await db.save_ui_state(conn_u, tg_user_id, msg.message_id, "premium:expired:notice", "{}")
            await db.set_user_last_msg(conn_u, tg_user_id, last_chat_id, msg.message_id)
            await db.mark_access_expired_notice_sent(
                conn_u,
                tg_user_id,
                row.get("premium_until_utc"),
                row.get("starter_trial_until_utc"),
            )
        finally:
            await conn_u.close()

async def reminder_fire_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data or {}
    tg_user_id = int(data.get("tg_user_id"))
    supplement_ids = data.get("supplement_ids")
    if isinstance(supplement_ids, list) and supplement_ids:
        await reminder_fire_batch(context, tg_user_id, supplement_ids, is_snooze=True)
        return
    supplement_id = int(data.get("supplement_id"))
    await reminder_fire(context, tg_user_id, supplement_id, is_snooze=True)

def _parse_callback_batch_ids(raw: str | None) -> list[int]:
    out: list[int] = []
    for token in str(raw or "").split(","):
        token = token.strip()
        if not token or not token.isdigit():
            continue
        sid = int(token)
        if sid <= 0 or sid in out:
            continue
        out.append(sid)
    return out


def _reminder_batch_actions(supplement_ids: list[int], *, day: str | None = None, records: list[dict] | None = None) -> InlineKeyboardMarkup:
    batch_ids = [int(sid) for sid in supplement_ids if int(sid) > 0]
    encoded_batch = ",".join(str(sid) for sid in batch_ids)
    if day:
        encoded_batch = f"d{day.replace('-', '')}:{encoded_batch}"
    done_cb = f"rem:done_all:{encoded_batch}" if encoded_batch else "rem:done_all"
    skip_cb = f"rem:skip_all:{encoded_batch}" if encoded_batch else "rem:skip_all"
    snooze15_cb = f"rem:snooze_all:15:{encoded_batch}" if encoded_batch else "rem:snooze_all:15"
    snooze30_cb = f"rem:snooze_all:30:{encoded_batch}" if encoded_batch else "rem:snooze_all:30"
    snooze60_cb = f"rem:snooze_all:60:{encoded_batch}" if encoded_batch else "rem:snooze_all:60"
    if max(len(done_cb), len(skip_cb), len(snooze15_cb), len(snooze30_cb), len(snooze60_cb)) > 64:
        if day:
            raise ValueError("reminder_batch_too_large")
        done_cb = "rem:done_all"
        skip_cb = "rem:skip_all"
        snooze15_cb = "rem:snooze_all:15"
        snooze30_cb = "rem:snooze_all:30"
        snooze60_cb = "rem:snooze_all:60"
    rows = [
            [
                InlineKeyboardButton("✅ Выпил", callback_data=done_cb),
                InlineKeyboardButton("❌ Не выпил", callback_data=skip_cb),
            ],
            [
                InlineKeyboardButton("⏱ 15 мин", callback_data=snooze15_cb),
                InlineKeyboardButton("⏱ 30 мин", callback_data=snooze30_cb),
                InlineKeyboardButton("⏱ 60 мин", callback_data=snooze60_cb),
            ],
        ]
    for item in records or []:
        rows.append([InlineKeyboardButton(f"💊 {str(item['name'])[:60]}", callback_data=f"mysupp:open:{int(item['id'])}")])
    return InlineKeyboardMarkup(rows)

def _reminder_groups(entries: list[dict], day: str) -> list[list[dict]]:
    groups, current = [], []
    for item in entries:
        candidate = current + [item]
        encoded = ','.join(str(entry['id']) for entry in candidate)
        if current and (len(candidate) > 8 or len(f"rem:snooze_all:60:d{day.replace('-', '')}:{encoded}") > 64):
            groups.append(current)
            current = []
        current.append(item)
    if current:
        groups.append(current)
    return groups

def _reminder_preview(text: str, limit: int) -> str:
    chunks = _escaped_text_chunks(text, limit)
    return chunks[0] + ('…' if len(chunks) > 1 else '')

async def reminder_fire(
    context: ContextTypes.DEFAULT_TYPE,
    tg_user_id: int,
    supplement_id: int,
    *,
    is_snooze: bool = False,
) -> None:
    await reminder_fire_batch(context, tg_user_id, [supplement_id], is_snooze=is_snooze)

async def reminder_fire_batch(
    context: ContextTypes.DEFAULT_TYPE,
    tg_user_id: int,
    supplement_ids: list[int],
    *,
    is_snooze: bool = False,
) -> None:
    s = context.application.bot_data["settings"]
    conn = await db.connect(s.DB_PATH)
    try:
        user_row = await db.fetchone(
            conn,
            """SELECT u.id, u.tg_user_id, u.tz_name, u.last_chat_id
               FROM users u
               LEFT JOIN user_notification_prefs np ON np.user_id=u.id
               WHERE u.tg_user_id=? AND COALESCE(u.is_banned,0)=0
                 AND COALESCE(u.deleted_at_utc,'')=''
                 AND COALESCE(np.reminders_enabled,1)=1""",
            (tg_user_id,),
        )
        if not user_row:
            return
        ui_message_id, ui_screen, ui_payload = await db.get_ui_state(conn, tg_user_id)
        try:
            payload_data = json.loads(ui_payload) if ui_payload else {}
        except json.JSONDecodeError:
            payload_data = {}
        if not isinstance(payload_data, dict):
            payload_data = {}

        previous_reminder_msg_id = int(payload_data.get("reminder_msg_id") or 0)

        entries: list[dict] = []
        day = db.get_user_local_date(user_row['tz_name'] or 'UTC')
        slots = {str(slot['slot_id']): slot for slot in await db.get_user_slots(conn, int(user_row['id']))}
        schedules = await db.get_user_schedule_map(conn, int(user_row['id']))
        for supplement_id in dict.fromkeys(supplement_ids):
            user_supp = await db.get_user_supplement(conn, tg_user_id, supplement_id)
            if not user_supp:
                continue
            slot_id = str((schedules.get(supplement_id) or {}).get('slot_id') or 'morning')
            if not bool((slots.get(slot_id) or {}).get('is_enabled', True)) and not await db.list_reminders_for_supplement(conn, tg_user_id, supplement_id):
                continue
            if await db.get_day_status(conn, tg_user_id, day, supplement_id) in {'taken', 'skipped'}:
                continue
            supplement = await db.get_supplement_by_id(conn, supplement_id)
            if not supplement:
                continue
            name_ru = (supplement["name_ru"] or supplement["name"] or "").strip()
            name_en = (supplement["name_en"] or "").strip()
            name_line = name_ru
            if name_en and name_en != name_ru:
                name_line = f"{name_line} ({name_en})"
            dose_text = "—"
            if user_supp and str(user_supp["dose_text"] or "").strip():
                dose_text = str(user_supp["dose_text"]).strip()
            entries.append({"id": supplement_id, "name": name_line, "dose": dose_text})

        if not entries:
            return

        target_chat_id = int(user_row["last_chat_id"] or 0) or tg_user_id
        groups = _reminder_groups(entries, day)
        new_messages = []
        for index, group in enumerate(groups):
            lines = ["⏰ <b>Напоминание</b>" + (f" · Часть {index + 1}/{len(groups)}" if len(groups) > 1 else ""), "Пора принять:"]
            lines.extend(f"• <b>{_reminder_preview(item['name'], 200)}</b> — {_reminder_preview(item['dose'], 220)}" for item in group)
            if any(len(_escaped_text_chunks(item['dose'], 220)) > 1 for item in group):
                lines.append("\nПолная запись — по кнопке с названием добавки.")
            msg = await context.bot.send_message(chat_id=target_chat_id, text='\n'.join(lines), reply_markup=_reminder_batch_actions([item['id'] for item in group], day=day, records=group), parse_mode="HTML", disable_web_page_preview=True)
            new_messages.append({'message_id': int(msg.message_id), 'chat_id': target_chat_id})
        previous_messages = payload_data.get('reminder_messages')
        if not isinstance(previous_messages, list):
            previous_messages = []
        if not previous_messages and previous_reminder_msg_id > 0:
            previous_messages = [{'message_id': previous_reminder_msg_id, 'chat_id': int(payload_data.get('reminder_chat_id') or 0) or target_chat_id}]
        same_day = payload_data.get('reminder_day') == day
        for previous in ([] if same_day else previous_messages):
            if not isinstance(previous, dict) or not str(previous.get('message_id') or '').isdigit() or not str(previous.get('chat_id') or '').lstrip('-').isdigit():
                continue
            try:
                await context.bot.delete_message(chat_id=int(previous['chat_id']), message_id=int(previous['message_id']))
            except Exception:
                pass

        # Sending can overlap navigation; merge into the current screen, not the old snapshot.
        async with db.transaction(conn) as tx:
            current_message, current_screen, current_json = await db.get_ui_state(tx, tg_user_id)
            try:
                current_payload = json.loads(current_json) if current_json else {}
            except json.JSONDecodeError:
                current_payload = {}
            if not isinstance(current_payload, dict):
                current_payload = {}
            current_payload['reminder_msg_id'] = int(msg.message_id)
            current_payload['reminder_chat_id'] = target_chat_id
            current_payload['reminder_batch_ids'] = [item['id'] for item in groups[-1]]
            current_messages = current_payload.get('reminder_messages') if current_payload.get('reminder_day') == day else []
            current_payload['reminder_messages'] = (current_messages if isinstance(current_messages, list) else []) + new_messages
            current_payload['reminder_day'] = day
            await db.save_ui_state(tx, tg_user_id, current_message, current_screen, json.dumps(current_payload, ensure_ascii=False))
    finally:
        await conn.close()

async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard_not_banned(update, context):
        return
    q = update.callback_query
    if not q:
        return
    await _safe_answer_callback(update)

    data = q.data or ""
    s = context.application.bot_data["settings"]
    tg_uid = update.effective_user.id

    conn = await db.connect(s.DB_PATH)
    try:
        await db.get_or_create_user(conn, tg_uid, tz_name="")
        has_consent = await db.has_required_consent(
            conn,
            tg_uid,
            offer_version=s.OFFER_VERSION,
            privacy_version=s.PRIVACY_VERSION,
        )

        if data == "consent:accept":
            await db.save_user_consent(
                conn,
                tg_uid,
                offer_version=s.OFFER_VERSION,
                privacy_version=s.PRIVACY_VERSION,
                source="bot:start",
            )
            await _continue_after_consent(update, context, conn, tg_uid)
            return
        if data == "consent:decline":
            await show(
                update,
                context,
                "Без согласия с документами мы не можем продолжить начальный подбор.\nКогда будешь готов(а), нажми /start.",
                InlineKeyboardMarkup([[InlineKeyboardButton("🏠 В меню", callback_data="menu:home")]]),
                route="consent:decline",
            )
            return

        if not has_consent:
            await _render_consent_screen(update, context)
            return

        tz_name = await db.get_user_tz(conn, tg_uid)
        if _is_tz_unset(tz_name):
            context.user_data.pop("tz_back_data", None)
            context.user_data["onboarding"] = "tz"
            await _show_timezone_picker(update, context, 0)
            return

        compact_curated = re.fullmatch(r"curated:(open_id|add_id|detail_id):(\d+)(?::(-?\d+))?", data)
        if compact_curated:
            action, stack_id, page = compact_curated.groups()
            stack = await db.admin_get_curated_stack_by_id(conn, int(stack_id))
            if not stack or not int(stack['is_active'] or 0):
                await show(update, context, "Стек не найден.", back(), route="menu:curated")
                return
            slug = str(stack['slug'])
            data = f"curated:detail_page:{slug}:{page or 0}" if action == 'detail_id' else f"curated:{'add' if action == 'add_id' else 'open'}:{slug}"

        onboarding_state = await db.get_onboarding_state(conn, tg_uid)
        onboarding_status = str(onboarding_state.get("onboarding_status") or "")
        onboarding_raw = str(onboarding_state.get("onboarding_raw_json") or "")
        onboarding_active = onboarding_status in {"active", "results"} and bool(onboarding_raw)
        allowed_during_onboarding = {
            "onboard:results",
            "onboard:retry",
            "onboard:skip",
            "onboard:finish",
            "tz:back",
        }
        if onboarding_active and data.startswith("menu:"):
            await _render_onboarding_results(update, context, conn)
            return
        if onboarding_active and data.startswith("premium:") and data not in allowed_during_onboarding:
            await _render_onboarding_results(update, context, conn)
            return

        if data == "main":
            await home(update, context)
            return
        if data == "menu:home":
            await home(update, context)
            return
        if data == "menu:ai:exit":
            await home(update, context)
            return
        if data == "onboard:results":
            restored = await _render_onboarding_results(update, context, conn)
            if not restored:
                await _start_initial_pick(update, context, prefix="Давай попробуем ещё раз.", back_cb="tz:back")
            return
        if data == "onboard:retry":
            await _start_initial_pick(update, context, prefix="Опиши цель ещё раз — сделаю новый подбор.", back_cb="tz:back")
            return
        if data == "onboard:skip":
            await _complete_onboarding_flow(update, context, conn, tg_uid)
            return
        if data == "onboard:finish":
            await _complete_onboarding_flow(update, context, conn, tg_uid)
            return
        if data == "starter:activate":
            has_premium = await db.is_premium_active(conn, tg_uid)
            if not has_premium:
                until_iso = await db.start_starter_trial(conn, tg_uid, days=3, source="onboarding_finish")
                s_cfg = context.application.bot_data["settings"]
                rows = []
                if s_cfg.WEBAPP_URL:
                    webapp_url = build_webapp_entry_url(
                        s_cfg.WEBAPP_URL,
                        source="starter_activation",
                        tutorial="main",
                    )
                    rows.append([InlineKeyboardButton("🔓 Открыть веб-приложение", web_app=WebAppInfo(url=webapp_url))])
                rows.append([InlineKeyboardButton("🏠 В меню", callback_data="menu:home")])
                await show(
                    update,
                    context,
                    f"Премиум доступ активирован до <b>{until_iso}</b>\n\n"
                    "<i>Открой веб-приложение и начни пользоваться полным режимом.</i>",
                    InlineKeyboardMarkup(rows),
                    route="starter:active",
                )
                return
            await show(
                update,
                context,
                "У тебя уже активна подписка — веб-приложение открыто без ограничений.",
                InlineKeyboardMarkup([[InlineKeyboardButton("🏠 В меню", callback_data="menu:home")]]),
                route="menu:home",
            )
            return
        if data == "starter:stay":
            context.user_data["home_entry_reason"] = "onboarding_completed"
            await home(update, context)
            return
        if data == "starter:premium_about":
            await show(
                update,
                context,
                "⭐ <b>Что входит в подписку</b>\n\n"
                "• полный доступ к веб-приложению\n"
                "• карта органов с деталями по системам\n"
                "• ИИ-опрос и подсказки по самочувствию\n"
                "• удобный трекинг приёма и напоминаний\n"
                "• достижения и персонализация интерфейса\n\n"
                "База добавок и стартовый подбор по-прежнему доступны бесплатно.",
                InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("🎁 Активировать 3 дня", callback_data="starter:activate")],
                        [InlineKeyboardButton("⬅️ Назад", callback_data="starter:offer")],
                    ]
                ),
                route="starter:about",
            )
            return
        if data == "starter:offer":
            await _show_starter_trial_offer(update, context)
            return
        if data == "onboard:restart_confirm":
            kb = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("✅ Начать заново", callback_data="onboard:restart_apply")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="menu:settings")],
                ]
            )
            await show(
                update,
                context,
                "Подтверди перезапуск стартового подбора. Предыдущие рекомендации будут очищены.",
                kb,
                route="onboard:restart_confirm",
            )
            return
        if data == "onboard:restart_apply":
            if not s.OPENAI_API_KEY:
                await show(
                    update,
                    context,
                    "Начальный подбор недоступен: добавь OPENAI_API_KEY в .env.",
                    settings_menu(),
                    route="menu:settings",
                )
                return
            existing_ids = await db.get_user_initial_stack_ids(conn, tg_uid)
            if existing_ids:
                await db.remove_user_supplements_bulk(conn, tg_uid, existing_ids)
            await db.set_user_initial_stack_ids(conn, tg_uid, [])
            await db.clear_onboarding_state(conn, tg_uid)
            await db.start_onboarding_state(conn, tg_uid)
            await _start_initial_pick(
                update,
                context,
                prefix="🔄 Начинаем новый подбор. Предыдущий список очищен.",
                back_cb="menu:settings",
            )
            return
        if data == "menu:settings":
            await _render_settings_menu(update, context, conn, tg_uid)
            return
        if data == "menu:premium":
            await _show_premium_hub(update, context)
            return
        if data == "premium:expired:notice":
            await show(update, context, _subscription_expired_text(), _subscription_expired_kb(), route="premium:expired:notice")
            return
        if data == "premium:expired:pay":
            await _show_premium_hub(update, context, back_cb="premium:expired:notice", route="premium:from_expired")
            return
        if data == "premium:about":
            await show(
                update,
                context,
                "⭐ <b>Что входит в подписку</b>\n\n"
                "• полный доступ к веб-приложению\n"
                "• карта органов с деталями по системам\n"
                "• ИИ-опрос и подсказки по самочувствию\n"
                "• удобный трекинг приёма и напоминаний\n"
                "• достижения и персонализация интерфейса\n\n"
                "База добавок и стартовый подбор по-прежнему доступны бесплатно.",
                premium_upsell_kb(),
                route="menu:premium",
            )
            return
        if data == "premium:stars_menu":
            s_cfg = context.application.bot_data["settings"]
            plans = _premium_plans_payload(s_cfg)
            current_route = str(context.user_data.get("last_route") or "")
            stars_back = "menu:premium"
            stars_route = "menu:premium"
            if current_route == "premium:from_expired":
                stars_back = "premium:expired:notice"
                stars_route = "premium:from_expired"
            await show(
                update,
                context,
                "⭐ <b>Оплата в звёздах</b>\n\nВыберите срок подписки:",
                premium_stars_kb(plans, back_cb=stars_back),
                route=stars_route,
            )
            return
        if data.startswith("premium:buy:"):
            _, _, plan_id, method = data.split(":", 3)
            await _send_subscription_invoice(update, context, plan_id, method)
            return
        if data == "menu:complexes":
            context.user_data[COMPLEXES_BACK_ROUTE_KEY] = "mysupp:list:0"
            await _show_complexes_hub(update, context, conn, tg_uid)
            return
        if data.startswith("mysupp:complexes:"):
            parts = data.split(":")
            page = 0
            if len(parts) >= 3:
                try:
                    page = max(0, int(parts[2]))
                except ValueError:
                    page = 0
            part = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            context.user_data[COMPLEXES_BACK_ROUTE_KEY] = f"mysupp:list:{page}" + (f":{part}" if part else "")
            await _show_complexes_hub(update, context, conn, tg_uid)
            return
        if data == "cx:list" or data.startswith("cx:list:"):
            page = int(data.split(":")[-1]) if data.split(":")[-1].isdigit() else 0
            await _show_complexes_hub(update, context, conn, tg_uid, page)
            return
        if data == "cx:create":
            context.user_data[WAITING_KEY] = "cx:create"
            await show(update, context, "Введи название нового комплекса.", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="cx:list")]]), route="cx:create")
            return
        if data.startswith("cx:open:"):
            parts = data.split(":")
            complex_id = int(parts[2])
            page = int(parts[3]) if len(parts) > 3 and parts[3].lstrip('-').isdigit() else 0
            await _show_complex_detail(update, context, conn, tg_uid, complex_id, page)
            return
        if data.startswith("cx:rename:"):
            complex_id = int(data.split(":", 2)[2])
            context.user_data[WAITING_KEY] = f"cx:rename:{complex_id}"
            await show(update, context, "Введи новое название комплекса.", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"cx:open:{complex_id}")]]), route=f"cx:rename:{complex_id}")
            return
        if data.startswith("cx:delete:confirm:"):
            complex_id = int(data.split(":", 3)[3])
            await db.delete_complex(conn, tg_uid, complex_id)
            await _show_complexes_hub(update, context, conn, tg_uid)
            return
        if data.startswith("cx:delete:"):
            complex_id = int(data.split(":", 2)[2])
            await show(update, context, "Удалить комплекс?", InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Да, удалить", callback_data=f"cx:delete:confirm:{complex_id}")],[InlineKeyboardButton("⬅️ Отмена", callback_data=f"cx:open:{complex_id}")]]), route=f"cx:delete:{complex_id}")
            return
        if data.startswith("cx:add_pick:"):
            parts = data.split(":", 3)
            if len(parts) != 4:
                log.warning("Invalid complex pick callback payload: %s", data)
                return
            _, _, cx_id, supp_id = parts
            try:
                await db.add_item_to_complex(conn, tg_uid, int(cx_id), int(supp_id), dose_override="")
            except ValueError:
                await _show_complex_detail(update, context, conn, tg_uid, int(cx_id))
                return
            await _show_complex_detail(update, context, conn, tg_uid, int(cx_id))
            return
        if data.startswith("cx:add:mine:"):
            _, _, _, cx_id, page_raw = data.split(":", 4)
            complex_id = int(cx_id)
            page = max(0, int(page_raw) if str(page_raw).isdigit() else 0)
            rows, total = await db.list_user_supplements_page(conn, tg_uid, q=None, page=page, page_size=8)
            if not rows:
                await show(
                    update,
                    context,
                    "В твоём списке пока нет БАДов. Сначала добавь их в «Мои БАДы» или выбери из каталога.",
                    complex_add_menu_kb(complex_id),
                    route=f"cx:add:{complex_id}",
                )
                return
            await show(
                update,
                context,
                "Выбери БАД из уже добавленных в твой список:",
                complex_add_mine_kb(
                    complex_id,
                    rows,
                    page,
                    has_prev=page > 0,
                    has_next=((page + 1) * 8) < int(total or 0),
                ),
                route=f"cx:add:mine:{complex_id}:{page}",
            )
            return
        if data.startswith("cx:add:catalog:"):
            complex_id = int(data.split(":")[-1])
            context.user_data[WAITING_KEY] = f"cx:add_query:{complex_id}"
            await show(
                update,
                context,
                "Введи название БАДа для добавления из каталога.",
                InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"cx:add:{complex_id}")]]),
                route=f"cx:add:catalog:{complex_id}",
            )
            return
        if data.startswith("cx:add:"):
            complex_id = int(data.split(":", 2)[2])
            await show(
                update,
                context,
                "Как добавить БАД в комплекс?",
                complex_add_menu_kb(complex_id),
                route=f"cx:add:{complex_id}",
            )
            return
        if data.startswith("cx:item:open:"):
            _, _, _, cx_id, supp_id = data.split(":", 4)
            await _show_complex_item(update, context, conn, tg_uid, int(cx_id), int(supp_id))
            return
        if data.startswith("cx:item:dose:"):
            _, _, _, cx_id, supp_id = data.split(":", 4)
            context.user_data[WAITING_KEY] = f"cx:item:dose:{cx_id}:{supp_id}"
            await show(update, context, "Введи дозировку для этой добавки в комплексе.", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"cx:item:open:{cx_id}:{supp_id}")]]), route=f"cx:item:dose:{cx_id}:{supp_id}")
            return
        if data.startswith("cx:item:remove:"):
            _, _, _, cx_id, supp_id = data.split(":", 4)
            await db.remove_item_from_complex(conn, tg_uid, int(cx_id), int(supp_id))
            await _show_complex_detail(update, context, conn, tg_uid, int(cx_id))
            return
        if data.startswith("cx:schedule:") and data.count(":") == 2:
            complex_id = int(data.split(":", 2)[2])
            user_id = await db.get_or_create_user(conn, tg_uid, tz_name="")
            slots = await db.get_user_slots(conn, user_id)
            await show(update, context, "Выбери слот для комплекса.", complex_schedule_kb(complex_id, slots), route=f"cx:schedule:{complex_id}")
            return
        if data.startswith("cx:schedule:set:"):
            _, _, _, cx_id, slot_id = data.split(":", 4)
            user_id = await db.get_or_create_user(conn, tg_uid, tz_name="")
            slots = await db.get_user_slots(conn, user_id)
            slot = next((x for x in slots if str(x.get("slot_id") or "") == slot_id), None)
            if not slot:
                await show(update, context, "Этот слот уже удалён. Выбери другой.", complex_schedule_kb(int(cx_id), slots), route=f"cx:schedule:{cx_id}")
                return
            time_local = str(slot.get("time_local") or "") if slot else ""
            try:
                await db.set_complex_schedule(conn, tg_uid, int(cx_id), slot_id, time_local)
            except ValueError:
                await _show_complexes_hub(update, context, conn, tg_uid)
                return
            await _show_complex_detail(update, context, conn, tg_uid, int(cx_id))
            return
        if data.startswith("cx:schedule:clear:"):
            complex_id = int(data.split(":", 3)[3])
            schedules = [x for x in await db.list_complex_schedules(conn, tg_uid) if int(x.get("complex_id") or 0) == complex_id]
            for item in schedules:
                await db.remove_complex_schedule(conn, tg_uid, complex_id, str(item.get("slot_id") or ""))
            await _show_complex_detail(update, context, conn, tg_uid, complex_id)
            return
        if data == "menu:stats":
            await _show_stats_screen(update, context, conn, tg_uid)
            return
        if data == "menu:webapp":
            await show(
                update,
                context,
                "Открой WebApp для расширенного режима и дополнительных функций.",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("⭐ Купить Premium", callback_data="menu:premium")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")],
                ]),
                route="menu:webapp",
            )
            return
        if data == "settings:initial_pick":
            await show(
                update,
                context,
                "Опция «Начальный подбор» больше недоступна в настройках.",
                settings_menu(),
                route="menu:settings",
            )
            return
        if data == "settings:tz":
            context.user_data["tz_back_data"] = "menu:settings"
            await _show_timezone_picker(update, context, 0)
            return
        if data == "settings:schedule":
            await _show_schedule_home(update, context, conn, tg_uid)
            return
        if data == "settings:notifications":
            await _show_notifications_settings(update, context, conn, tg_uid)
            return
        if data.startswith("settings:notifications:global:"):
            enabled = data.split(":")[-1] == "1"
            await db.set_user_notifications_enabled(conn, tg_uid, enabled)
            await _show_notifications_settings(update, context, conn, tg_uid)
            return
        if data.startswith("settings:notifications:slot:"):
            parts = data.split(":", 3)[3].rsplit(":", 1)
            slot_id = parts[0]
            desired = parts[1] == "1" if len(parts) == 2 and parts[1] in {"0", "1"} else None
            user_id = await db.get_or_create_user(conn, tg_uid, tz_name="")
            try:
                await db.set_user_slot_enabled(conn, user_id, slot_id, desired)
            except ValueError:
                await show(update, context, "Слот уже удалён. Обнови список уведомлений.", InlineKeyboardMarkup([[InlineKeyboardButton("🔔 Уведомления", callback_data="settings:notifications")]]), route="settings:notifications")
                return
            await _show_notifications_settings(update, context, conn, tg_uid)
            return
        if data == "settings:complexes":
            context.user_data[COMPLEXES_BACK_ROUTE_KEY] = "menu:settings"
            await _show_complexes_hub(update, context, conn, tg_uid)
            return
        if data == "settings:gift":
            await _show_gift_hub(update, context, tg_uid)
            return
        if data == "gift:admin:create":
            if not _is_admin(tg_uid):
                await _show_gift_hub(update, context, tg_uid)
                return
            await show(
                update,
                context,
                "Введите количество дней Premium для нового кода (целое число).",
                InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="settings:gift")]]),
                route="gift:admin:create_wait",
            )
            return
        if data == "gift:admin:codes":
            if not _is_admin(tg_uid):
                await _show_gift_hub(update, context, tg_uid)
                return
            stats = await db.gift_codes_stats(conn)
            await show(
                update,
                context,
                (
                    "🎟 <b>Коды</b>\n"
                    f"Всего создано: <b>{stats['total']}</b>\n"
                    f"Активные: <b>{stats['active']}</b>\n"
                    f"Использованные/деактивированные: <b>{stats['used']}</b>"
                ),
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("🟢 Неиспользованные", callback_data="gift:admin:list:active")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="settings:gift")],
                ]),
                route="gift:admin:codes",
            )
            return
        if data == "gift:admin:list:active":
            if not _is_admin(tg_uid):
                await _show_gift_hub(update, context, tg_uid)
                return
            rows = await db.gift_codes_list(conn, only_active=True, limit=20)
            kb_rows = [[InlineKeyboardButton(f"{r['code']} · {int(r['days'])} дн.", callback_data=f"gift:admin:open:{r['code']}")] for r in rows]
            kb_rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="gift:admin:codes")])
            text = "Активные коды:\n" + ("\n".join([f"• <code>{r['code']}</code> — {int(r['days'])} дн." for r in rows]) if rows else "Пока нет активных кодов.")
            await show(update, context, text, InlineKeyboardMarkup(kb_rows), route="gift:admin:list:active")
            return
        if data.startswith("gift:admin:open:"):
            if not _is_admin(tg_uid):
                await _show_gift_hub(update, context, tg_uid)
                return
            code = data.split(":", 3)[3]
            await show(update, context, f"Код: <code>{code}</code>", InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 Деактивировать", callback_data=f"gift:admin:delete:{code}")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="gift:admin:list:active")],
            ]), route="gift:admin:open")
            return
        if data.startswith("gift:admin:delete:"):
            if not _is_admin(tg_uid):
                await _show_gift_hub(update, context, tg_uid)
                return
            code = data.split(":", 3)[3]
            await db.gift_code_deactivate(conn, code)
            await show(update, context, "Код деактивирован.", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ К кодам", callback_data="gift:admin:codes")]]), route="gift:admin:codes")
            return
        if data == "schedule:slot_pick":
            user_id = await db.get_or_create_user(conn, tg_uid, tz_name="")
            slots = await db.get_user_slots(conn, user_id)
            await show(update, context, "Выбери слот для изменения времени:", schedule_slots_pick_kb(slots), route="schedule:slot_pick")
            return
        if data.startswith("schedule:slot_time_set:"):
            slot_id = data.split(":", 2)[2]
            context.user_data[WAITING_KEY] = f"schedule:slot_time_set:{slot_id}"
            await show(update, context, "Введи время в формате HH:MM", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="settings:notifications")]]), route=f"schedule:slot_time_set:{slot_id}")
            return
        if data == "schedule:add_custom":
            context.user_data[WAITING_KEY] = "schedule:add_custom"
            await show(update, context, "Введи время в формате HH:MM", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="settings:notifications")]]), route="schedule:add_custom")
            return
        if data == "schedule:delete_pick":
            user_id = await db.get_or_create_user(conn, tg_uid, tz_name="")
            slots = await db.get_user_slots(conn, user_id)
            default_slot_ids = {str(item.get("slot_id") or "") for item in db.default_user_slots()}
            custom_slots = [slot for slot in slots if str(slot.get("slot_id") or "") not in default_slot_ids]
            if not custom_slots:
                await show(update, context, "Кастомных слотов пока нет.", schedule_menu_kb(has_custom_slots=False), route="settings:schedule")
                return
            await show(
                update,
                context,
                "Выбери кастомный слот для удаления. БАДы из него автоматически перенесём в ближайший слот по времени.",
                schedule_slots_pick_kb(custom_slots, prefix="schedule:delete_slot"),
                route="schedule:delete_pick",
            )
            return
        if data.startswith("schedule:delete_slot:"):
            slot_id = data.split(":", 2)[2]
            user_id = await db.get_or_create_user(conn, tg_uid, tz_name="")
            try:
                result = await db.delete_custom_slot(conn, user_id, slot_id)
                moved_to = str(result.get("moved_to_slot_id") or "ближайший")
                if update.callback_query:
                    await update.callback_query.answer(f"Слот удалён. Перенесли в: {moved_to}")
            except ValueError as exc:
                message = "Не удалось удалить слот"
                if str(exc) == "slot_not_found":
                    message = "Слот уже удалён"
                if update.callback_query:
                    await update.callback_query.answer(message, show_alert=False)
            await _show_schedule_home(update, context, conn, tg_uid)
            return

        if data == "menu:add":
            context.user_data[WAITING_KEY] = "add_query"
            await show(
                update,
                context,
                "Введи название добавки (поиск по каталогу).\nНапример: <b>caffeine</b> или <b>magnesium</b>",
                back(),
                route="menu:add",
            )
            return

        if data in {"menu:catalog", "catalog"}:
            await _show_catalog_home(update, context, conn)
            return

        if data == "menu:curated":
            await _show_curated_page(update, context, conn, 0)
            return

        if data == "admin:home":
            context.user_data.pop("admin_seed_buffer", None)
            await _show_admin_home(update, context)
            return

        if data == "admin:bulk_stub":
            await show(update, context, "Bulk-операции доступны в WebApp разделе Admin.", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin:home")]]), route="admin:home")
            return

        if data == "admin:seed_patch":
            if not await _require_admin(update, context, "content_bulk"):
                return
            context.user_data["admin_seed_buffer"] = ""
            await show(
                update,
                context,
                (
                    "➕ <b>Дополнить базу</b>\n"
                    "Отправь одним сообщением JSON-объект (или несколько подряд), как в rich seed.\n"
                    "Обязательное поле: <code>slug</code>.\n\n"
                    "После отправки бот запустит импорт и сразу запишет данные в БД.\n\n"
                    f"{_admin_seed_helper_text()}"
                ),
                InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin:home")]]),
                route="admin:seed_patch_wait",
                payload={"mode": "rich_patch", "buffer": ""},
            )
            return

        if data == "admin:users_stub":
            await show(update, context, "Модерация пользователей доступна в WebApp разделе Admin.", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin:home")]]), route="admin:home")
            return

        if data == "admin:paychecks":
            if not await _require_admin(update, context, "payments_view"):
                return
            await _show_admin_paychecks(update, context, conn, filter_code="wants_receipt", index=0)
            return

        if data.startswith("admin:paychecks:"):
            if not await _require_admin(update, context, "payments_view"):
                return
            _, _, ui_payload = await db.get_ui_state(conn, tg_uid)
            payload_data = {}
            try:
                payload_data = json.loads(ui_payload) if ui_payload else {}
            except json.JSONDecodeError:
                payload_data = {}
            if not isinstance(payload_data, dict):
                payload_data = {}
            filter_code = str(payload_data.get("filter") or "wants_receipt")
            index = int(payload_data.get("index") or 0)
            payment_id = int(payload_data.get("payment_id") or 0)
            if data.startswith("admin:paychecks:filter:"):
                filter_code = data.split(":", 3)[-1]
                index = 0
            elif data.startswith("admin:paychecks:nav:"):
                nav = data.split(":")[-1]
                rows = await db.admin_list_receipt_payments(conn, filter_code=filter_code, limit=500)
                total = len(rows)
                if nav == "first":
                    index = 0
                elif nav == "last":
                    index = max(0, total - 1)
                elif nav == "prev":
                    index = max(0, index - 1)
                elif nav == "next":
                    index = min(max(0, total - 1), index + 1)
            elif data.endswith("mark_sent") and payment_id > 0:
                await db.admin_set_payment_receipt_status(conn, payment_id, status="sent")
            elif data.endswith("mark_pending") and payment_id > 0:
                await db.admin_set_payment_receipt_status(conn, payment_id, status="pending")
            await _show_admin_paychecks(update, context, conn, filter_code=filter_code, index=index)
            return

        if data == "admin:diag":
            if not await _require_admin(update, context, "system_diag"):
                return
            await show(update, context, "Запуск диагностики целостности данных.", admin_diag_kb(), route="admin:diag")
            return

        if data == "admin:diag:dry":
            if not await _require_admin(update, context, "system_diag"):
                return
            diag = await db.run_diagnostics(conn, apply=False)
            await show(update, context, _render_diag_summary(diag), admin_diag_kb(), route="admin:diag")
            return

        if data == "admin:diag:apply":
            if not await _require_admin(update, context, "system_diag"):
                return
            await show(update, context, "Подтверди ремонт данных. Это безопасные правки без удаления пользовательских сущностей.", admin_diag_confirm_kb(), route="admin:diag:confirm")
            return

        if data == "admin:diag:apply:confirm":
            if not await _require_admin(update, context, "system_diag"):
                return
            diag = await db.run_diagnostics(conn, apply=True)
            await show(update, context, _render_diag_summary(diag), admin_diag_kb(), route="admin:diag")
            return

        if data == "admin:roles":
            context.user_data[WAITING_KEY] = "owner_wait_target_tg_id"
            await show(update, context, "Введите tg_user_id пользователя для управления ролью.", InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data="admin:home")]]), route="owner_wait_target_tg_id")
            return

        if data.startswith("admin:roles:set:"):
            parts = data.split(":")
            role_to_set = parts[3] if len(parts) > 3 else "none"
            target_id = int(context.user_data.get("owner_target_tg_id") or 0)
            if target_id <= 0:
                await show(update, context, "Сначала укажите tg_user_id.", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin:roles")]]), route="admin:roles")
                return
            if role_to_set == "none":
                await db.remove_role(conn, tg_uid, target_id)
            else:
                await db.set_role(conn, tg_uid, target_id, role_to_set, "bot owner panel")
            await conn.commit()
            role_now = await db.get_role(conn, target_id)
            await show(update, context, f"Роль пользователя {target_id}: {role_now}", InlineKeyboardMarkup([[InlineKeyboardButton("Сделать админом", callback_data="admin:roles:set:admin")],[InlineKeyboardButton("Сделать редактором", callback_data="admin:roles:set:editor")],[InlineKeyboardButton("Сделать саппортом", callback_data="admin:roles:set:support")],[InlineKeyboardButton("Снять роль", callback_data="admin:roles:set:none")],[InlineKeyboardButton("⬅️ Назад", callback_data="admin:home")]]), route="admin:roles")
            return

        if data.startswith("admin:"):
            role, perms = await _get_role_perms_ctx(context, tg_uid)
            if role == db.ROLE_NONE:
                await show(
                    update,
                    context,
                    "Нет доступа.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")]]),
                    route="menu:home",
                )
                return
            required_perm = None
            if data.startswith("admin:supp"):
                required_perm = "content_edit"
            elif data.startswith("admin:stack") or data == "admin:stacks":
                required_perm = "content_bulk"
            elif data.startswith("admin:seed_patch"):
                required_perm = "content_bulk"
            elif data.startswith("admin:roles"):
                required_perm = "roles_manage"
            elif data.startswith("admin:users"):
                required_perm = "users_view"
            elif data.startswith("admin:paychecks"):
                required_perm = "payments_view"
            elif data.startswith("admin:diag"):
                required_perm = "system_diag"
            if required_perm and not perms.get(required_perm, False):
                await show(update, context, "Нет доступа к разделу.", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin:home")]]), route="admin:home")
                return
            item_action = re.fullmatch(r"admin:stack:item:(open|del|edit|move):(\d+):(\d+)(?::[^:]+)?", data)
            if item_action:
                _, stack_id, item_id = item_action.groups()
                item = await db.admin_get_curated_stack_item(conn, int(item_id))
                if not item or int(item['stack_id']) != int(stack_id):
                    await _show_admin_stack_items(update, context, conn, int(stack_id))
                    return

        if data == "admin:supp_search":
            await show(
                update,
                context,
                "Введи название/часть названия (RU/EN) или slug:",
                InlineKeyboardMarkup([[InlineKeyboardButton("↩️ В админ-меню", callback_data="admin:home")]]),
                route="admin_wait_query",
                payload={"admin_query": "", "offset": 0},
            )
            return

        if data.startswith("admin:supp_list:"):
            offset = int(data.split(":")[-1]) if data.split(":")[-1].isdigit() else 0
            _, _, ui_payload = await db.get_ui_state(conn, tg_uid)
            payload_data = {}
            try:
                payload_data = json.loads(ui_payload) if ui_payload else {}
            except json.JSONDecodeError:
                payload_data = {}
            query = str(payload_data.get("admin_query") or "").strip()
            await _show_admin_supp_results(update, context, conn, query, offset=offset)
            return

        if data.startswith("admin:supp_open:"):
            supplement_id = int(data.split(":")[-1])
            _, _, ui_payload = await db.get_ui_state(conn, tg_uid)
            payload_data = {}
            try:
                payload_data = json.loads(ui_payload) if ui_payload else {}
            except json.JSONDecodeError:
                payload_data = {}
            await _show_admin_supp_card(
                update,
                context,
                conn,
                supplement_id,
                admin_query=str(payload_data.get("admin_query") or ""),
                offset=int(payload_data.get("offset") or 0),
            )
            return

        if data.startswith("admin:supp_set_link:"):
            supplement_id = int(data.split(":")[-1])
            _, _, ui_payload = await db.get_ui_state(conn, tg_uid)
            payload_data = {}
            try:
                payload_data = json.loads(ui_payload) if ui_payload else {}
            except json.JSONDecodeError:
                payload_data = {}
            await show(
                update,
                context,
                "Отправь URL (https://...)",
                InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Назад", callback_data=f"admin:supp_open:{supplement_id}")]]),
                route="admin_wait_link",
                payload={"supplement_id": supplement_id, "admin_query": str(payload_data.get("admin_query") or ""), "offset": int(payload_data.get("offset") or 0)},
            )
            return

        if data.startswith("admin:supp_edit:"):
            supplement_id = int(data.split(":")[-1])
            await _show_admin_supp_edit_picker(update, context, conn, supplement_id)
            return

        if data.startswith("admin:supp_edit_field:"):
            parts = data.split(":", 3)
            if len(parts) != 4:
                await show(update, context, "Некорректная команда.", admin_hub_kb(), route="admin:home")
                return
            supplement_id = int(parts[2])
            field = parts[3]
            await _show_admin_supp_field_input(update, context, conn, supplement_id, field)
            return

        if data.startswith("admin:supp_edit_clear:"):
            supplement_id = int(data.split(":")[-1])
            _, ui_screen, ui_payload = await db.get_ui_state(conn, tg_uid)
            if ui_screen != "admin_edit_field":
                await _show_admin_supp_card(update, context, conn, supplement_id)
                return
            try:
                payload_data = json.loads(ui_payload) if ui_payload else {}
            except json.JSONDecodeError:
                payload_data = {}
            field_name = str(payload_data.get("field_name") or "")
            try:
                await db.admin_set_supplement_field(conn, tg_uid, supplement_id, field_name, "")
            except ValueError:
                await show(update, context, "Не удалось очистить поле.", admin_supp_edit_wait_kb(supplement_id), route="admin_edit_field", payload=payload_data)
                return
            await show(
                update,
                context,
                "✅ Поле очищено",
                InlineKeyboardMarkup([[InlineKeyboardButton("Продолжить", callback_data=f"admin:supp_open:{supplement_id}")]]),
                route=f"admin:supp_open:{supplement_id}",
                payload={"supplement_id": supplement_id},
            )
            await _show_admin_supp_card(update, context, conn, supplement_id)
            return

        if data.startswith("admin:supp_audit:"):
            supplement_id = int(data.split(":")[-1])
            audit_rows = await db.get_admin_audit_for_supplement(conn, supplement_id, limit=10)
            lines = ["🧾 <b>История правок</b>"]
            if not audit_rows:
                lines.append("Пока нет записей.")
            else:
                for item in audit_rows:
                    lines.append(
                        f"[{item['created_at_utc']}] {item['field']}: {item['old_value'] or '—'} → {item['new_value'] or '—'} ({item['action']}, admin {item['admin_tg_id']})"
                    )
            await show(
                update,
                context,
                "\n".join(lines),
                admin_supp_audit_back_kb(supplement_id),
                route=f"admin:supp_audit:{supplement_id}",
                payload={"supplement_id": supplement_id},
            )
            return

        if data.startswith("admin:supp_toggle:"):
            supplement_id = int(data.split(":")[-1])
            row = await db.get_supplement_admin(conn, supplement_id)
            if not row:
                await show(update, context, "Добавка не найдена.", admin_hub_kb(), route="admin:home")
                return
            current_active = int(row.get("is_active") or 0)
            to_active = 0 if current_active else 1
            question = "Скрыть из каталога? Пользователи не увидят БАД." if to_active == 0 else "Показать в каталоге для пользователей?"
            await show(
                update,
                context,
                question,
                admin_supp_toggle_confirm_kb(supplement_id, to_active),
                route=f"admin:supp_toggle_confirm:{supplement_id}",
                payload={"supplement_id": supplement_id, "to_active": to_active},
            )
            return

        if data.startswith("admin:supp_toggle_confirm:"):
            parts = data.split(":")
            if len(parts) < 5:
                await show(update, context, "Некорректная команда.", admin_hub_kb(), route="admin:home")
                return
            supplement_id = int(parts[3])
            to_active = int(parts[4])
            await db.admin_toggle_supplement_active(conn, tg_uid, supplement_id, to_active)
            await _show_admin_supp_card(update, context, conn, supplement_id)
            return

        if data.startswith("admin:supp_clear_link:"):
            supplement_id = int(data.split(":")[-1])
            await db.admin_set_supplement_field(conn, tg_uid, supplement_id, "purchase_url", "")
            _, _, ui_payload = await db.get_ui_state(conn, tg_uid)
            payload_data = {}
            try:
                payload_data = json.loads(ui_payload) if ui_payload else {}
            except json.JSONDecodeError:
                payload_data = {}
            await _show_admin_supp_card(update, context, conn, supplement_id, admin_query=str(payload_data.get("admin_query") or ""), offset=int(payload_data.get("offset") or 0))
            return

        if data == "admin:back":
            _, _, ui_payload = await db.get_ui_state(conn, tg_uid)
            payload_data = {}
            try:
                payload_data = json.loads(ui_payload) if ui_payload else {}
            except json.JSONDecodeError:
                payload_data = {}
            query = str(payload_data.get("admin_query") or "").strip()
            offset = int(payload_data.get("offset") or 0)
            await _show_admin_supp_results(update, context, conn, query, offset=offset)
            return

        if data == "admin:stacks":
            await _show_admin_stacks(update, context, conn, 0)
            return

        if data.startswith("admin:stacks:page:"):
            page = int(data.split(":")[-1]) if data.split(":")[-1].isdigit() else 0
            await _show_admin_stacks(update, context, conn, max(0, page))
            return

        if data == "admin:stack:new":
            await show(
                update,
                context,
                "Отправь название нового стека.\nПример: База для сна",
                InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin:stacks")]]),
                route="admin:stack:new_wait",
                payload={"return": "admin:stacks"},
            )
            return

        if data.startswith("admin:stack:open:"):
            stack_id = int(data.split(":")[-1])
            await _show_admin_stack_editor(update, context, conn, stack_id)
            return

        if data.startswith("admin:stack:toggle:"):
            stack_id = int(data.split(":")[-1])
            row = await db.admin_get_curated_stack_by_id(conn, stack_id)
            if not row:
                await show(update, context, "Стек не найден.", back(), route="admin:stacks")
                return
            await db.admin_set_curated_stack_active(conn, stack_id, 0 if int(row["is_active"] or 0) else 1)
            await _show_admin_stack_editor(update, context, conn, stack_id)
            return

        if data.startswith("admin:stack:delete_confirm:"):
            stack_id = int(data.split(":")[-1])
            row = await db.admin_get_curated_stack_by_id(conn, stack_id)
            if not row:
                await show(update, context, "Стек не найден.", back(), route="admin:stacks")
                return
            await show(
                update,
                context,
                f"Удалить стек «{row['title_ru'] or 'Без названия'}»?\nЭто действие удалит и его состав.",
                admin_stack_delete_confirm_kb(stack_id),
                route=f"admin:stack:delete_confirm:{stack_id}",
            )
            return

        if data.startswith("admin:stack:delete:"):
            stack_id = int(data.split(":")[-1])
            row = await db.admin_get_curated_stack_by_id(conn, stack_id)
            if not row:
                await show(update, context, "Стек не найден.", back(), route="admin:stacks")
                return
            await db.admin_delete_curated_stack(conn, stack_id)
            await q.answer("Стек удалён.", show_alert=False)
            await _show_admin_stacks(update, context, conn, 0)
            return

        if data.startswith("admin:stack:edit:"):
            parts = data.split(":")
            if len(parts) < 5:
                return
            stack_id = int(parts[3])
            field = parts[4]
            prompts = {
                "title_ru": "Отправь новое название.",
                "author_ru": "Отправь автора (можно пусто: '-').",
                "description_ru": "Отправь описание (можно несколько строк).",
                "source_url": "Отправь ссылку-источник (или '-' чтобы очистить).",
                "tags": "Отправь теги через запятую.\nПример: сон, стресс, восстановление",
            }
            if field not in prompts:
                return
            await show(
                update,
                context,
                prompts[field],
                InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"admin:stack:open:{stack_id}")]]),
                route="admin:stack:edit_wait",
                payload={"stack_id": stack_id, "field": field},
            )
            return

        if data.startswith("admin:stack:items:"):
            parts = data.split(":")
            stack_id = int(parts[3])
            page = int(parts[4]) if len(parts)>4 and parts[4].isdigit() else 0
            await _show_admin_stack_items(update, context, conn, stack_id, page)
            return

        if data.startswith("admin:stack:item:open:"):
            parts = data.split(":")
            stack_id, item_id = int(parts[4]), int(parts[5])
            page = int(parts[6]) if len(parts)>6 else 0
            await _show_admin_stack_item(update, context, conn, stack_id, item_id, page)
            return

        if data.startswith("admin:stack:item:del:"):
            _, _, _, _, stack_id, item_id = data.split(":")
            await db.admin_delete_curated_stack_item(conn, int(item_id))
            await q.answer("Удалено.", show_alert=False)
            await _show_admin_stack_items(update, context, conn, int(stack_id))
            return

        if data.startswith("admin:stack:item:move:"):
            _, _, _, _, stack_id, item_id, direction = data.split(":")
            await db.admin_move_curated_stack_item(conn, int(stack_id), int(item_id), direction)
            await _show_admin_stack_items(update, context, conn, int(stack_id))
            return

        if data.startswith("admin:stack:item:edit:"):
            _, _, _, _, stack_id, item_id = data.split(":")
            await show(
                update,
                context,
                "Отправь одной строкой:\nДоза | Время | Заметка(опц.)\nПример: 500 мг | утром с едой | 8 недель",
                InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"admin:stack:item:open:{stack_id}:{item_id}")]]),
                route="admin:stack:item_edit_wait",
                payload={"stack_id": int(stack_id), "item_id": int(item_id)},
            )
            return

        if data.startswith("admin:stack:item:add:"):
            stack_id = int(data.split(":")[-1])
            await show(
                update,
                context,
                "Напиши, что добавить (поиск по каталогу).\nПример: магний",
                InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"admin:stack:items:{stack_id}")]]),
                route="admin:stack:item_search_wait",
                payload={"stack_id": stack_id, "query": "", "page": 0},
            )
            return

        if data.startswith("admin:stack:item:search_page:"):
            _, _, _, _, stack_id, page = data.split(":")
            _, _, ui_payload = await db.get_ui_state(conn, tg_uid)
            try:
                payload_data = json.loads(ui_payload) if ui_payload else {}
            except json.JSONDecodeError:
                payload_data = {}
            query = str(payload_data.get("query") or "").strip()
            page_i = max(0, int(page))
            rows, total = await db.search_supplements(conn, query, page_i, page_size=10)
            has_next = (page_i + 1) * 10 < int(total)
            await show(
                update,
                context,
                "🔎 <b>Поиск по каталогу</b>\nВыбери добавку для стека.",
                admin_stack_search_kb(int(stack_id), rows, page_i, has_prev=page_i > 0, has_next=has_next),
                route="admin:stack:item_search",
                payload={"stack_id": int(stack_id), "query": query, "page": page_i},
            )
            return

        if data.startswith(("admin:stack:item:pick:", "admin:stack:item:pick_id:")):
            parts = data.split(":", 5)
            if len(parts) != 6:
                await show(update, context, "Некорректная команда выбора добавки.", back(), route="admin:stacks")
                return
            _, _, _, action, stack_id, value = parts
            if not await db.admin_get_curated_stack_by_id(conn, int(stack_id)):
                await show(update, context, "Стек не найден.", admin_stacks_kb(), route="admin:stacks")
                return
            supplement = await db.get_supplement_by_id(conn, int(value)) if action == 'pick_id' and value.isdigit() else await db.get_supplement_by_slug(conn, value) if action == 'pick' else None
            if not supplement:
                await show(update, context, "Добавка не найдена.", back(), route=f"admin:stack:items:{stack_id}")
                return
            name = str(supplement["name_ru"] or supplement["name_en"] or supplement["slug"])
            slug = str(supplement['slug'])
            await show(
                update,
                context,
                f"Добавляем: <b>{html.escape(name)}</b>\nОтправь:\nДоза | Время | Заметка(опц.)",
                InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"admin:stack:items:{stack_id}")]]),
                route="admin:stack:item_details_wait",
                payload={"stack_id": int(stack_id), "slug": slug, "name": name},
            )
            return

        if data == "menu:today" or data == "today:home":
            await _show_today_home(update, context, conn, tg_uid)
            return

        if data == "today:slots":
            await _show_today_slots(update, context, conn, tg_uid)
            return

        if data.startswith("today:slot:"):
            slot_id = data.split(":", 2)[2]
            await _show_slot_screen(update, context, conn, tg_uid, slot_id)
            return

        if data.startswith("today:toggle:"):
            supplement_id_raw = data.split(":", 2)[2]
            if not supplement_id_raw.isdigit():
                await q.answer("Некорректный supplement_id", show_alert=False)
                return
            supplement_id = int(supplement_id_raw)
            tz_name = await db.get_user_tz(conn, tg_uid)
            date_local = _today_date_for_user(tz_name)
            current = await db.get_day_state_map(conn, tg_uid, date_local)
            cur_status = current.get(supplement_id)
            if cur_status == "taken":
                next_status = "skipped"
                msg = "Отмечено: пропущено"
            elif cur_status == "skipped":
                next_status = "none"
                msg = "Статус сброшен"
            else:
                next_status = "taken"
                msg = "Отмечено: принято"
            await db.set_day_state(conn, tg_uid, date_local, supplement_id, next_status)
            await q.answer(msg, show_alert=False)
            await _show_today_slots(update, context, conn, tg_uid)
            return

        if data.startswith("intake:all:"):
            slot_id = data.split(":", 2)[2]
            date_local, slots = await _today_slots_payload(conn, tg_uid)
            slot = _find_today_slot(slots, slot_id)
            for item in _slot_intake_items(slot):
                await db.set_day_state(conn, tg_uid, date_local, int(item.get("supplement_id") or 0), "taken")
            await q.answer("Отмечено: выпил всё", show_alert=False)
            await _show_slot_screen(update, context, conn, tg_uid, slot_id)
            return

        if data.startswith("intake:skip:"):
            slot_id = data.split(":", 2)[2]
            date_local, slots = await _today_slots_payload(conn, tg_uid)
            slot = _find_today_slot(slots, slot_id)
            for item in _slot_intake_items(slot):
                await db.set_day_state(conn, tg_uid, date_local, int(item.get("supplement_id") or 0), "skipped")
            await q.answer("Отмечено: пропустить всё", show_alert=False)
            await _show_slot_screen(update, context, conn, tg_uid, slot_id)
            return

        if data.startswith("intake:pick:"):
            parts = data.split(":")
            slot_id = parts[2] if len(parts) > 2 else ""
            page = int(parts[3]) if len(parts) > 3 and str(parts[3]).isdigit() else 0
            await _show_intake_picker(update, context, conn, tg_uid, slot_id, page)
            return

        if data.startswith("intake:toggle:"):
            parts = data.split(":")
            if len(parts) < 5:
                await q.answer("Некорректные данные", show_alert=False)
                return
            slot_id = parts[2]
            supplement_id = int(parts[3]) if str(parts[3]).isdigit() else 0
            page = int(parts[4]) if str(parts[4]).isdigit() else 0
            tz_name = await db.get_user_tz(conn, tg_uid)
            date_local = _today_date_for_user(tz_name)
            state_map = await db.get_day_state_map(conn, tg_uid, date_local)
            next_status = "none" if state_map.get(supplement_id) == "taken" else "taken"
            await db.set_day_state(conn, tg_uid, date_local, supplement_id, next_status)
            await q.answer("Обновлено", show_alert=False)
            await _show_intake_picker(update, context, conn, tg_uid, slot_id, page)
            return

        if data.startswith("intake:save:"):
            slot_id = data.split(":")[2] if len(data.split(":")) > 2 else ""
            await q.answer("Сохранено", show_alert=False)
            await _show_slot_screen(update, context, conn, tg_uid, slot_id)
            return

        if data == "rem:home":
            await _show_reminders_home(update, context, conn, tg_uid)
            return

        if data == "rem:edit":
            user_id = await db.get_or_create_user(conn, tg_uid, tz_name="")
            slots = await db.get_user_slots(conn, user_id)
            rows = [[InlineKeyboardButton(f"{slot.get('title_ru', 'Слот')} · {slot.get('time_local', '--:--')}", callback_data=f"rem:set:{slot.get('slot_id','')}")] for slot in slots]
            rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="rem:home")])
            await show(update, context, "Выбери слот и отправь время в формате HH:MM.", InlineKeyboardMarkup(rows), route="rem:edit")
            return

        if data.startswith("rem:set:"):
            slot_id = data.split(":", 2)[2]
            context.user_data[WAITING_KEY] = f"rem:set:{slot_id}"
            await show(update, context, "Напиши новое время в формате HH:MM.", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="rem:home")]]), route=f"rem:set:{slot_id}")
            return

        if data.startswith("rem:toggle:"):
            parts = data.split(":", 2)[2].rsplit(":", 1)
            slot_id = parts[0]
            desired = parts[1] == "1" if len(parts) == 2 and parts[1] in {"0", "1"} else None
            user_id = await db.get_or_create_user(conn, tg_uid, tz_name="")
            try:
                await db.set_user_slot_enabled(conn, user_id, slot_id, desired)
            except ValueError:
                await show(update, context, "Слот уже удалён. Обнови список напоминаний.", InlineKeyboardMarkup([[InlineKeyboardButton("⏰ Напоминания", callback_data="rem:home")]]), route="rem:home")
                return
            await _show_reminders_home(update, context, conn, tg_uid)
            return

        if data == "menu:my":
            await _show_my_list(update, context, conn, tg_uid, 0)
            return

        if data == "menu:compat":
            context.user_data[WAITING_KEY] = "compat_query"
            await show(
                update,
                context,
                "Введи добавку, чтобы показать совместимость (синергии/антагонисты).",
                back(),
                route="menu:compat",
            )
            return

        if data.startswith("compat_pick:"):
            slug = data.split(":", 1)[1]
            row = await db.get_supplement_by_slug(conn, slug)
            if not row:
                await show(update, context, "Не нашёл в каталоге.", back(), route="menu:compat")
                return
            await _show_supplement_compat(update, context, conn, int(row['id']), 'synergy', 0, back_data='menu:compat')
            return

        if data.startswith("add_pick:"):
            slug = data.split(":", 1)[1]
            row = await db.get_supplement_by_slug(conn, slug)
            if row:
                supplement_id = int(row["id"])
                context.user_data["add_to_stack_pending"] = {
                    "mode": "mysupp_hub",
                    "supplement_id": supplement_id,
                    "back_cb": "menu:add",
                }
                await _show_add_to_stack_slot_picker(update, context, conn, tg_uid, supplement_id)
            else:
                await show(update, context, "Не нашёл в каталоге.", back(), route="menu:add")
            return

        if data == "noop":
            return

        if data.startswith("cat:page:"):
            parts = data.split(":")
            if len(parts) == 3:
                try:
                    page = int(parts[2])
                except ValueError:
                    page = None
                if page is not None:
                    await render_catalog(update, context, conn, page)
                    return

        if data.startswith("cat:open:"):
            parts = data.split(":")
            if len(parts) == 4:
                try:
                    supplement_id = int(parts[2])
                    return_page = int(parts[3])
                except ValueError:
                    supplement_id = None
                if supplement_id is not None:
                    await render_supplement_card(
                        update,
                        context,
                        conn,
                        supplement_id,
                        return_page,
                        tab="brief",
                        page=0,
                    )
                    return

        if data == "cat:home":
            await _show_catalog_home(update, context, conn)
            return

        if data == "cat:goal":
            await _show_catalog_class_picker(update, context, conn, 0)
            return

        if data == "cat:organ":
            await _show_catalog_body_picker(update, context, "benefit")
            return

        if data in {"cat:search", "cat:mode:search"}:
            _, _, ui_payload = await db.get_ui_state(conn, tg_uid)
            try:
                payload_data = json.loads(ui_payload) if ui_payload else {}
            except json.JSONDecodeError:
                payload_data = {}
            if not isinstance(payload_data, dict):
                payload_data = {}
            return_page = payload_data.get("return_page")
            if return_page is None:
                return_page = payload_data.get("page", 0)
            await _show_catalog_search_prompt(
                update,
                context,
                conn,
                return_page=int(return_page or 0),
            )
            return

        if data == "cat:mode:evidence":
            await _show_catalog_list(update, context, conn, tg_uid, {
                "mode": "list",
                "list_mode": "evidence",
                "page": 0,
                "query": "",
                "class_key": "",
                "body_token": "",
                "return_page": 0,
            })
            return

        if data == "cat:mode:list":
            await _show_catalog_list(update, context, conn, tg_uid, {
                "mode": "list",
                "list_mode": "alpha",
                "page": 0,
                "query": "",
                "class_key": "",
                "body_token": "",
                "return_page": 0,
            })
            return

        if data.startswith("cat:list:"):
            try:
                page = int(data.split(":")[-1])
            except ValueError:
                page = 0
            await _show_catalog_list(update, context, conn, tg_uid, {
                "mode": "list",
                "list_mode": "alpha",
                "page": max(page, 0),
                "query": "",
                "class_key": "",
                "body_token": "",
                "return_page": max(page, 0),
            })
            return

        if data == "cat:mode:class":
            await _show_catalog_class_picker(update, context, conn, 0)
            return

        if data == "cat:mode:type":
            await _show_catalog_type_picker(update, context)
            return

        if data == "cat:mode:body:benefit":
            await _show_catalog_body_picker(update, context, "benefit")
            return

        if data == "cat:mode:body:burden":
            await _show_catalog_body_picker(update, context, "burden")
            return

        if data == "cat:mode:group":
            await _show_catalog_group_picker(update, context)
            return

        if data.startswith("cat:class:page:"):
            page = int(data.split(":")[-1])
            await _show_catalog_class_picker(update, context, conn, page)
            return

        if data.startswith("cat:class:pick:"):
            class_key = data.split(":", 3)[-1]
            await _show_catalog_list(update, context, conn, tg_uid, {
                "mode": "list",
                "list_mode": "class",
                "page": 0,
                "query": "",
                "class_key": class_key,
                "body_token": "",
                "return_page": 0,
            })
            return

        if data.startswith("cat:organ:pick:"):
            token = data.split(":", 3)[-1]
            await _show_catalog_list(update, context, conn, tg_uid, {
                "mode": "list",
                "list_mode": "body_benefit",
                "page": 0,
                "query": "",
                "class_key": "",
                "body_token": token,
                "return_page": 0,
            })
            return

        if data.startswith("cat:type:"):
            token = data.split(":", 2)[-1]
            await _show_catalog_list(update, context, conn, tg_uid, {
                "mode": "list",
                "list_mode": "type",
                "page": 0,
                "query": "",
                "class_key": "",
                "type_token": token,
                "body_token": "",
                "return_page": 0,
            })
            return

        if data.startswith("cat:body:benefit:"):
            token = data.split(":", 3)[-1]
            await _show_catalog_list(update, context, conn, tg_uid, {
                "mode": "list",
                "list_mode": "body_benefit",
                "page": 0,
                "query": "",
                "class_key": "",
                "body_token": token,
                "return_page": 0,
            })
            return

        if data.startswith("cat:body:burden:"):
            token = data.split(":", 3)[-1]
            await _show_catalog_list(update, context, conn, tg_uid, {
                "mode": "list",
                "list_mode": "body_burden",
                "page": 0,
                "query": "",
                "class_key": "",
                "body_token": token,
                "return_page": 0,
            })
            return

        if data.startswith("cat:group:"):
            token = data.split(":", 2)[-1]
            await _show_catalog_list(update, context, conn, tg_uid, {
                "mode": "list",
                "list_mode": "group",
                "page": 0,
                "query": "",
                "class_key": "",
                "group_token": token,
                "body_token": "",
                "return_page": 0,
            })
            return

        if data == "cat:page:prev":
            payload = await _load_catalog_payload(conn, tg_uid)
            payload["page"] = max(0, int(payload.get("page") or 0) - 1)
            await _show_catalog_list(update, context, conn, tg_uid, payload)
            return

        if data == "cat:page:next":
            payload = await _load_catalog_payload(conn, tg_uid)
            payload["page"] = int(payload.get("page") or 0) + 1
            await _show_catalog_list(update, context, conn, tg_uid, payload)
            return

        if data.startswith("cat:sres:"):
            page = max(int(data.split(":")[-1]), 0)
            _, _, ui_payload = await db.get_ui_state(conn, tg_uid)
            try:
                payload_data = json.loads(ui_payload) if ui_payload else {}
            except json.JSONDecodeError:
                payload_data = {}
            if not isinstance(payload_data, dict):
                payload_data = {}
            query = str(payload_data.get("query") or "").strip()
            return_page = int(payload_data.get("return_page") or 0)
            if not query:
                await _show_catalog_search_prompt(
                    update,
                    context,
                    conn,
                    return_page=return_page,
                )
                return
            await render_search_results(
                update,
                context,
                conn,
                tg_uid=tg_uid,
                query=query,
                page=page,
                return_page=return_page,
            )
            return

        if data.startswith("cat:sopen:"):
            parts = data.split(":")
            if len(parts) >= 4:
                supplement_id = int(parts[2])
                page = max(int(parts[3]), 0)
                _, _, ui_payload = await db.get_ui_state(conn, tg_uid)
                try:
                    payload_data = json.loads(ui_payload) if ui_payload else {}
                except json.JSONDecodeError:
                    payload_data = {}
                if not isinstance(payload_data, dict):
                    payload_data = {}
                query = str(payload_data.get("query") or "").strip()
                await render_supplement_card(
                    update,
                    context,
                    conn,
                    supplement_id,
                    page,
                    tab="brief",
                    page=0,
                    return_mode="search",
                    search_query=query or None,
                )
            return

        if data.startswith("supp:tab:"):
            _, _, sid, tab = data.split(":", 3)
            _, last_screen, ui_payload = await db.get_ui_state(conn, tg_uid)
            try:
                payload_data = json.loads(ui_payload) if ui_payload else {}
            except json.JSONDecodeError:
                payload_data = {}
            if not isinstance(payload_data, dict):
                payload_data = {}
            if last_screen != "supp_card":
                payload_data = {}
            return_page = int(payload_data.get("return_page") or 0)
            return_mode = payload_data.get("return_mode") or "catalog"
            search_query = payload_data.get("query")
            prev_payload = payload_data.get("prev_payload") if return_mode == "supp_back" else None
            await render_supplement_card(
                update,
                context,
                conn,
                int(sid),
                return_page,
                tab=tab,
                page=0,
                return_mode=return_mode,
                search_query=search_query,
                prev_payload=prev_payload,
            )
            return

        if data.startswith("supp:page:"):
            _, _, sid, tab, page_text = data.split(":", 4)
            _, last_screen, ui_payload = await db.get_ui_state(conn, tg_uid)
            try:
                payload_data = json.loads(ui_payload) if ui_payload else {}
            except json.JSONDecodeError:
                payload_data = {}
            if not isinstance(payload_data, dict):
                payload_data = {}
            if last_screen != "supp_card":
                payload_data = {}
            return_page = int(payload_data.get("return_page") or 0)
            return_mode = payload_data.get("return_mode") or "catalog"
            search_query = payload_data.get("query")
            prev_payload = payload_data.get("prev_payload") if return_mode == "supp_back" else None
            await render_supplement_card(
                update,
                context,
                conn,
                int(sid),
                return_page,
                tab=tab,
                page=int(page_text),
                return_mode=return_mode,
                search_query=search_query,
                prev_payload=prev_payload,
            )
            return

        if data == "supp:back":
            _, last_screen, ui_payload = await db.get_ui_state(conn, tg_uid)
            try:
                payload_data = json.loads(ui_payload) if ui_payload else {}
            except json.JSONDecodeError:
                payload_data = {}
            if not isinstance(payload_data, dict) or last_screen != "supp_card":
                payload_data = {}
            prev_payload = payload_data.get("prev_payload")
            if isinstance(prev_payload, dict):
                await render_supplement_card(
                    update,
                    context,
                    conn,
                    int(prev_payload.get("id")),
                    int(prev_payload.get("return_page") or 0),
                    tab=prev_payload.get("tab") or "brief",
                    page=int(prev_payload.get("page") or 0),
                    return_mode=prev_payload.get("return_mode") or "catalog",
                    search_query=prev_payload.get("query"),
                    prev_payload=prev_payload.get("prev_payload"),
                )
                return
            await render_catalog(update, context, conn, 0)
            return

        if data == "cat:back":
            payload = await _load_catalog_payload(conn, tg_uid)
            if payload.get("mode") == "list":
                await _show_catalog_list(update, context, conn, tg_uid, payload)
            else:
                await _show_catalog_home(update, context, conn)
            return

        if data.startswith("cat:open:"):
            supplement_id = int(data.split(":")[-1])
            await _show_catalog_card(update, context, conn, tg_uid, supplement_id)
            return

        if data.startswith("curated:page:"):
            page = int(data.split(":")[-1])
            await _show_curated_page(update, context, conn, page)
            return

        if data.startswith("curated:detail_page:"):
            _, _, slug, page = data.split(":", 3)
            await _show_curated_stack(update, context, conn, slug, page=int(page))
            return
        if data.startswith("curated:open:"):
            slug = data.split(":", 2)[-1]
            await _show_curated_stack(update, context, conn, slug)
            return

        if data.startswith("curated:add:"):
            slug = data.split(":", 2)[-1]
            stack = await db.get_curated_stack(conn, slug)
            if not stack:
                await show(update, context, "Стек не найден.", back(), route="menu:curated")
                return
            async with db.transaction(conn) as tx:
                items = await db.list_curated_stack_items(tx, int(stack["id"]))
                not_found: list[str] = []
                already_added: list[str] = []
                to_add: list[dict] = []
                changed_slugs: set[str] = set()
                existing_ids = await db.get_user_stack_supplement_ids(tx, tg_uid)
                for item in items:
                    supplement = await db.find_supplement_by_slug_or_name(
                        tx,
                        item["supplement_slug"],
                        item["supplement_name"],
                    )
                    if not supplement:
                        name = (
                            str(item["supplement_name"] or "").strip()
                            or str(item["supplement_slug"] or "").strip()
                            or "Без названия"
                        )
                        not_found.append(name)
                        continue
                    supplement_id = int(supplement["id"])
                    if supplement_id in existing_ids:
                        already_added.append(str(supplement["name_ru"] or supplement["name_en"] or supplement["slug"] or "Без названия"))
                        continue
                    to_add.append(
                        {
                            "supplement_id": supplement_id,
                            "dose_ru": item["dose_ru"],
                            "timing_ru": item["timing_ru"],
                            "notes_ru": item["notes_ru"],
                        }
                    )
                    existing_ids.add(supplement_id)
                    changed_slugs.add(str(supplement["slug"] or "").strip())
                internal_user_id = await db.get_or_create_user(tx, tg_uid, tz_name="")
                summary = await db.add_stack_items_to_user(tx, internal_user_id, to_add)
                if changed_slugs:
                    await db.enqueue_stack_change_rebuilds(tx, tg_uid, sorted(slug for slug in changed_slugs if slug))
            lines = [f"✅ Добавлено новых: {summary['added']}"]
            lines.append(f"ℹ️ Уже были в ваших БАДах: {len(already_added)}")
            if not_found:
                lines.append(f"⚠️ Не найдено в каталоге: {len(not_found)}")
                lines.extend([f"• {name}" for name in not_found[:10]])
                if len(not_found) > 10:
                    lines.append("…и ещё")
            else:
                lines.append("⚠️ Не найдено в каталоге: 0")
            if already_added:
                lines.append("")
                lines.append("Уже были добавлены:")
                lines.extend([f"• {name}" for name in already_added[:10]])
                if len(already_added) > 10:
                    lines.append("…и ещё")
            lines.append("")
            lines.append("<i>Это не медсовет.</i>")
            back_kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Назад", callback_data=f"curated:open:{slug}")]]
            )
            await show(
                update,
                context,
                "\n".join(lines),
                back_kb,
                route=f"curated:add:{slug}",
            )
            return

        if data.startswith("supp:openai:"):
            supplement_id = int(data.split(":")[-1])
            await render_supplement_card(
                update,
                context,
                conn,
                supplement_id,
                0,
                tab="brief",
                page=0,
                return_mode="onboard_back",
            )
            return

        if data.startswith("supp:open:"):
            supplement_id = int(data.split(":")[-1])
            _, last_screen, ui_payload = await db.get_ui_state(conn, tg_uid)
            try:
                payload_data = json.loads(ui_payload) if ui_payload else {}
            except json.JSONDecodeError:
                payload_data = {}
            if not isinstance(payload_data, dict):
                payload_data = {}
            if last_screen == "supp_card":
                return_page = int(payload_data.get("return_page") or 0)
                await render_supplement_card(
                    update,
                    context,
                    conn,
                    supplement_id,
                    return_page,
                    tab="brief",
                    page=0,
                    return_mode="supp_back",
                    prev_payload=payload_data,
                )
            else:
                back_route = context.user_data.get("last_route")
                if isinstance(back_route, str) and back_route.startswith("supp:compat:"):
                    parts = back_route.split(":")
                    if len(parts) >= 3:
                        parent_id = parts[2]
                        if parent_id.isdigit():
                            back_route = f"supp:open:{parent_id}"
                elif isinstance(back_route, str) and back_route.startswith("card:compat:"):
                    parts = back_route.split(":")
                    if len(parts) >= 3:
                        parent_id = parts[2]
                        if parent_id.isdigit():
                            back_route = f"cat:open:{parent_id}"
                await _show_supplement_page(update, context, conn, tg_uid, supplement_id, back_route=back_route)
            return

        if data.startswith("card:add:"):
            supplement_id = int(data.split(":")[-1])
            context.user_data["add_to_stack_pending"] = {
                "mode": "card",
                "supplement_id": supplement_id,
                "back_cb": f"cat:open:{supplement_id}",
            }
            await _show_add_to_stack_slot_picker(update, context, conn, tg_uid, supplement_id)
            return

        if data.startswith("card:remove:"):
            supplement_id = int(data.split(":")[-1])
            await db.remove_user_supplement(conn, tg_uid, supplement_id)
            await _show_catalog_card(update, context, conn, tg_uid, supplement_id)
            return

        if data.startswith("card:compat:"):
            parts = data.split(":")
            supplement_id = int(parts[2])
            kind = "synergy"
            page = 0
            if len(parts) >= 5:
                kind = parts[3]
                page = int(parts[4])
            await _show_supplement_compat(
                update,
                context,
                conn,
                supplement_id,
                kind,
                page,
                back_data=f"cat:open:{supplement_id}",
                route_prefix=f"card:compat:{supplement_id}",
            )
            return

        if data.startswith("card:evidence:"):
            supplement_id = int(data.split(":")[-1])
            await _show_catalog_evidence(update, context, conn, tg_uid, supplement_id)
            return

        if data.startswith("supp:view:"):
            _, _, slug, page = data.split(":", 3)
            row = await db.get_supplement_by_slug(conn, slug)
            if row:
                back_route = f"cat:page:{int(page)}"
                await _show_supplement_page(update, context, conn, tg_uid, int(row["id"]), back_route=back_route)
            return

        if data.startswith("supp:compat:"):
            _, _, sid, kind, page = data.split(":", 4)
            await _show_supplement_compat(update, context, conn, int(sid), kind, int(page))
            return

        if data.startswith("mysupp:list:"):
            parts = data.split(":")
            page = int(parts[2])
            part = int(parts[3]) if len(parts) > 3 else 0
            await _show_my_list(update, context, conn, tg_uid, page, part=part)
            return

        if data.startswith("mysupp:add:") and ":retcat:" in data:
            parts = data.split(":")
            if len(parts) >= 5:
                try:
                    supplement_id = int(parts[2])
                    return_page = int(parts[4])
                except ValueError:
                    supplement_id = None
                if supplement_id is not None:
                    _, _, ui_payload = await db.get_ui_state(conn, tg_uid)
                    try:
                        payload_data = json.loads(ui_payload) if ui_payload else {}
                    except json.JSONDecodeError:
                        payload_data = {}
                    if not isinstance(payload_data, dict):
                        payload_data = {}
                    tab = payload_data.get("tab") or "brief"
                    page = int(payload_data.get("page") or 0)
                    prev_payload = payload_data.get("prev_payload") if payload_data.get("return_mode") == "supp_back" else None
                    context.user_data["add_to_stack_pending"] = {
                        "mode": "supp_card",
                        "supplement_id": supplement_id,
                        "return_tag": "retcat",
                        "return_page": return_page,
                        "tab": tab,
                        "page": page,
                        "return_mode": payload_data.get("return_mode") or "catalog",
                        "query": payload_data.get("query"),
                        "prev_payload": prev_payload,
                        "back_cb": "supp:back",
                    }
                    await _show_add_to_stack_slot_picker(update, context, conn, tg_uid, supplement_id)
                    return

        if data.startswith("mysupp:add:") and ":retsearch:" in data:
            parts = data.split(":")
            if len(parts) >= 5:
                try:
                    supplement_id = int(parts[2])
                    return_page = int(parts[4])
                except ValueError:
                    supplement_id = None
                if supplement_id is not None:
                    _, _, ui_payload = await db.get_ui_state(conn, tg_uid)
                    try:
                        payload_data = json.loads(ui_payload) if ui_payload else {}
                    except json.JSONDecodeError:
                        payload_data = {}
                    if not isinstance(payload_data, dict):
                        payload_data = {}
                    query = str(payload_data.get("query") or "").strip()
                    tab = payload_data.get("tab") or "brief"
                    page = int(payload_data.get("page") or 0)
                    prev_payload = payload_data.get("prev_payload") if payload_data.get("return_mode") == "supp_back" else None
                    context.user_data["add_to_stack_pending"] = {
                        "mode": "supp_card",
                        "supplement_id": supplement_id,
                        "return_tag": "retsearch",
                        "return_page": return_page,
                        "tab": tab,
                        "page": page,
                        "query": query,
                        "prev_payload": prev_payload,
                        "back_cb": "supp:back",
                    }
                    await _show_add_to_stack_slot_picker(update, context, conn, tg_uid, supplement_id)
                    return

        if data.startswith("mysupp:add:") and ":retai" in data:
            parts = data.split(":")
            if len(parts) >= 4:
                try:
                    supplement_id = int(parts[2])
                except ValueError:
                    supplement_id = None
                if supplement_id is not None:
                    _, _, ui_payload = await db.get_ui_state(conn, tg_uid)
                    try:
                        payload_data = json.loads(ui_payload) if ui_payload else {}
                    except json.JSONDecodeError:
                        payload_data = {}
                    if not isinstance(payload_data, dict):
                        payload_data = {}
                    tab = payload_data.get("tab") or "brief"
                    page = int(payload_data.get("page") or 0)
                    context.user_data["add_to_stack_pending"] = {
                        "mode": "supp_card",
                        "supplement_id": supplement_id,
                        "return_tag": "retai",
                        "tab": tab,
                        "page": page,
                        "back_cb": "supp:back",
                    }
                    await _show_add_to_stack_slot_picker(update, context, conn, tg_uid, supplement_id)
                    return

        if data.startswith("mysupp:add:") and ":ret_onboard" in data:
            parts = data.split(":")
            if len(parts) >= 4:
                try:
                    supplement_id = int(parts[2])
                except ValueError:
                    supplement_id = None
                if supplement_id is not None:
                    _, _, ui_payload = await db.get_ui_state(conn, tg_uid)
                    try:
                        payload_data = json.loads(ui_payload) if ui_payload else {}
                    except json.JSONDecodeError:
                        payload_data = {}
                    if not isinstance(payload_data, dict):
                        payload_data = {}
                    tab = payload_data.get("tab") or "brief"
                    page = int(payload_data.get("page") or 0)
                    context.user_data["add_to_stack_pending"] = {
                        "mode": "supp_card",
                        "supplement_id": supplement_id,
                        "return_tag": "ret_onboard",
                        "tab": tab,
                        "page": page,
                        "back_cb": "supp:back",
                    }
                    await _show_add_to_stack_slot_picker(update, context, conn, tg_uid, supplement_id)
                    return

        if data.startswith("mysupp:add:") and ":retback" in data:
            parts = data.split(":")
            if len(parts) >= 4:
                try:
                    supplement_id = int(parts[2])
                except ValueError:
                    supplement_id = None
                if supplement_id is not None:
                    _, _, ui_payload = await db.get_ui_state(conn, tg_uid)
                    try:
                        payload_data = json.loads(ui_payload) if ui_payload else {}
                    except json.JSONDecodeError:
                        payload_data = {}
                    if not isinstance(payload_data, dict):
                        payload_data = {}
                    tab = payload_data.get("tab") or "brief"
                    page = int(payload_data.get("page") or 0)
                    return_page = int(payload_data.get("return_page") or 0)
                    prev_payload = payload_data.get("prev_payload")
                    context.user_data["add_to_stack_pending"] = {
                        "mode": "supp_card",
                        "supplement_id": supplement_id,
                        "return_tag": "retback",
                        "return_page": return_page,
                        "tab": tab,
                        "page": page,
                        "prev_payload": prev_payload,
                        "back_cb": "supp:back",
                    }
                    await _show_add_to_stack_slot_picker(update, context, conn, tg_uid, supplement_id)
                    return

        if data == "mysupp:add":
            context.user_data[WAITING_KEY] = "mysupp_add_query"
            await show(
                update,
                context,
                "Введи название БАДА для добавления.",
                my_list_back(),
                route="mysupp:add",
            )
            return

        if data.startswith("mysupp:add_pick:"):
            slug = data.split(":", 2)[-1]
            row = await db.get_supplement_by_slug(conn, slug)
            if row:
                supplement_id = int(row["id"])
                context.user_data["add_to_stack_pending"] = {
                    "mode": "mysupp_hub",
                    "supplement_id": supplement_id,
                    "back_cb": "mysupp:add",
                }
                await _show_add_to_stack_slot_picker(update, context, conn, tg_uid, supplement_id)
            else:
                await show(update, context, "Не нашёл в каталоге.", my_list_back(), route="mysupp:add")
            return

        if data.startswith("mysupp:add_direct:"):
            supplement_id = int(data.split(":")[-1])
            context.user_data["add_to_stack_pending"] = {
                "mode": "mysupp_hub",
                "supplement_id": supplement_id,
                "back_cb": f"mysupp:open:{supplement_id}",
            }
            await _show_add_to_stack_slot_picker(update, context, conn, tg_uid, supplement_id)
            return

        if data.startswith("mysupp:addslot:pick:"):
            parts = data.split(":")
            if len(parts) >= 5:
                supplement_id = int(parts[3])
                slot_id = str(parts[4] or "").strip()
                if slot_id:
                    await _complete_add_to_stack_with_slot(update, context, conn, tg_uid, supplement_id, slot_id)
            return

        if data.startswith("mysupp:addslot:newtime:"):
            parts = data.split(":")
            if len(parts) >= 4:
                supplement_id = int(parts[3])
                context.user_data[WAITING_KEY] = f"mysupp:addslot:newtime:{supplement_id}"
                back_cb = f"mysupp:addslot:{supplement_id}"
                await show(
                    update,
                    context,
                    "Введите время нового слота в формате HH:MM.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=back_cb)]]),
                    route=f"mysupp:addslot:newtime:{supplement_id}",
                )
            return

        if data.startswith("mysupp:addslot:") and data.count(":") == 2:
            supplement_id = int(data.split(":")[-1])
            await _show_add_to_stack_slot_picker(update, context, conn, tg_uid, supplement_id)
            return

        if data.startswith("mysupp:open:") or data.startswith("mysupp:hub:"):
            parts = data.split(":")
            supplement_id = int(parts[2])
            part = int(parts[3]) if len(parts) > 3 else 0
            await show_my_supp_hub(update, context, tg_uid, supplement_id, conn=conn, part=part)
            return

        if data.startswith("mysupp:quick:") or data.startswith("mysupp:aiquick:"):
            parts = data.split(":")
            if len(parts) >= 5:
                action = parts[2]
                supplement_id = int(parts[3])
                page = int(parts[4])
                if action == "taken":
                    await db.set_today_status(conn, tg_uid, supplement_id, "taken")
                elif action == "skip":
                    await db.set_today_status(conn, tg_uid, supplement_id, "skipped")
                await _show_my_list(update, context, conn, tg_uid, page)
            return

        if data.startswith("supplement:set_time:"):
            supplement_id = int(data.split(":", 2)[2])
            split_ctx = context.user_data.get("split_flow")
            split_origin_id = None
            if isinstance(split_ctx, dict):
                new_id = int(split_ctx.get("new_id") or 0)
                if new_id == supplement_id:
                    split_origin_id = int(split_ctx.get("origin_id") or 0) or None
            await _show_supplement_time_screen(
                update,
                context,
                conn,
                tg_uid,
                supplement_id,
                split_origin_id=split_origin_id,
            )
            return

        if data.startswith("supplement:set_slot:"):
            parts = data.split(":")
            if len(parts) >= 4:
                supplement_id = int(parts[2])
                slot_id = parts[3]
                user_id = await db.get_or_create_user(conn, tg_uid, tz_name="")
                try:
                    await _save_bot_supplement_schedule(conn, user_id, supplement_id, slot_id=slot_id)
                except ValueError:
                    await _show_supplement_time_screen(update, context, conn, tg_uid, supplement_id)
                    return
                split_ctx = context.user_data.get("split_flow")
                split_origin_id = None
                if isinstance(split_ctx, dict) and int(split_ctx.get("new_id") or 0) == supplement_id:
                    split_origin_id = int(split_ctx.get("origin_id") or 0) or None
                await _show_supplement_time_screen(update, context, conn, tg_uid, supplement_id, split_origin_id=split_origin_id)
            return

        if data.startswith("supplement:set_override:"):
            supplement_id = int(data.split(":", 2)[2])
            context.user_data[WAITING_KEY] = f"supplement:set_override:{supplement_id}"
            await show(update, context, "Введи время в формате HH:MM", my_hub_back(supplement_id), route=f"supplement:set_override:{supplement_id}")
            return

        if data.startswith("supplement:clear_override:"):
            supplement_id = int(data.split(":", 2)[2])
            user_id = await db.get_or_create_user(conn, tg_uid, tz_name="")
            try:
                await _save_bot_supplement_schedule(conn, user_id, supplement_id, set_override=True)
            except ValueError:
                await _show_supplement_time_screen(update, context, conn, tg_uid, supplement_id)
                return
            split_ctx = context.user_data.get("split_flow")
            split_origin_id = None
            if isinstance(split_ctx, dict) and int(split_ctx.get("new_id") or 0) == supplement_id:
                split_origin_id = int(split_ctx.get("origin_id") or 0) or None
            await _show_supplement_time_screen(update, context, conn, tg_uid, supplement_id, split_origin_id=split_origin_id)
            return

        if data.startswith("mysupp:set_dose:"):
            supplement_id = int(data.split(":")[-1])
            supplement = await db.get_supplement_by_id(conn, supplement_id)
            recommended_dose = "—"
            if supplement and "dose_ru" in supplement.keys():
                recommended_dose = (supplement["dose_ru"] or "").strip() or "—"
            context.user_data[WAITING_KEY] = f"mysupp:set_dose:{supplement_id}"
            await show(
                update,
                context,
                (
                    "Напиши дозу (до 80 символов).\n"
                    f"Рекомендуемая доза: {html.escape(recommended_dose)}"
                ),
                my_hub_back(supplement_id),
                route=f"mysupp:set_dose:{supplement_id}",
            )
            return

        if data.startswith("mysupp:mark:taken:"):
            supplement_id = int(data.split(":")[-1])
            await db.set_today_status(conn, tg_uid, supplement_id, "taken")
            await show_my_supp_hub(update, context, tg_uid, supplement_id, conn=conn)
            return

        if data.startswith("mysupp:mark:skip:"):
            supplement_id = int(data.split(":")[-1])
            await db.set_today_status(conn, tg_uid, supplement_id, "skipped")
            await show_my_supp_hub(update, context, tg_uid, supplement_id, conn=conn)
            return

        if data.startswith("mysupp:hist:"):
            parts = data.split(":")
            supplement_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            month_offset = int(parts[3]) if len(parts) > 3 and re.fullmatch(r"-?\d+", parts[3] or "") else 0
            if supplement_id <= 0:
                await q.answer("Некорректный БАД", show_alert=False)
                return
            await _show_my_supp_history(update, context, conn, tg_uid, supplement_id, month_offset)
            return

        if data.startswith("mysupp:split:") and not data.startswith("mysupp:split:cancel:") and not data.startswith("mysupp:split:apply:"):
            supplement_id = int(data.split(":")[-1])
            if context.user_data.get("split_busy"):
                return
            pending = context.user_data.get("split_flow") or {}
            new_id = int(pending.get("new_id") or 0)
            if int(pending.get("origin_id") or 0) == supplement_id and new_id:
                uid = await db.get_or_create_user(conn, tg_uid)
                exists = await db.fetchone(conn, "SELECT 1 FROM user_supplements WHERE user_id=? AND supplement_id=? AND is_active=1", (uid, new_id))
                if exists:
                    await _show_supplement_time_screen(update, context, conn, tg_uid, new_id, split_origin_id=supplement_id)
                    return
            context.user_data["split_busy"] = True
            try:
                result = await db.split_user_supplement_intake(conn, tg_uid, supplement_id)
                new_id = int(result.get("supplement_id") or 0)
                if new_id <= 0:
                    raise ValueError("missing split id")
                context.user_data["split_flow"] = {"origin_id": supplement_id, "new_id": new_id}
                await q.answer("Создан второй приём", show_alert=False)
                await _show_supplement_time_screen(
                    update,
                    context,
                    conn,
                    tg_uid,
                    new_id,
                    split_origin_id=supplement_id,
                )
            except Exception:
                await q.answer("Не удалось разделить приём", show_alert=False)
            finally:
                context.user_data.pop("split_busy", None)
            return

        if data.startswith("mysupp:split:cancel:"):
            _, _, _, new_id_raw, origin_id_raw = data.split(":", 4)
            new_id = int(new_id_raw)
            origin_id = int(origin_id_raw)
            await db.remove_user_supp(conn, tg_uid, new_id)
            await db.delete_reminders_for_supplement(conn, tg_uid, new_id)
            context.user_data.pop("split_flow", None)
            await q.answer("Второй приём отменён", show_alert=False)
            await show_my_supp_hub(update, context, tg_uid, origin_id, conn=conn)
            return

        if data.startswith("mysupp:split:apply:"):
            new_id = int(data.split(":")[-1])
            context.user_data.pop("split_flow", None)
            await q.answer("Второй приём сохранён", show_alert=False)
            await show_my_supp_hub(update, context, tg_uid, new_id, conn=conn)
            return

        if data.startswith("mysupp:remove:"):
            supplement_id = int(data.split(":")[-1])
            await db.remove_user_supp(conn, tg_uid, supplement_id)
            await db.delete_reminders_for_supplement(conn, tg_uid, supplement_id)
            await _show_my_list(update, context, conn, tg_uid, 0)
            return

        if data.startswith("mysupp:rem:del:"):
            parts = data.split(":")
            supplement_id, time_key = parts[3:5]
            page = int(parts[5]) if len(parts) > 5 else 0
            time_local = time_key.replace("-", ":")
            await db.delete_reminder(conn, tg_uid, int(supplement_id), time_local)
            await _show_my_supp_reminders(update, context, conn, tg_uid, int(supplement_id), page=page)
            return

        if data.startswith("mysupp:rem:add:"):
            supplement_id = int(data.split(":")[-1])
            context.user_data[WAITING_KEY] = f"mysupp:rem_add:{supplement_id}"
            await show(
                update,
                context,
                "Напиши время (например: 09:30, 9.30, 9 30 или 0930)",
                my_hub_back(supplement_id),
                route=f"mysupp:rem:add:{supplement_id}",
            )
            return

        if data.startswith("mysupp:rem:suggest:"):
            parts = data.split(":")
            if len(parts) >= 6:
                supplement_id = int(parts[3])
                time_local = parts[4].replace("-", ":")
                page = int(parts[5])
                try:
                    await db.add_reminder_for_supplement(conn, tg_uid, supplement_id, time_local)
                except ValueError:
                    await show(update, context, "Напоминание недоступно. Обнови список БАДов и выбери время заново.", my_list_back(), route="mysupp:list:0")
                    return
                await _show_my_supp_reminders(update, context, conn, tg_uid, supplement_id, page=page)
            return

        if data.startswith("mysupp:rem:page:"):
            parts = data.split(":")
            if len(parts) >= 5:
                supplement_id = int(parts[3])
                page = int(parts[4])
                await _show_my_supp_reminders(update, context, conn, tg_uid, supplement_id, page=page)
            return

        if data.startswith("mysupp:rem:"):
            supplement_id = int(data.split(":")[-1])
            await _show_my_supp_reminders(update, context, conn, tg_uid, supplement_id)
            return

        if data == "menu:checkin_ai":
            allowed = await require_premium_or_upsell(update, context, "AI check-in")
            if not allowed:
                return
            context.user_data.pop(WAITING_KEY, None)
            await _render_ai_checkin_entry(update, context)
            return

        if data == "menu:todayplan":
            await _show_today_plan(update, context, conn, tg_uid)
            return

        if data == "menu:ai":
            onboarding_state = await db.get_onboarding_state(conn, tg_uid)
            onboarding_status = str(onboarding_state.get("onboarding_status") or "")
            onboarding_raw = str(onboarding_state.get("onboarding_raw_json") or "")
            free_pick_used = int(onboarding_state.get("onboarding_free_pick_used") or 0)

            if onboarding_status in {"active", "results"} and onboarding_raw:
                restored = await _render_onboarding_results(update, context, conn)
                if not restored:
                    await home(update, context)
                return

            if free_pick_used == 0:
                if not s.OPENAI_API_KEY:
                    await show(
                        update,
                        context,
                        "Начальный подбор недоступен: добавь OPENAI_API_KEY в .env.",
                        back(),
                        route="menu:ai",
                    )
                    return
                await db.start_onboarding_state(conn, tg_uid)
                await _start_initial_pick(update, context, back_cb="tz:back")
                return

            await _show_ai_used_upsell(update, context)
            return

    finally:
        await conn.close()

    await home(update, context)

async def on_ai_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard_not_banned(update, context):
        return
    q = update.callback_query
    if not q:
        return
    await _safe_answer_callback(update)
    data = q.data or ""
    if not data.startswith("aiadd:"):
        return
    parts = data.split(":", 1)
    if len(parts) != 2:
        return
    try:
        supplement_id = int(parts[1])
    except ValueError:
        return

    s = context.application.bot_data["settings"]
    tg_uid = update.effective_user.id
    conn = await db.connect(s.DB_PATH)
    try:
        await db.add_user_supplement(conn, tg_uid, supplement_id)
        await db.add_user_initial_stack_id(conn, tg_uid, supplement_id)
        name = await db.get_supplement_name(conn, supplement_id)
        name = name or "добавка"
        await show(
            update,
            context,
            "✅ Добавлено в «Мои БАДы»: "
            f"<b>{name}</b>\n\n"
            "Хочешь добавить ещё из рекомендаций?",
            back(),
            route="menu:ai",
        )
    finally:
        await conn.close()

async def on_noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q:
        await _safe_answer_callback(update)

async def on_ai_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard_not_banned(update, context):
        return
    allowed = await require_premium_or_upsell(update, context, "AI check-in")
    if not allowed:
        return
    q = update.callback_query
    if not q:
        return
    await _safe_answer_callback(update)
    data = q.data or ""
    if not data.startswith("aicheck:"):
        return

    state = _get_ai_checkin_state(context)
    step = int(state.get("step") or 1)
    answers = state.get("answers", {}) if isinstance(state.get("answers"), dict) else {}

    if data == "aicheck:entry":
        context.user_data.pop(WAITING_KEY, None)
        _get_ai_checkin_target_day(context)
        await _render_ai_checkin_entry(update, context)
        return

    if data == "aicheck:voice":
        context.user_data.pop(WAITING_KEY, None)
        await _render_ai_checkin_voice_prompt(update, context)
        return

    if data == "aicheck:text":
        context.user_data[WAITING_KEY] = AI_CHECKIN_TEXT_WAIT
        await _render_ai_checkin_text_prompt(update, context)
        return

    if data.startswith("aicheck:target:"):
        next_target = data.split(":", 2)[2]
        _set_ai_checkin_target_day(context, next_target)
        state["target_day"] = _get_ai_checkin_target_day(context)
        _set_ai_checkin_state(context, state)
        await _render_ai_checkin_entry(update, context)
        return

    if data == "aicheck:quick":
        context.user_data.pop(WAITING_KEY, None)
        target_day = _get_ai_checkin_target_day(context)
        state = {
            "step": 1,
            "target_day": target_day,
            "answers": {
                "sleep_range": "",
                "energy": None,
                "stress": None,
                "training": None,
                "goals": [],
                "note": "",
            },
            "note_pending": False,
            "last_ai_actions_json": "",
            "selected_action_ids": [],
        }
        _set_ai_checkin_state(context, state)
        await _render_ai_checkin_wizard(update, context, state=state)
        return

    if data == "aicheck:note":
        state["note_pending"] = True
        _set_ai_checkin_state(context, state)
        context.user_data[WAITING_KEY] = AI_CHECKIN_NOTE_WAIT
        await show(
            update,
            context,
            "Напиши одним сообщением коротко: что важного про самочувствие/планы/симптомы.",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="aicheck:back:goals")]]),
            route=AI_CHECKIN_ROUTE,
            payload={"ai_checkin": state},
        )
        return

    if data == "aicheck:back":
        state["step"] = max(1, step - 1)
        _set_ai_checkin_state(context, state)
        await _render_ai_checkin_wizard(update, context, state=state)
        return

    if data == "aicheck:back:goals":
        state["note_pending"] = False
        _set_ai_checkin_state(context, state)
        context.user_data.pop(WAITING_KEY, None)
        await _render_ai_checkin_wizard(update, context, state=state)
        return

    if data == "aicheck:back:results":
        payload = _ai_checkin_actions_payload(state)
        date_local = state.get("last_date_local") or db.get_local_date_str(None)
        await _render_ai_checkin_results(
            update,
            context,
            date_local=date_local,
            payload=payload,
        )
        return

    if data == "aicheck:next":
        state["step"] = min(5, step + 1)
        _set_ai_checkin_state(context, state)
        await _render_ai_checkin_wizard(update, context, state=state)
        return

    if data == "aicheck:finish":
        state["target_day"] = str(state.get("target_day") or _get_ai_checkin_target_day(context))
        required_fields = ["sleep_range", "energy", "stress"]
        for field in required_fields:
            if not answers.get(field):
                await _render_ai_checkin_wizard(update, context, state=state)
                return
        if answers.get("training") is None:
            await _render_ai_checkin_wizard(update, context, state=state)
            return
        s = context.application.bot_data["settings"]
        tg_uid = update.effective_user.id
        conn = await db.connect(s.DB_PATH)
        try:
            tz_name = await db.get_user_tz(conn, tg_uid)
            if _is_tz_unset(tz_name):
                context.user_data.pop("tz_back_data", None)
                context.user_data["onboarding"] = "tz"
                await _show_timezone_picker(update, context, 0)
                return
            user_id = await db.get_or_create_user(conn, tg_uid)
            await _finalize_ai_checkin(
                update,
                context,
                conn,
                tg_uid=tg_uid,
                user_id=user_id,
                tz_name=tz_name or "UTC",
                state=state,
            )
        finally:
            await conn.close()
        return

    if data.startswith("aicheck:sleep:"):
        answers["sleep_range"] = data.split(":", 2)[2]
        state["answers"] = answers
        if int(state.get("step") or 1) == 1:
            state["step"] = 2
        _set_ai_checkin_state(context, state)
        await _render_ai_checkin_wizard(update, context, state=state)
        return

    if data.startswith("aicheck:energy:"):
        try:
            answers["energy"] = int(data.split(":", 2)[2])
        except ValueError:
            return
        state["answers"] = answers
        if int(state.get("step") or 1) == 2:
            state["step"] = 3
        _set_ai_checkin_state(context, state)
        await _render_ai_checkin_wizard(update, context, state=state)
        return

    if data.startswith("aicheck:stress:"):
        try:
            answers["stress"] = int(data.split(":", 2)[2])
        except ValueError:
            return
        state["answers"] = answers
        if int(state.get("step") or 1) == 3:
            state["step"] = 4
        _set_ai_checkin_state(context, state)
        await _render_ai_checkin_wizard(update, context, state=state)
        return

    if data.startswith("aicheck:training:"):
        try:
            answers["training"] = bool(int(data.split(":", 2)[2]))
        except ValueError:
            return
        state["answers"] = answers
        if int(state.get("step") or 1) == 4:
            state["step"] = 5
        _set_ai_checkin_state(context, state)
        await _render_ai_checkin_wizard(update, context, state=state)
        return

    if data.startswith("aicheck:goal:"):
        goal = data.split(":", 2)[2]
        goals = list(answers.get("goals") or [])
        if goal in goals:
            goals.remove(goal)
        else:
            goals.append(goal)
        answers["goals"] = goals
        state["answers"] = answers
        _set_ai_checkin_state(context, state)
        await _render_ai_checkin_wizard(update, context, state=state)
        return

    if data == "aicheck:select":
        payload = _ai_checkin_actions_payload(state)
        selected = set(state.get("selected_action_ids") or [])
        date_local = state.get("last_date_local") or db.get_local_date_str(None)
        await _render_ai_checkin_selection(
            update,
            context,
            payload=payload,
            selected_ids=selected,
            date_local=date_local,
        )
        return

    if data.startswith("aicheck:toggle:"):
        action_id = data.split(":", 2)[2]
        selected = set(state.get("selected_action_ids") or [])
        if action_id in selected:
            selected.remove(action_id)
        else:
            selected.add(action_id)
        state["selected_action_ids"] = list(selected)
        _set_ai_checkin_state(context, state)
        payload = _ai_checkin_actions_payload(state)
        date_local = state.get("last_date_local") or db.get_local_date_str(None)
        await _render_ai_checkin_selection(
            update,
            context,
            payload=payload,
            selected_ids=selected,
            date_local=date_local,
        )
        return

    if data == "aicheck:apply:all":
        payload = _ai_checkin_actions_payload(state)
        s = context.application.bot_data["settings"]
        tg_uid = update.effective_user.id
        conn = await db.connect(s.DB_PATH)
        try:
            user_id = await db.get_or_create_user(conn, tg_uid)
            date_local = state.get("last_date_local") or await db.get_user_local_day(conn, user_id)
            await engine.apply_day_plan_actions_with_slots(
                conn,
                user_id=user_id,
                tg_user_id=tg_uid,
                date_local=date_local,
                target_day=str(state.get("target_day") or "today"),
                ai_actions=payload,
            )
        finally:
            await conn.close()
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📅 План дня", callback_data="menu:todayplan")],
                [InlineKeyboardButton("🏠 Меню", callback_data="menu:home")],
            ]
        )
        await show(update, context, "Готово ✅ План дня обновлён.", kb, route=AI_CHECKIN_ROUTE)
        return

    if data == "aicheck:apply:selected":
        payload = _ai_checkin_actions_payload(state)
        selected = list(state.get("selected_action_ids") or [])
        s = context.application.bot_data["settings"]
        tg_uid = update.effective_user.id
        conn = await db.connect(s.DB_PATH)
        try:
            user_id = await db.get_or_create_user(conn, tg_uid)
            date_local = state.get("last_date_local") or await db.get_user_local_day(conn, user_id)
            await engine.apply_day_plan_actions_with_slots(
                conn,
                user_id=user_id,
                tg_user_id=tg_uid,
                date_local=date_local,
                target_day=str(state.get("target_day") or "today"),
                ai_actions=payload,
                selected_ids=set(selected),
            )
        finally:
            await conn.close()
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📅 План дня", callback_data="menu:todayplan")],
                [InlineKeyboardButton("🏠 Меню", callback_data="menu:home")],
            ]
        )
        await show(update, context, "Готово ✅ План дня обновлён.", kb, route=AI_CHECKIN_ROUTE)
        return

    if data == "aicheck:deny":
        s = context.application.bot_data["settings"]
        tg_uid = update.effective_user.id
        conn = await db.connect(s.DB_PATH)
        try:
            user_id = await db.get_or_create_user(conn, tg_uid)
            date_local = state.get("last_date_local") or await db.get_user_local_day(conn, user_id)
            await db.clear_day_plan(conn, user_id, date_local)
        finally:
            await conn.close()
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Меню", callback_data="menu:home")]])
        await show(update, context, "Ок, не применяю.", kb, route=AI_CHECKIN_ROUTE)

async def on_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard_not_banned(update, context):
        return
    q = update.callback_query
    if not q:
        return
    await _safe_answer_callback(update)
    data = q.data or ""
    s = context.application.bot_data["settings"]
    tg_uid = update.effective_user.id
    if data.startswith("tz:page:"):
        _, _, page = data.split(":", 2)
        await _show_timezone_picker(update, context, int(page))
        return
    if data == "tz:back":
        if context.user_data.get("onboarding") == "tz" or context.user_data.get("initial_pick_back_to_tz"):
            context.user_data["onboarding"] = "tz"
            page = int(context.user_data.get("tz_page") or 0)
            await _show_timezone_picker(
                update,
                context,
                page,
                message_text="Нужно выбрать часовой пояс, чтобы продолжить.",
            )
            return
        back_data = context.user_data.get("tz_back_data")
        if back_data == "menu:settings":
            conn = await db.connect(s.DB_PATH)
            try:
                await _render_settings_menu(update, context, conn, tg_uid)
            finally:
                await conn.close()
        else:
            await home(update, context)
        return
    if data.startswith("tz:set:"):
        tz_tag = data.split(":", 2)[2]
        tz_value = tz.tz_name_from_tag(tz_tag)
        conn = await db.connect(s.DB_PATH)
        try:
            await db.get_or_create_user(conn, tg_uid, tz_name="")
            await db.set_user_tz(conn, tg_uid, tz_value)
            context.user_data.pop("tz_back_data", None)
            onboarding_step = context.user_data.pop("onboarding", None)
            back_to_onboarding = bool(context.user_data.pop("initial_pick_back_to_tz", None))
            if onboarding_step != "tz" and back_to_onboarding:
                onboarding_step = "tz"
            tzinfo = db.parse_utc_tz(tz_value)
            local_now = datetime.now(tzinfo).strftime("%H:%M")
            label = tz.format_confirmation_label(tz_value)
            if onboarding_step == "tz":
                prefix = (
                    "✅ Часовой пояс сохранён: "
                    f"{label}\n"
                    f"Текущее время у тебя: {local_now}\n"
                    "Теперь сделаем начальный подбор."
                )
                onboarding_state = await db.get_onboarding_state(conn, tg_uid)
                free_pick_used = int(onboarding_state.get("onboarding_free_pick_used") or 0)
                onboarding_status = str(onboarding_state.get("onboarding_status") or "")
                onboarding_raw = str(onboarding_state.get("onboarding_raw_json") or "")
                if onboarding_status in {"active", "results"} and onboarding_raw:
                    await _render_onboarding_results(update, context, conn)
                elif free_pick_used == 0:
                    await db.start_onboarding_state(conn, tg_uid)
                    await _start_initial_pick(
                        update,
                        context,
                        prefix=prefix,
                        back_cb="tz:back",
                    )
                else:
                    await _show_ai_used_upsell(update, context)
            else:
                await show(
                    update,
                    context,
                    "✅ Часовой пояс сохранён: "
                    f"{label}\n"
                    f"Текущее время у тебя: {local_now}",
                    main_menu(
                        is_admin=(await _get_role_perms_ctx(context, update.effective_user.id if update.effective_user else 0))[0] != db.ROLE_NONE,
                        show_ai_checkin=await _has_ai_checkin_access(context, tg_uid),
                    ),
                    route="menu:home",
                )
        finally:
            await conn.close()

async def on_reminder_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard_not_banned(update, context):
        return
    q = update.callback_query
    if not q:
        return
    await _safe_answer_callback(update)
    data = q.data or ""

    parts = data.split(":")
    if len(parts) < 2:
        return
    action = parts[1]
    supplement_id: int | None = None
    snooze_minutes = 15
    callback_batch_ids: list[int] = []
    callback_day: str | None = None
    if action in ("done", "skip"):
        if len(parts) >= 3 and str(parts[2]).isdigit():
            supplement_id = int(parts[2])
    elif action == "snooze":
        if len(parts) >= 3 and str(parts[2]).isdigit():
            supplement_id = int(parts[2])
        if len(parts) >= 4:
            try:
                snooze_minutes = int(parts[3])
            except Exception:
                snooze_minutes = 15
    elif action in ("done_all", "skip_all"):
        if len(parts) >= 3:
            index = 2
            if parts[index].startswith('d'):
                try:
                    callback_day = datetime.strptime(parts[index][1:], '%Y%m%d').date().isoformat()
                except ValueError:
                    return
                index += 1
            callback_batch_ids = _parse_callback_batch_ids(parts[index] if len(parts) > index else '')
        supplement_id = None
    elif action == "snooze_all":
        if len(parts) >= 3:
            try:
                snooze_minutes = int(parts[2])
            except Exception:
                snooze_minutes = 15
        if len(parts) >= 4:
            index = 3
            if parts[index].startswith('d'):
                try:
                    callback_day = datetime.strptime(parts[index][1:], '%Y%m%d').date().isoformat()
                except ValueError:
                    return
                index += 1
            callback_batch_ids = _parse_callback_batch_ids(parts[index] if len(parts) > index else '')
        supplement_id = None
    else:
        return

    s = context.application.bot_data["settings"]
    tg_uid = update.effective_user.id
    ui_screen = ""
    payload_data: dict = {}
    ui_message_id: int | None = None
    conn = await db.connect(s.DB_PATH)
    try:
        ui_message_id, ui_screen, ui_payload = await db.get_ui_state(conn, tg_uid)
        try:
            payload_data = json.loads(ui_payload) if ui_payload else {}
        except json.JSONDecodeError:
            payload_data = {}
        if not isinstance(payload_data, dict):
            payload_data = {}

        message_id = int(q.message.message_id) if q.message else 0
        message_chat_id = int(q.message.chat_id) if q.message else 0
        current_reminder = message_id > 0 and message_id == int(payload_data.get('reminder_msg_id') or 0) and message_chat_id == int(payload_data.get('reminder_chat_id') or tg_uid)
        payload_batch_ids = _parse_callback_batch_ids(','.join(str(v) for v in (payload_data.get('reminder_batch_ids') or []) if str(v).isdigit())) if isinstance(payload_data.get('reminder_batch_ids'), list) else []
        if action.endswith('_all') and not callback_batch_ids and (callback_day or not current_reminder):
            await _safe_answer_callback(update, "Это напоминание устарело. Открой актуальный план дня.")
            return
        effective_batch_ids = callback_batch_ids or (payload_batch_ids if current_reminder else [])
        requested_ids = effective_batch_ids if action.endswith('_all') else ([supplement_id] if supplement_id else [])
        async with db.transaction(conn) as tx:
            day = await db.ensure_daily_rollover(tx, tg_uid)
            if callback_day and callback_day != day:
                await _safe_answer_callback(update, "Это напоминание за другой день. Открой актуальный план дня.")
                return
            valid_ids = [int(sid) for sid in dict.fromkeys(requested_ids) if await db.get_user_supplement(tx, tg_uid, int(sid))]
            if not valid_ids:
                await _safe_answer_callback(update, "Эти позиции больше не активны в твоём списке.")
                return
            effective_batch_ids = valid_ids
            if action in {'done', 'skip', 'done_all', 'skip_all'}:
                status = 'taken' if action in {'done', 'done_all'} else 'skipped'
                for item_id in valid_ids:
                    await db.set_day_status(tx, tg_uid, day, item_id, status)
        if action in {"snooze", "snooze_all"}:
            minutes = snooze_minutes
            if minutes not in {15, 30, 60}:
                return
            guard = context.application.bot_data.setdefault("snooze_guard", {})
            batch_key = ','.join(str(sid) for sid in sorted(effective_batch_ids))
            key = f"{tg_uid}:{batch_key}"
            now = datetime.utcnow()
            next_allowed = guard.get(key)
            if next_allowed and now < next_allowed:
                pass
            else:
                snooze_name = f"snooze:{tg_uid}:{batch_key}"
                for job in context.application.job_queue.get_jobs_by_name(snooze_name):
                    job.schedule_removal()
                if action == "snooze_all":
                    batch_ids = effective_batch_ids
                    if not batch_ids:
                        pass
                    else:
                        context.application.job_queue.run_once(
                            reminder_fire_job,
                            when=timedelta(minutes=minutes),
                            name=snooze_name,
                            data={
                                "tg_user_id": tg_uid,
                                "supplement_ids": [int(item_id) for item_id in batch_ids],
                            },
                        )
                else:
                    context.application.job_queue.run_once(
                        reminder_fire_job,
                        when=timedelta(minutes=minutes),
                        name=snooze_name,
                        data={
                            "tg_user_id": tg_uid,
                            "supplement_id": supplement_id,
                        },
                    )
                guard[key] = now + timedelta(minutes=minutes)

        async with db.transaction(conn) as tx:
            ui_message_id, ui_screen, latest_json = await db.get_ui_state(tx, tg_uid)
            try:
                payload_data = json.loads(latest_json) if latest_json else {}
            except json.JSONDecodeError:
                payload_data = {}
            if not isinstance(payload_data, dict):
                payload_data = {}
            is_latest = message_id > 0 and message_id == int(payload_data.get('reminder_msg_id') or 0) and message_chat_id == int(payload_data.get('reminder_chat_id') or tg_uid)
            if is_latest:
                for key in ('reminder_msg_id', 'reminder_chat_id', 'reminder_batch_ids'):
                    payload_data.pop(key, None)
            pending_messages = payload_data.get('reminder_messages')
            if isinstance(pending_messages, list):
                payload_data['reminder_messages'] = [item for item in pending_messages if not (isinstance(item, dict) and item.get('message_id') == message_id and item.get('chat_id') == message_chat_id)]
            await db.update_ui_state_payload(tx, tg_uid, json.dumps(payload_data, ensure_ascii=False))
    finally:
        await conn.close()

    if q.message:
        try:
            await q.message.delete()
        except Exception:
            pass

    route = ui_screen or "menu:home"
    payload = payload_data if isinstance(payload_data, dict) else {}
    conn = await db.connect(s.DB_PATH)
    try:
        await _render_route(
            update,
            context,
            conn,
            tg_uid,
            route,
            None,
            ui_message_id,
            payload=payload,
        )
    finally:
        await conn.close()

async def render_view(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    tg_user_id: int,
    view: str,
    payload: str,
) -> None:
    try:
        payload_data = json.loads(payload) if payload else {}
    except json.JSONDecodeError:
        payload_data = {}
    if not isinstance(payload_data, dict):
        payload_data = {}

    s = context.application.bot_data["settings"]
    conn = await db.connect(s.DB_PATH)
    try:
        if view == "catalog":
            if "mode" in payload_data:
                mode = payload_data.get("mode") or "home"
                if mode == "list":
                    await _show_catalog_list(update, context, conn, tg_user_id, payload_data)
                else:
                    await _show_catalog_home(update, context, conn)
                return
            page = int(payload_data.get("page") or 0)
            await render_catalog(update, context, conn, page)
            return
        if view == "supp_card":
            supplement_id = payload_data.get("id")
            return_page = int(payload_data.get("return_page") or 0)
            return_mode = payload_data.get("return_mode") or "catalog"
            search_query = payload_data.get("query")
            tab = payload_data.get("tab") or "brief"
            page = int(payload_data.get("page") or 0)
            prev_payload = payload_data.get("prev_payload")
            if supplement_id is not None:
                await render_supplement_card(
                    update,
                    context,
                    conn,
                    int(supplement_id),
                    return_page,
                    tab=tab,
                    page=page,
                    return_mode=return_mode,
                    search_query=search_query,
                    prev_payload=prev_payload,
                )
                return
        if view == "mine_list":
            page = int(payload_data.get("page") or 0)
            await _show_my_list(update, context, conn, tg_user_id, page)
            return
        if view == "mine_hub":
            supplement_id = payload_data.get("supp_id")
            if supplement_id is not None:
                await show_my_supp_hub(update, context, tg_user_id, int(supplement_id), conn=conn)
                return
        if view == "mysupp_list":
            page = int(payload_data.get("page") or 0)
            await _show_my_list(update, context, conn, tg_user_id, page, part=int(payload_data.get("part") or 0))
            return
        if view == "mysupp_hub":
            supplement_id = payload_data.get("supp_id") or payload_data.get("supplement_id")
            if supplement_id is not None:
                await show_my_supp_hub(update, context, tg_user_id, int(supplement_id), conn=conn, part=int(payload_data.get("part") or 0))
                return
        if view == "mysupp_reminders":
            supplement_id = payload_data.get("supplement_id")
            if supplement_id is not None:
                await _show_my_supp_reminders(update, context, conn, tg_user_id, int(supplement_id), page=int(payload_data.get("page") or 0))
                return
        await home(update, context)
    finally:
        await conn.close()

async def _render_route(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    tg_uid: int,
    route: str,
    target_chat_id: int | None,
    target_message_id: int | None,
    *,
    payload: dict | None = None,
) -> None:
    if route == "menu:home":
        await home(update, context, target_chat_id=target_chat_id, target_message_id=target_message_id)
        return
    saved_waiting = _waiting_from_saved_form(route)
    if saved_waiting:
        context.user_data[WAITING_KEY] = saved_waiting
        prompt, back_cb = None, None
        if route.startswith(("schedule:slot_time_set:", "schedule:add_custom")):
            prompt, back_cb = "Введи время в формате HH:MM", "settings:notifications"
        elif route.startswith("rem:set:"):
            prompt, back_cb = "Напиши новое время в формате HH:MM.", "rem:home"
        elif route.startswith("supplement:set_override:"):
            prompt, back_cb = "Введи время в формате HH:MM", f"mysupp:open:{route.split(':')[-1]}"
        elif route.startswith("mysupp:addslot:newtime:"):
            prompt, back_cb = "Введите время нового слота в формате HH:MM.", f"mysupp:addslot:{route.split(':')[-1]}"
        elif route == "cx:create":
            prompt, back_cb = "Введи название нового комплекса.", "cx:list"
        elif route.startswith("cx:rename:"):
            prompt, back_cb = "Введи новое название комплекса.", f"cx:open:{route.split(':')[-1]}"
        elif route.startswith("cx:item:dose:"):
            prompt, back_cb = "Введи дозировку для этой добавки в комплексе.", route.replace('dose', 'open', 1)
        elif route.startswith("cx:add:catalog:"):
            prompt, back_cb = "Введи название БАДа для добавления из каталога.", f"cx:add:{route.split(':')[-1]}"
        if prompt:
            await show(update, context, prompt, InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=back_cb)]]), route=route, target_chat_id=target_chat_id, target_message_id=target_message_id)
            return
    compact_curated = re.fullmatch(r"curated:(open_id|detail_id):(\d+)(?::(-?\d+))?", route)
    if compact_curated:
        _, stack_id, page = compact_curated.groups()
        stack = await db.admin_get_curated_stack_by_id(conn, int(stack_id))
        if not stack or not int(stack['is_active'] or 0):
            await show(update, context, "Стек не найден.", back(), route="menu:curated", target_chat_id=target_chat_id, target_message_id=target_message_id)
            return
        await _show_curated_stack(update, context, conn, str(stack['slug']), page=int(page or 0), target_chat_id=target_chat_id, target_message_id=target_message_id)
        return
    if route in {"cx:list", "menu:complexes", "settings:complexes"} or re.fullmatch(r"cx:list:\d+", route):
        page = int(route.split(":")[-1]) if route.startswith('cx:list:') else 0
        await _show_complexes_hub(update, context, conn, tg_uid, page, target_chat_id=target_chat_id, target_message_id=target_message_id)
        return
    if re.fullmatch(r"cx:open:\d+(?::-?\d+)?", route):
        parts = route.split(":")
        await _show_complex_detail(update, context, conn, tg_uid, int(parts[2]), int(parts[3]) if len(parts)>3 else 0, target_chat_id=target_chat_id, target_message_id=target_message_id)
        return
    if re.fullmatch(r"cx:item:open:\d+:\d+", route):
        parts = route.split(":")
        await _show_complex_item(update, context, conn, tg_uid, int(parts[3]), int(parts[4]), target_chat_id=target_chat_id, target_message_id=target_message_id)
        return
    if route == "catalog":
        page = int(payload.get("page") or 0) if isinstance(payload, dict) else 0
        await render_catalog(
            update,
            context,
            conn,
            page,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if re.fullmatch(r"cat:(card|evidence)(?::\d+)?", route):
        parts = route.split(':')
        supplement_id = int(parts[2]) if len(parts)>2 else int(payload.get('supplement_id') or 0) if isinstance(payload, dict) else 0
        if supplement_id:
            renderer = _show_catalog_evidence if parts[1] == 'evidence' else _show_catalog_card
            await renderer(update, context, conn, tg_uid, supplement_id, target_chat_id=target_chat_id, target_message_id=target_message_id)
        else:
            await render_catalog(update, context, conn, 0, target_chat_id=target_chat_id, target_message_id=target_message_id)
        return
    if route == "supp_card":
        if isinstance(payload, dict):
            supplement_id = payload.get("id")
            return_page = int(payload.get("return_page") or 0)
            return_mode = payload.get("return_mode") or "catalog"
            search_query = payload.get("query")
            tab = payload.get("tab") or "brief"
            page = int(payload.get("page") or 0)
            prev_payload = payload.get("prev_payload")
            if supplement_id is not None:
                await render_supplement_card(
                    update,
                    context,
                    conn,
                    int(supplement_id),
                    return_page,
                    tab=tab,
                    page=page,
                    return_mode=return_mode,
                    search_query=search_query,
                    prev_payload=prev_payload,
                    target_chat_id=target_chat_id,
                    target_message_id=target_message_id,
                )
                return
    if route == "cat_search_wait":
        return_page = int(payload.get("return_page") or 0) if isinstance(payload, dict) else 0
        await _show_catalog_search_prompt(
            update,
            context,
            conn,
            return_page=return_page,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route == "cat_search_results":
        if isinstance(payload, dict):
            query = str(payload.get("query") or "").strip()
            page = int(payload.get("page") or 0)
            return_page = int(payload.get("return_page") or 0)
            if not query:
                await _show_catalog_search_prompt(
                    update,
                    context,
                    conn,
                    return_page=return_page,
                    target_chat_id=target_chat_id,
                    target_message_id=target_message_id,
                )
                return
            await render_search_results(
                update,
                context,
                conn,
                tg_uid=tg_uid,
                query=query,
                page=page,
                return_page=return_page,
                target_chat_id=target_chat_id,
                target_message_id=target_message_id,
            )
            return
    if route in {"menu:today", "today:home"}:
        await _show_today_home(update, context, conn, tg_uid)
        return
    if route == "today:slots":
        await _show_today_slots(update, context, conn, tg_uid)
        return
    if route.startswith("today:slot:"):
        await _show_slot_screen(update, context, conn, tg_uid, route.split(":", 2)[2])
        return
    if route.startswith("intake:pick:"):
        parts = route.split(":")
        slot_id = parts[2]
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        await _show_intake_picker(update, context, conn, tg_uid, slot_id, page)
        return
    if route == "rem:home":
        await _show_reminders_home(update, context, conn, tg_uid)
        return
    if route == "settings:notifications":
        await _show_notifications_settings(update, context, conn, tg_uid)
        return

    if route == "menu:settings":
        await _render_settings_menu(update, context, conn, tg_uid)
        return
    if route == "menu:add":
        await show(
            update,
            context,
            "Введи название добавки (поиск по каталогу).\nНапример: <b>caffeine</b> или <b>magnesium</b>",
            back(),
            route=route,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route == "menu:compat":
        await show(
            update,
            context,
            "Введи добавку, чтобы показать совместимость (синергии/антагонисты).",
            back(),
            route=route,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route == AI_CHECKIN_ROUTE:
        if isinstance(payload, dict) and payload.get("ai_checkin"):
            state = _get_ai_checkin_state(context)
            state["step"] = int(state.get("step") or 1)
            _set_ai_checkin_state(context, state)
            await _render_ai_checkin_wizard(update, context, state=state)
        else:
            await _render_ai_checkin_entry(
                update,
                context,
                target_chat_id=target_chat_id,
                target_message_id=target_message_id,
            )
        return
    if route == AI_CHECKIN_VOICE_ROUTE:
        await _render_ai_checkin_voice_prompt(
            update,
            context,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route == "menu:curated":
        await _show_curated_page(
            update,
            context,
            conn,
            0,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route == "menu:catalog":
        await _show_catalog_home(
            update,
            context,
            conn,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route == "menu:todayplan":
        await _show_today_plan(update, context, conn, tg_uid)
        return
    if route == "mysupp:add":
        context.user_data[WAITING_KEY] = "mysupp_add_query"
        await show(
            update,
            context,
            "Введи название БАДА для добавления.",
            my_list_back(),
            route=route,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route.startswith("mysupp:addslot:"):
        supplement_id = int(route.split(":")[-1])
        await _show_add_to_stack_slot_picker(update, context, conn, tg_uid, supplement_id)
        return
    if route.startswith("cat:page:"):
        payload = {"mode": "list", "list_mode": "alpha", "page": int(route.split(":")[-1]), "query": "", "class_key": "", "body_token": "", "return_page": int(route.split(":")[-1])}
        await _show_catalog_list(
            update,
            context,
            conn,
            tg_uid,
            payload,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route == "cat:home":
        await _show_catalog_home(
            update,
            context,
            conn,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route == "cat:goal":
        page = int(payload.get("page") or 0) if isinstance(payload, dict) else 0
        await _show_catalog_class_picker(
            update,
            context,
            conn,
            page,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route == "cat:organ":
        await _show_catalog_body_picker(update, context, "benefit")
        return
    if route.startswith("cat:list"):
        page = 0
        parts = route.split(":")
        if len(parts) == 3 and parts[2].isdigit():
            page = int(parts[2])
        elif isinstance(payload, dict):
            page = int(payload.get("page") or 0)
        list_payload = payload if isinstance(payload, dict) else {
            "mode": "list",
            "list_mode": "alpha",
            "page": max(page, 0),
            "query": "",
            "class_key": "",
            "body_token": "",
            "return_page": max(page, 0),
        }
        list_payload["page"] = max(page, 0)
        await _show_catalog_list(
            update,
            context,
            conn,
            tg_uid,
            list_payload,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route == "admin:home":
        await _show_admin_home(update, context)
        return
    if route == "admin:stacks":
        if not await _require_admin(update, context, "content_bulk"):
            return
        page = 0
        if isinstance(payload, dict):
            page = int(payload.get("page") or 0)
        await _show_admin_stacks(update, context, conn, page)
        return
    if re.fullmatch(r"admin:stack:(?:open:\d+|items:\d+(?::\d+)?|item:open:\d+:\d+(?::\d+)?)", route):
        if not await _require_admin(update, context, "content_bulk"):
            return
        parts = route.split(":")
        if parts[2] == 'open':
            await _show_admin_stack_editor(update, context, conn, int(parts[3]), target_chat_id=target_chat_id, target_message_id=target_message_id)
        elif parts[2] == 'items':
            await _show_admin_stack_items(update, context, conn, int(parts[3]), int(parts[4]) if len(parts)>4 else 0, target_chat_id=target_chat_id, target_message_id=target_message_id)
        else:
            await _show_admin_stack_item(update, context, conn, int(parts[4]), int(parts[5]), int(parts[6]) if len(parts)>6 else 0, target_chat_id=target_chat_id, target_message_id=target_message_id)
        return
    if route.startswith("cat:class:page:"):
        page = int(route.split(":")[-1])
        await _show_catalog_class_picker(
            update,
            context,
            conn,
            page,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route.startswith("curated:page:"):
        page = int(route.split(":")[-1])
        await _show_curated_page(
            update,
            context,
            conn,
            page,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route.startswith("curated:detail_page:"):
        _, _, slug, page = route.split(":", 3)
        await _show_curated_stack(update, context, conn, slug, page=int(page), target_chat_id=target_chat_id, target_message_id=target_message_id)
        return
    if route.startswith("curated:open:"):
        slug = route.split(":", 2)[-1]
        await _show_curated_stack(
            update,
            context,
            conn,
            slug,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route.startswith("curated:add:"):
        slug = route.split(":", 2)[-1]
        await _show_curated_stack(
            update,
            context,
            conn,
            slug,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route.startswith("mysupp:list:"):
        parts = route.split(":")
        page = int(parts[2])
        part = int(parts[3]) if len(parts) > 3 else 0
        await _show_my_list(
            update,
            context,
            conn,
            tg_uid,
            page,
            part=part,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route.startswith("mysupp:open:") or route.startswith("mysupp:hub:"):
        parts = route.split(":")
        supplement_id = int(parts[2])
        part = int(parts[3]) if len(parts) > 3 else 0
        await show_my_supp_hub(
            update,
            context,
            tg_uid,
            supplement_id,
            conn=conn,
            part=part,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route.startswith("supp:open:"):
        supplement_id = int(route.split(":")[-1])
        await _show_supplement_page(
            update,
            context,
            conn,
            tg_uid,
            supplement_id,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route.startswith("supp:view:"):
        _, _, slug, page = route.split(":", 3)
        row = await db.get_supplement_by_slug(conn, slug)
        if not row:
            await home(update, context, target_chat_id=target_chat_id, target_message_id=target_message_id)
            return
        await _show_supplement_page(
            update,
            context,
            conn,
            tg_uid,
            int(row["id"]),
            back_route=f"cat:page:{int(page)}",
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route.startswith("supp:compat:"):
        _, _, sid, kind, page = route.split(":", 4)
        await _show_supplement_compat(
            update,
            context,
            conn,
            int(sid),
            kind,
            int(page),
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route.startswith("mysupp:set_dose:"):
        supplement_id = int(route.split(":")[-1])
        context.user_data[WAITING_KEY] = f"mysupp:set_dose:{supplement_id}"
        await show(
            update,
            context,
            "Напиши дозу (до 80 символов).",
            my_hub_back(supplement_id),
            route=route,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route.startswith("mysupp:rem:add:"):
        supplement_id = int(route.split(":")[-1])
        context.user_data[WAITING_KEY] = f"mysupp:rem_add:{supplement_id}"
        await show(
            update,
            context,
            "Напиши время (например: 09:30, 9.30, 9 30 или 0930)",
            my_hub_back(supplement_id),
            route=route,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route.startswith("mysupp:rem:"):
        parts = route.split(":")
        supplement_id = int(parts[3]) if parts[2] == "page" else int(parts[-1])
        page = int(parts[4]) if parts[2] == "page" else 0
        await _show_my_supp_reminders(
            update,
            context,
            conn,
            tg_uid,
            supplement_id,
            page=page,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route.startswith("tz:page:"):
        _, _, page = route.split(":", 2)
        await _show_timezone_picker(
            update,
            context,
            int(page),
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route.startswith("add_pick:"):
        slug = route.split(":", 1)[1]
        row = await db.get_supplement_by_slug(conn, slug)
        if row:
            text = f"✅ Добавлено: <b>{row['name']}</b>"
        else:
            text = "Не нашёл в каталоге."
        await show(
            update,
            context,
            text,
            back(),
            route=route,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route.startswith("compat_pick:"):
        slug = route.split(":", 1)[1]
        row = await db.get_supplement_by_slug(conn, slug)
        if not row:
            await show(
                update,
                context,
                "Не нашёл в каталоге.",
                back(),
                route=route,
                target_chat_id=target_chat_id,
                target_message_id=target_message_id,
            )
            return
        await _show_supplement_compat(
            update, context, conn, int(row['id']), 'synergy', 0,
            back_data='menu:compat', target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route in {"menu:ai", "onboard:results"}:
        restored = await _render_onboarding_results(
            update,
            context,
            conn,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        if restored:
            return
        await home(update, context, target_chat_id=target_chat_id, target_message_id=target_message_id)
        return

    await home(update, context, target_chat_id=target_chat_id, target_message_id=target_message_id)

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard_not_banned(update, context):
        return
    if not update.message or not update.message.text:
        return
    if context.user_data.get("onboarding") == "tz":
        try:
            await update.message.delete()
        except Exception:
            pass
        return
    txt_raw = update.message.text
    txt = txt_raw.strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    s = context.application.bot_data["settings"]
    tg_uid = update.effective_user.id
    conn = await db.connect(s.DB_PATH)
    try:
        ui_message_id, ui_screen, ui_payload = await db.get_ui_state(conn, tg_uid)
        if str(ui_screen or '').startswith('admin:stack:') and not await _require_admin(update, context, "content_bulk"):
            return
        if ui_screen == "cat_search_wait":
            try:
                payload_data = json.loads(ui_payload) if ui_payload else {}
            except json.JSONDecodeError:
                payload_data = {}
            if not isinstance(payload_data, dict):
                payload_data = {}
            return_page = int(payload_data.get("return_page") or 0)
            await render_search_results(
                update,
                context,
                conn,
                tg_uid=tg_uid,
                query=txt,
                page=0,
                return_page=return_page,
                target_message_id=ui_message_id,
            )
            try:
                await update.message.delete()
            except Exception:
                pass
            return
        if ui_screen == AI_CHECKIN_VOICE_ROUTE:
            if not await require_premium_or_upsell(update, context, "AI check-in"):
                return
            try:
                payload_data = json.loads(ui_payload) if ui_payload else {}
            except json.JSONDecodeError:
                payload_data = {}
            if not isinstance(payload_data, dict):
                payload_data = {}
            await show(
                update,
                context,
                "Пришли именно голосовое сообщение.",
                ai_checkin_voice_kb(),
                route=AI_CHECKIN_VOICE_ROUTE,
                payload=payload_data or _ai_checkin_voice_payload(),
                target_message_id=ui_message_id,
            )
            try:
                await update.message.delete()
            except Exception:
                pass
            return
        if ui_screen == "owner_wait_target_tg_id":
            if not await _require_admin(update, context, "roles_manage"):
                return
            if not txt.isdigit():
                await show(update, context, "Нужен числовой tg_user_id.", InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data="admin:home")]]), route="owner_wait_target_tg_id")
                return
            target_id = int(txt)
            context.user_data["owner_target_tg_id"] = target_id
            role_now = await db.get_role(conn, target_id)
            await show(update, context, f"Текущая роль {target_id}: {role_now}", InlineKeyboardMarkup([[InlineKeyboardButton("Сделать админом", callback_data="admin:roles:set:admin")],[InlineKeyboardButton("Сделать редактором", callback_data="admin:roles:set:editor")],[InlineKeyboardButton("Сделать саппортом", callback_data="admin:roles:set:support")],[InlineKeyboardButton("Снять роль", callback_data="admin:roles:set:none")],[InlineKeyboardButton("Отмена", callback_data="admin:home")]]), route="admin:roles")
            try:
                await update.message.delete()
            except Exception:
                pass
            return
        if ui_screen == "gift:admin:create_wait":
            if not _is_admin(tg_uid):
                await _show_gift_hub(update, context, tg_uid)
                return
            if not txt.isdigit() or int(txt) <= 0:
                await show(update, context, "Введите корректное число дней (например, 30).", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="settings:gift")]]), route="gift:admin:create_wait")
                return
            days = int(txt)
            try:
                code = await db.create_gift_code(conn, admin_tg_id=tg_uid, days=days)
            except Exception:
                log.exception("Gift code generation failed for admin_tg_id=%s", tg_uid)
                await show(
                    update,
                    context,
                    "⚠️ Не удалось создать код из-за внутренней ошибки. Попробуйте ещё раз.",
                    InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔁 Повторить", callback_data="gift:admin:create")],
                        [InlineKeyboardButton("⬅️ Назад", callback_data="settings:gift")],
                    ]),
                    route="gift:admin:create_wait",
                )
                return
            await show(
                update,
                context,
                f"✅ Код создан.\nДней Premium: <b>{days}</b>\nКод для копирования:\n<code>{code}</code>",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Создать ещё", callback_data="gift:admin:create")],
                    [InlineKeyboardButton("📋 Коды", callback_data="gift:admin:codes")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="settings:gift")],
                ]),
                route="settings:gift",
            )
            return
        if ui_screen == "settings:gift":
            status, days, until_iso = await db.redeem_gift_code(conn, tg_user_id=tg_uid, code=txt)
            if status == "ok":
                context.user_data["home_entry_reason"] = "premium_activated"
                context.user_data["home_entry_extra"] = (
                    f"🎁 Подарочный код активирован: +{days} дн. Premium до {_format_until_ru(until_iso)}."
                )
                await home(update, context)
                return
            if status == "used":
                await show(update, context, "Извините, этот код уже истёк или был использован.", gift_user_kb(is_admin=_is_admin(tg_uid)), route="settings:gift")
                return
            await show(update, context, "Извините, такого кода не существует.", gift_user_kb(is_admin=_is_admin(tg_uid)), route="settings:gift")
            return

        if ui_screen == "admin_wait_query":
            if not await _require_admin(update, context, "content_edit"):
                return
            await _show_admin_supp_results(update, context, conn, txt, offset=0)
            try:
                await update.message.delete()
            except Exception:
                pass
            return

        if ui_screen == "admin:seed_patch_wait":
            if not await _require_admin(update, context, "content_bulk"):
                return
            try:
                payload_data = json.loads(ui_payload) if ui_payload else {}
            except json.JSONDecodeError:
                payload_data = {}
            if not isinstance(payload_data, dict):
                payload_data = {}
            part = txt_raw
            memory_buffer = str(context.user_data.get("admin_seed_buffer") or "")
            payload_buffer = str(payload_data.get("buffer") or "")
            buffered = memory_buffer if len(memory_buffer) >= len(payload_buffer) else payload_buffer
            # Важно склеивать без добавления символов: Telegram может резать JSON
            # посреди строки/escape-последовательности, и лишний \n ломает payload.
            merged_payload = f"{buffered}{part}" if buffered else part
            try:
                items = _parse_admin_seed_payload(merged_payload)
            except AdminSeedPayloadError as exc:
                if exc.incomplete:
                    context.user_data["admin_seed_buffer"] = merged_payload
                    await show(
                        update,
                        context,
                        (
                            "Принял часть данных. Похоже, JSON ещё не завершён.\n"
                            "Если Telegram разрезал длинный payload, просто отправь следующую часть — "
                            "я склею сообщения и продолжу импорт."
                        ),
                        InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin:home")]]),
                        route="admin:seed_patch_wait",
                        payload={"mode": "rich_patch", "buffer": merged_payload},
                    )
                    return
                context.user_data.pop("admin_seed_buffer", None)
                await show(
                    update,
                    context,
                    (
                        "Не удалось разобрать payload.\n"
                        f"Ошибка: <code>{html.escape(str(exc))}</code>\n\n"
                        "Проверь JSON и отправь снова.\n\n"
                        f"{_admin_seed_helper_text()}"
                    ),
                    InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin:home")]]),
                    route="admin:seed_patch_wait",
                    payload={"mode": "rich_patch", "buffer": ""},
                )
                return
            try:
                imported_slugs = await _admin_import_seed_objects(items)
            except Exception as exc:
                context.user_data.pop("admin_seed_buffer", None)
                await show(
                    update,
                    context,
                    (
                        "Импорт завершился с ошибкой.\n"
                        f"<code>{html.escape(str(exc))}</code>"
                    ),
                    InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin:home")]]),
                    route="admin:seed_patch_wait",
                    payload={"mode": "rich_patch", "buffer": ""},
                )
                return
            context.user_data.pop("admin_seed_buffer", None)
            preview = ", ".join(imported_slugs[:5])
            more = f" и ещё {len(imported_slugs) - 5}" if len(imported_slugs) > 5 else ""
            await show(
                update,
                context,
                (
                    f"✅ Импортировано объектов: <b>{len(imported_slugs)}</b>\n"
                    f"Slug: <code>{html.escape(preview)}</code>{more}"
                ),
                InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В админ-меню", callback_data="admin:home")]]),
                route="admin:home",
            )
            try:
                await update.message.delete()
            except Exception:
                pass
            return

        if ui_screen == "admin_wait_link":
            if not await _require_admin(update, context, "content_edit"):
                return
            try:
                payload_data = json.loads(ui_payload) if ui_payload else {}
            except json.JSONDecodeError:
                payload_data = {}
            supplement_id = int(payload_data.get("supplement_id") or 0)
            url = txt.strip()
            if len(url) > 500 or not (url.startswith("http://") or url.startswith("https://")):
                await show(
                    update,
                    context,
                    "Некорректный URL. Отправь ссылку, начинающуюся с http:// или https://",
                    InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Назад", callback_data=f"admin:supp_open:{supplement_id}")]]),
                    route="admin_wait_link",
                    payload={"supplement_id": supplement_id, "admin_query": str(payload_data.get("admin_query") or ""), "offset": int(payload_data.get("offset") or 0)},
                )
                return
            await db.admin_set_supplement_field(conn, tg_uid, supplement_id, "purchase_url", url)
            await _show_admin_supp_card(update, context, conn, supplement_id, admin_query=str(payload_data.get("admin_query") or ""), offset=int(payload_data.get("offset") or 0))
            try:
                await update.message.delete()
            except Exception:
                pass
            return

        if ui_screen == "admin_edit_field":
            if not await _require_admin(update, context, "content_edit"):
                return
            try:
                payload_data = json.loads(ui_payload) if ui_payload else {}
            except json.JSONDecodeError:
                payload_data = {}
            supplement_id = int(payload_data.get("supplement_id") or 0)
            field_name = str(payload_data.get("field_name") or "")
            if supplement_id <= 0 or not field_name:
                await show(update, context, "Сессия редактирования устарела.", admin_hub_kb(), route="admin:home")
                return
            try:
                await db.admin_set_supplement_field(conn, tg_uid, supplement_id, field_name, txt)
            except ValueError as exc:
                err = str(exc)
                hint = "Некорректное значение."
                if err.startswith("max_len_"):
                    hint = f"Слишком длинное значение ({err.replace('max_len_', 'максимум ')})"
                elif err == "invalid_choice":
                    hint = "Недопустимое значение для поля."
                elif err in {"invalid_json", "json_array_required", "json_strings_required"}:
                    hint = "Ожидается JSON-массив строк, например [\"brain\",\"gut\"]."
                elif err == "invalid_url":
                    hint = "URL должен начинаться с http:// или https://"
                await show(
                    update,
                    context,
                    f"{hint}\nТекущее поле: {_admin_field_label(field_name)}",
                    admin_supp_edit_wait_kb(supplement_id),
                    route="admin_edit_field",
                    payload=payload_data,
                )
                return
            await _show_admin_supp_card(update, context, conn, supplement_id)
            try:
                await update.message.delete()
            except Exception:
                pass
            return

        if ui_screen == "admin:stack:new_wait":
            if not await _require_admin(update, context, "content_bulk"):
                return
            title = txt[:120].strip()
            if not title:
                await show(
                    update,
                    context,
                    "Название пустое. Отправь название нового стека.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin:stacks")]]),
                    route="admin:stack:new_wait",
                    payload={"return": "admin:stacks"},
                )
                return
            base = _slugify_ascii(title) or f"stack_{datetime.now().strftime('%Y%m%d%H%M')}"
            slug = base
            suffix = 2
            while await db.admin_get_curated_stack_by_slug(conn, slug):
                slug = f"{base}_{suffix}"
                suffix += 1
            await db.admin_upsert_curated_stack(
                conn,
                slug=slug,
                title_ru=title,
                author_ru="",
                description_ru="",
                source_url="",
                tags=[],
                is_active=0,
            )
            stack = await db.admin_get_curated_stack_by_slug(conn, slug)
            await _show_admin_stack_editor(update, context, conn, int(stack["id"]))
            try:
                await update.message.delete()
            except Exception:
                pass
            return
        if ui_screen == "admin:stack:edit_wait":
            if not await _require_admin(update, context, "content_bulk"):
                return
            try:
                payload_data = json.loads(ui_payload) if ui_payload else {}
            except json.JSONDecodeError:
                payload_data = {}
            stack_id = int(payload_data.get("stack_id") or 0)
            field = str(payload_data.get("field") or "")
            stack = await db.admin_get_curated_stack_by_id(conn, stack_id)
            if not stack or field not in {"title_ru", "author_ru", "description_ru", "source_url", "tags"}:
                await show(
                    update,
                    context,
                    "Не удалось сохранить.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin:stacks")]]),
                    route="admin:stacks",
                )
                return
            title_ru = str(stack["title_ru"] or "")
            author_ru = str(stack["author_ru"] or "")
            description_ru = str(stack["description_ru"] or "")
            source_url = str(stack["source_url"] or "")
            tags = []
            try:
                tags = json.loads(stack["tags_json"] or "[]")
            except Exception:
                tags = []
            value = "" if txt.strip() == "-" else txt.strip()
            if field == "tags":
                tags = [t.strip() for t in txt.split(",") if t.strip()][:20]
            elif field == "title_ru":
                title_ru = value[:120]
            elif field == "author_ru":
                author_ru = value[:120]
            elif field == "description_ru":
                description_ru = value
            elif field == "source_url":
                if value and not is_http_url(value):
                    await show(update, context, "Некорректная ссылка. Отправь http:// или https:// ссылку (до 2048 символов), либо '-' чтобы очистить.", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"admin:stack:open:{stack_id}")]]), route="admin:stack:edit_wait", payload=payload_data)
                    return
                source_url = value
            await db.admin_upsert_curated_stack(
                conn,
                slug=str(stack["slug"]),
                title_ru=title_ru,
                author_ru=author_ru,
                description_ru=description_ru,
                source_url=source_url,
                tags=tags,
                is_active=int(stack["is_active"] or 0),
            )
            await _show_admin_stack_editor(update, context, conn, stack_id)
            try:
                await update.message.delete()
            except Exception:
                pass
            return
        if ui_screen == "admin:stack:item_search_wait":
            try:
                payload_data = json.loads(ui_payload) if ui_payload else {}
            except json.JSONDecodeError:
                payload_data = {}
            stack_id = int(payload_data.get("stack_id") or 0)
            if not await db.admin_get_curated_stack_by_id(conn, stack_id):
                await show(update, context, "Стек не найден.", admin_stacks_kb(), route="admin:stacks")
                return
            query = txt.strip()
            if len(query) < 2:
                await show(
                    update,
                    context,
                    "Запрос слишком короткий. Напиши минимум 2 символа.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"admin:stack:items:{stack_id}")]]),
                    route="admin:stack:item_search_wait",
                    payload={"stack_id": stack_id, "query": "", "page": 0},
                )
                return
            rows, total = await db.search_supplements(conn, query, 0, page_size=10)
            await show(
                update,
                context,
                "🔎 <b>Поиск по каталогу</b>\nВыбери добавку для стека.",
                admin_stack_search_kb(stack_id, rows, 0, has_prev=False, has_next=10 < int(total)),
                route="admin:stack:item_search",
                payload={"stack_id": stack_id, "query": query, "page": 0},
            )
            try:
                await update.message.delete()
            except Exception:
                pass
            return
        if ui_screen == "admin:stack:item_details_wait":
            try:
                payload_data = json.loads(ui_payload) if ui_payload else {}
            except json.JSONDecodeError:
                payload_data = {}
            stack_id = int(payload_data.get("stack_id") or 0)
            if not await db.admin_get_curated_stack_by_id(conn, stack_id):
                await show(update, context, "Стек не найден.", admin_stacks_kb(), route="admin:stacks")
                return
            slug = str(payload_data.get("slug") or "")
            name = str(payload_data.get("name") or slug)
            parts = [p.strip() for p in txt.split("|")]
            dose_ru = parts[0] if len(parts) > 0 else ""
            timing_ru = parts[1] if len(parts) > 1 else ""
            notes_ru = parts[2] if len(parts) > 2 else ""
            if not dose_ru and not timing_ru:
                await show(
                    update,
                    context,
                    "Укажи хотя бы дозу или время.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"admin:stack:items:{stack_id}")]]),
                    route="admin:stack:item_details_wait",
                    payload=payload_data,
                )
                return
            await db.admin_add_curated_stack_item(conn, stack_id, slug, name, dose_ru, timing_ru, notes_ru)
            await _show_admin_stack_items(update, context, conn, stack_id)
            try:
                await update.message.delete()
            except Exception:
                pass
            return
        if ui_screen == "admin:stack:item_edit_wait":
            try:
                payload_data = json.loads(ui_payload) if ui_payload else {}
            except json.JSONDecodeError:
                payload_data = {}
            stack_id = int(payload_data.get("stack_id") or 0)
            item_id = int(payload_data.get("item_id") or 0)
            item = await db.admin_get_curated_stack_item(conn, item_id)
            if not item or int(item['stack_id']) != stack_id:
                await _show_admin_stack_items(update, context, conn, stack_id)
                return
            parts = [p.strip() for p in txt.split("|")]
            dose_ru = parts[0] if len(parts) > 0 else ""
            timing_ru = parts[1] if len(parts) > 1 else ""
            notes_ru = parts[2] if len(parts) > 2 else ""
            await db.admin_update_curated_stack_item(conn, item_id, dose_ru, timing_ru, notes_ru)
            await _show_admin_stack_item(update, context, conn, stack_id, item_id)
            try:
                await update.message.delete()
            except Exception:
                pass
            return
    finally:
        await conn.close()
    waiting = context.user_data.get(WAITING_KEY)
    managed_waiting = bool(waiting and (
        _waiting_from_saved_form(waiting)
        or waiting in {"add_query", "compat_query", "mysupp_add_query"}
        or waiting.startswith(("mysupp:rem_add:", "cx:add_query:"))
    ))
    legacy_complex_query = bool(waiting and waiting.startswith("cx:add_query:") and ui_screen == f"cx:add:{waiting.split(':')[-1]}")
    if not waiting or (ui_screen and managed_waiting and not legacy_complex_query):
        waiting = _waiting_from_saved_form(ui_screen)
        if waiting:
            context.user_data[WAITING_KEY] = waiting
        else:
            context.user_data.pop(WAITING_KEY, None)
    if waiting and waiting.startswith(("mysupp:set_dose:", "mysupp:rem_add:", "rem:set:", "schedule:add_custom", "schedule:slot_time_set:", "supplement:set_override:", "mysupp:addslot:newtime:")):
        supplement_id = None
        if waiting.startswith(("mysupp:set_dose:", "mysupp:rem_add:", "supplement:set_override:")):
            _, _, value_text = waiting.split(":", 2)
            supplement_id = int(value_text)
        s = context.application.bot_data["settings"]
        tg_uid = update.effective_user.id
        conn = await db.connect(s.DB_PATH)
        try:
            if supplement_id is not None:
                user_id = await db.get_or_create_user(conn, tg_uid, tz_name="")
                member = await db.fetchone(conn, "SELECT 1 FROM user_supplements WHERE user_id=? AND supplement_id=? AND is_active=1", (user_id, int(supplement_id)))
                if not member:
                    context.user_data.pop(WAITING_KEY, None)
                    await show(update, context, "БАД не найден в твоём списке.", my_list_back(), route="mysupp:list:0")
                    return
            if waiting.startswith("mysupp:set_dose:"):
                trimmed = txt.strip()
                if not trimmed or len(trimmed) > 80:
                    await show(
                        update,
                        context,
                        "Напиши дозу (до 80 символов).",
                        my_hub_back(supplement_id),
                        route=f"mysupp:set_dose:{supplement_id}",
                    )
                else:
                    user_id = await db.get_or_create_user(conn, tg_uid, tz_name="")
                    try:
                        await db.set_user_supp_dose(conn, user_id, supplement_id, trimmed)
                    except ValueError:
                        context.user_data.pop(WAITING_KEY, None)
                        await show(update, context, "БАД не найден в твоём списке.", my_list_back(), route="mysupp:list:0")
                        return
                    context.user_data.pop(WAITING_KEY, None)
                    await show_my_supp_hub(update, context, tg_uid, supplement_id, conn=conn)
            elif waiting.startswith("mysupp:rem_add:"):
                parsed_time = _parse_user_time_input(txt)
                if not parsed_time:
                    await show(
                        update,
                        context,
                        "Не понял время. Пример: 09:30, 9.30, 9 30 или 0930",
                        my_hub_back(supplement_id),
                        route=f"mysupp:rem:add:{supplement_id}",
                    )
                else:
                    try:
                        await db.add_reminder_for_supplement(conn, tg_uid, supplement_id, parsed_time)
                    except ValueError:
                        context.user_data.pop(WAITING_KEY, None)
                        await show(update, context, "БАД не найден в твоём списке.", my_list_back(), route="mysupp:list:0")
                        return
                    context.user_data.pop(WAITING_KEY, None)
                    await _show_my_supp_reminders(update, context, conn, tg_uid, supplement_id)
            elif waiting == "schedule:add_custom":
                parsed_time = _parse_user_time_input(txt)
                user_id = await db.get_or_create_user(conn, tg_uid, tz_name="")
                if not parsed_time:
                    await show(update, context, "Не понял время. Пример: 16:00", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="settings:notifications")]]), route="schedule:add_custom")
                else:
                    await db.add_custom_slot(conn, user_id, parsed_time)
                    context.user_data.pop(WAITING_KEY, None)
                    await _show_schedule_home(update, context, conn, tg_uid)
            elif waiting.startswith("schedule:slot_time_set:"):
                parsed_time = _parse_user_time_input(txt)
                slot_id = waiting.split(":", 2)[2]
                user_id = await db.get_or_create_user(conn, tg_uid, tz_name="")
                if not parsed_time:
                    await show(update, context, "Не понял время. Пример: 08:30", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="settings:notifications")]]), route=f"schedule:slot_time_set:{slot_id}")
                else:
                    try:
                        await db.set_user_slot_time(conn, user_id, slot_id, parsed_time)
                    except ValueError:
                        context.user_data.pop(WAITING_KEY, None)
                        await show(update, context, "Этот слот уже удалён. Выбери другой.", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="settings:notifications")]]), route="settings:notifications")
                        return
                    context.user_data.pop(WAITING_KEY, None)
                    await _show_schedule_home(update, context, conn, tg_uid)
            elif waiting.startswith("mysupp:addslot:newtime:"):
                supplement_id = int(waiting.split(":")[-1])
                parsed_time = _parse_user_time_input(txt)
                if not parsed_time:
                    await show(
                        update,
                        context,
                        "Не понял время. Пример: 08:30",
                        InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"mysupp:addslot:{supplement_id}")]]),
                        route=f"mysupp:addslot:newtime:{supplement_id}",
                    )
                else:
                    user_id = await db.get_or_create_user(conn, tg_uid, tz_name="")
                    await db.add_custom_slot(conn, user_id, parsed_time, parsed_time)
                    context.user_data.pop(WAITING_KEY, None)
                    await _show_add_to_stack_slot_picker(update, context, conn, tg_uid, supplement_id)
            elif waiting.startswith("supplement:set_override:"):
                parsed_time = _parse_user_time_input(txt)
                user_id = await db.get_or_create_user(conn, tg_uid, tz_name="")
                split_ctx = context.user_data.get("split_flow")
                split_origin_id = None
                if isinstance(split_ctx, dict) and int(split_ctx.get("new_id") or 0) == supplement_id:
                    split_origin_id = int(split_ctx.get("origin_id") or 0) or None
                if not parsed_time:
                    back_markup = my_hub_back(supplement_id)
                    if split_origin_id:
                        back_markup = InlineKeyboardMarkup(
                            [[InlineKeyboardButton("✖️ Отменить второй приём", callback_data=f"mysupp:split:cancel:{supplement_id}:{split_origin_id}")]]
                        )
                    await show(update, context, "Не понял время. Пример: 16:00", back_markup, route=f"supplement:set_override:{supplement_id}")
                else:
                    try:
                        await _save_bot_supplement_schedule(conn, user_id, supplement_id, time_override=parsed_time, set_override=True)
                    except ValueError:
                        context.user_data.pop(WAITING_KEY, None)
                        await _show_supplement_time_screen(update, context, conn, tg_uid, supplement_id)
                        return
                    context.user_data.pop(WAITING_KEY, None)
                    await _show_supplement_time_screen(update, context, conn, tg_uid, supplement_id, split_origin_id=split_origin_id)
            else:
                parsed_time = _parse_user_time_input(txt)
                slot_id = waiting.split(":", 2)[2]
                user_id = await db.get_or_create_user(conn, tg_uid, tz_name="")
                if not parsed_time:
                    await show(
                        update,
                        context,
                        "Не понял время. Пример: 08:30",
                        InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="rem:home")]]),
                        route=f"rem:set:{slot_id}",
                    )
                else:
                    try:
                        await db.set_user_slot_time(conn, user_id, slot_id, parsed_time)
                    except ValueError:
                        context.user_data.pop(WAITING_KEY, None)
                        await show(update, context, "Этот слот уже удалён. Выбери другой.", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="rem:home")]]), route="rem:home")
                        return
                    context.user_data.pop(WAITING_KEY, None)
                    await show(update, context, "Время обновлено.", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="rem:home")]]), route="rem:home")
        finally:
            await conn.close()

        try:
            await update.message.delete()
        except Exception:
            pass
        return

    if not waiting:
        return

    s = context.application.bot_data["settings"]
    tg_uid = update.effective_user.id
    conn = await db.connect(s.DB_PATH)
    try:
        user_id = await db.get_or_create_user(conn, tg_uid, tz_name="")
        tz_name = await db.get_user_tz(conn, tg_uid)
        if _is_tz_unset(tz_name):
            context.user_data.pop("tz_back_data", None)
            context.user_data["onboarding"] = "tz"
            await _show_timezone_picker(update, context, 0)
            return

        if waiting == "cx:create":
            name = txt.strip()[:120]
            if not name:
                await show(update, context, "Название не может быть пустым.", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="cx:list")]]), route="cx:create")
            else:
                try:
                    created = await db.create_complex(conn, tg_uid, name)
                    context.user_data.pop(WAITING_KEY, None)
                    await _show_complex_detail(update, context, conn, tg_uid, int(created.get("id") or 0))
                except Exception:
                    await show(update, context, "Не удалось создать комплекс (возможно, такое имя уже есть).", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="cx:list")]]), route="cx:create")
            return
        if waiting.startswith("cx:rename:"):
            complex_id = int(waiting.split(":", 2)[2])
            name = txt.strip()[:120]
            if not name:
                await show(update, context, "Название не может быть пустым.", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"cx:open:{complex_id}")]]), route=f"cx:rename:{complex_id}")
            else:
                try:
                    await db.rename_complex(conn, tg_uid, complex_id, name)
                except ValueError as exc:
                    if str(exc) == "complex_not_found":
                        context.user_data.pop(WAITING_KEY, None)
                        await _show_complexes_hub(update, context, conn, tg_uid)
                    else:
                        await show(update, context, "Комплекс с таким названием уже есть. Введи другое название.", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"cx:open:{complex_id}")]]), route=f"cx:rename:{complex_id}")
                    return
                context.user_data.pop(WAITING_KEY, None)
                await _show_complex_detail(update, context, conn, tg_uid, complex_id)
            return
        if waiting.startswith("cx:add_query:"):
            complex_id = int(waiting.split(":", 2)[2])
            rows, _ = await db.search_supplements(conn, txt, page=0, page_size=15)
            if not rows:
                await show(update, context, "Не нашёл в каталоге. Введи другое название.", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"cx:add:{complex_id}")]]), route=f"cx:add:catalog:{complex_id}")
            else:
                await show(update, context, "Выбери добавку для комплекса:", complex_add_results_kb(complex_id, rows), route=f"cx:add:{complex_id}")
            context.user_data.pop(WAITING_KEY, None)
            return
        if waiting.startswith("cx:item:dose:"):
            _, _, _, cx_id, supp_id = waiting.split(":", 4)
            try:
                await db.set_complex_item_dose(conn, tg_uid, int(cx_id), int(supp_id), txt)
            except ValueError:
                context.user_data.pop(WAITING_KEY, None)
                await _show_complex_detail(update, context, conn, tg_uid, int(cx_id))
                return
            context.user_data.pop(WAITING_KEY, None)
            await _show_complex_detail(update, context, conn, tg_uid, int(cx_id))
            return

        if waiting == "add_query":
            rows, _ = await db.search_supplements(conn, txt, page=0, page_size=10)
            if not rows:
                await show(update, context, "Не нашёл в каталоге.", back(), route="menu:add")
            else:
                await show(
                    update,
                    context,
                    "Выбери добавку для добавления:",
                    pick_search_results(rows, "add_pick"),
                    route="menu:add",
                )
            context.user_data.pop(WAITING_KEY, None)

        elif waiting == "compat_query":
            rows, _ = await db.search_supplements(conn, txt, page=0, page_size=10)
            if not rows:
                await show(update, context, "Не нашёл в каталоге.", back(), route="menu:compat")
            else:
                await show(
                    update,
                    context,
                    "Выбери добавку:",
                    pick_search_results(rows, "compat_pick"),
                    route="menu:compat",
                )
            context.user_data.pop(WAITING_KEY, None)

        elif waiting == "mysupp_add_query":
            rows, _ = await db.search_supplements(conn, txt, page=0, page_size=10)
            if not rows:
                await show(update, context, "Не нашёл в каталоге.", my_list_back(), route="mysupp:add")
            else:
                await show(
                    update,
                    context,
                    "Выбери добавку для добавления:",
                    pick_search_results(rows, "mysupp:add_pick"),
                    route="mysupp:add",
                )
            context.user_data.pop(WAITING_KEY, None)
        elif waiting == "initial_pick_goal":
            context.user_data.pop(WAITING_KEY, None)
            if not s.OPENAI_API_KEY:
                await show(
                    update,
                    context,
                    "Начальный подбор недоступен: добавь OPENAI_API_KEY в .env.",
                    back(),
                    route="menu:ai",
                )
            else:
                await db.start_onboarding_state(conn, tg_uid, goal_text=txt)
                sups = await db.list_user_supplements(conn, tg_uid, limit=100)
                user_supp_text = ", ".join([r["name"] for r in sups]) or "нет"
                cfg = OpenAIConfig(api_key=s.OPENAI_API_KEY, model=s.OPENAI_MODEL)
                await show(update, context, "🤖 Думаю… Это может занять 1–3 минуты.", None, route="onboard:goal")
                raw = None
                try:
                    raw = await suggest_stack_v1(cfg, txt, user_supp_text)
                except Exception:
                    log.exception("OpenAI request failed")
                    await show(update, context, "Начальный подбор временно недоступен.", back(), route="onboard:goal")

                await _render_ai_suggestions(update, context, conn, txt, raw)
        elif waiting == AI_CHECKIN_TEXT_WAIT:
            if not await require_premium_or_upsell(update, context, "AI check-in"):
                context.user_data.pop(WAITING_KEY, None)
                return
            context.user_data.pop(WAITING_KEY, None)
            if not s.OPENAI_API_KEY:
                await show(
                    update,
                    context,
                    "AI отключен: добавь OPENAI_API_KEY в .env.",
                    back(),
                    route=AI_CHECKIN_ROUTE,
                )
            else:
                await show(update, context, "Готовлю рекомендации…", None, route=AI_CHECKIN_ROUTE)
                extracted: dict = {}
                try:
                    cfg = OpenAIConfig(api_key=s.OPENAI_API_KEY, model=s.OPENAI_MODEL)
                    extracted = await extract_checkin_from_text(txt, cfg=cfg)
                except Exception:
                    log.exception("Checkin extraction failed")
                    extracted = {}
                answers = _normalize_ai_checkin_extract(
                    extracted if isinstance(extracted, dict) else {},
                    txt,
                )
                state = {
                    "step": 5,
                    "target_day": _get_ai_checkin_target_day(context),
                    "answers": answers,
                    "note_pending": False,
                    "last_ai_actions_json": "",
                    "selected_action_ids": [],
                }
                _set_ai_checkin_state(context, state)
                user_id = await db.get_or_create_user(conn, tg_uid, tz_name="")
                tz_name = await db.get_user_tz(conn, tg_uid)
                if _is_tz_unset(tz_name):
                    context.user_data.pop("tz_back_data", None)
                    context.user_data["onboarding"] = "tz"
                    await _show_timezone_picker(update, context, 0)
                else:
                    await _finalize_ai_checkin(
                        update,
                        context,
                        conn,
                        tg_uid=tg_uid,
                        user_id=user_id,
                        tz_name=tz_name or "UTC",
                        state=state,
                    )
        elif waiting == AI_CHECKIN_NOTE_WAIT:
            state = _get_ai_checkin_state(context)
            answers = state.get("answers", {}) if isinstance(state.get("answers"), dict) else {}
            answers["note"] = _truncate_text(txt, 500)
            state["answers"] = answers
            state["note_pending"] = False
            _set_ai_checkin_state(context, state)
            context.user_data.pop(WAITING_KEY, None)
            await _render_ai_checkin_wizard(update, context, state=state)

    finally:
        await conn.close()


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard_not_banned(update, context):
        return
    if not update.message or not update.message.voice:
        return
    s = context.application.bot_data["settings"]
    tg_uid = update.effective_user.id

    conn = await db.connect(s.DB_PATH)
    try:
        ui_message_id, ui_screen, ui_payload = await db.get_ui_state(conn, tg_uid)
    finally:
        await conn.close()

    if ui_screen == AI_CHECKIN_VOICE_ROUTE:
        if not await require_premium_or_upsell(update, context, "AI check-in"):
            return
        try:
            payload_data = json.loads(ui_payload) if ui_payload else {}
        except json.JSONDecodeError:
            payload_data = {}
        if not isinstance(payload_data, dict):
            payload_data = {}
        voice = update.message.voice
        if (
            (voice.duration and voice.duration > CHECKIN_VOICE_MAX_SECONDS)
            or (voice.file_size and voice.file_size > CHECKIN_VOICE_MAX_BYTES)
        ):
            await show(
                update,
                context,
                f"Слишком длинно. Запиши до {CHECKIN_VOICE_MAX_SECONDS} секунд, пожалуйста.",
                ai_checkin_voice_kb(),
                route=AI_CHECKIN_VOICE_ROUTE,
                payload=payload_data or _ai_checkin_voice_payload(),
                target_message_id=ui_message_id,
            )
            try:
                await update.message.delete()
            except Exception:
                pass
            return

        await show(
            update,
            context,
            "Распознаю голос…",
            None,
            route=AI_CHECKIN_VOICE_ROUTE,
            payload=payload_data or _ai_checkin_voice_payload(),
            target_message_id=ui_message_id,
        )

        if not s.OPENAI_API_KEY:
            await show(
                update,
                context,
                "AI отключен: добавь OPENAI_API_KEY в .env.",
                ai_checkin_voice_kb(),
                route=AI_CHECKIN_VOICE_ROUTE,
                payload=payload_data or _ai_checkin_voice_payload(),
                target_message_id=ui_message_id,
            )
            try:
                await update.message.delete()
            except Exception:
                pass
            return

        voice_path: Path | None = None
        try:
            try:
                voice_path = await download_telegram_voice(update, context)
            except Exception:
                await show(
                    update,
                    context,
                    "Не смог скачать голосовое. Попробуй ещё раз.",
                    ai_checkin_voice_kb(),
                    route=AI_CHECKIN_VOICE_ROUTE,
                    payload=payload_data or _ai_checkin_voice_payload(),
                    target_message_id=ui_message_id,
                )
                return

            transcribe_cfg = OpenAITranscribeConfig(
                api_key=s.OPENAI_API_KEY,
                model=s.OPENAI_STT_MODEL,
            )
            transcription = ""
            try:
                transcription = await transcribe_audio(transcribe_cfg, str(voice_path))
            except Exception:
                await show(
                    update,
                    context,
                    "Не получилось распознать голос. Попробуй ещё раз короче и ближе к микрофону.",
                    ai_checkin_voice_kb(),
                    route=AI_CHECKIN_VOICE_ROUTE,
                    payload=payload_data or _ai_checkin_voice_payload(),
                    target_message_id=ui_message_id,
                )
                return

            transcription = transcription.strip()
            if not transcription:
                await show(
                    update,
                    context,
                    "Не получилось распознать голос. Попробуй ещё раз короче и ближе к микрофону.",
                    ai_checkin_voice_kb(),
                    route=AI_CHECKIN_VOICE_ROUTE,
                    payload=payload_data or _ai_checkin_voice_payload(),
                    target_message_id=ui_message_id,
                )
                return

            await show(
                update,
                context,
                "Готовлю рекомендации…",
                None,
                route=AI_CHECKIN_VOICE_ROUTE,
                payload=payload_data or _ai_checkin_voice_payload(),
                target_message_id=ui_message_id,
            )

            extracted: dict = {}
            try:
                cfg = OpenAIConfig(api_key=s.OPENAI_API_KEY, model=s.OPENAI_MODEL)
                extracted = await extract_checkin_from_text(transcription, cfg=cfg)
            except Exception:
                log.exception("Checkin extraction failed")
                extracted = {}

            answers = _normalize_ai_checkin_extract(
                extracted if isinstance(extracted, dict) else {},
                transcription,
            )
            state = {
                "step": 5,
                "target_day": _get_ai_checkin_target_day(context),
                "answers": answers,
                "note_pending": False,
                "last_ai_actions_json": "",
                "selected_action_ids": [],
            }
            _set_ai_checkin_state(context, state)

            conn = await db.connect(s.DB_PATH)
            try:
                user_id = await db.get_or_create_user(conn, tg_uid, tz_name="")
                tz_name = await db.get_user_tz(conn, tg_uid)
                if _is_tz_unset(tz_name):
                    context.user_data.pop("tz_back_data", None)
                    context.user_data["onboarding"] = "tz"
                    await _show_timezone_picker(
                        update,
                        context,
                        0,
                        target_message_id=ui_message_id,
                    )
                    return
                await _finalize_ai_checkin(
                    update,
                    context,
                    conn,
                    tg_uid=tg_uid,
                    user_id=user_id,
                    tz_name=tz_name or "UTC",
                    state=state,
                    target_message_id=ui_message_id,
                )
            finally:
                await conn.close()
        finally:
            if voice_path:
                try:
                    voice_path.unlink()
                except Exception:
                    pass
            try:
                await update.message.delete()
            except Exception:
                pass
        return

    waiting = context.user_data.get(WAITING_KEY)
    if waiting != "initial_pick_goal":
        return
    voice = update.message.voice
    if ((voice.duration and voice.duration > CHECKIN_VOICE_MAX_SECONDS)
            or (voice.file_size and voice.file_size > CHECKIN_VOICE_MAX_BYTES)):
        await show(update, context, f"Слишком длинно. Запиши до {CHECKIN_VOICE_MAX_SECONDS} секунд, пожалуйста.", back(), route="onboard:goal")
        return

    try:
        await update.message.delete()
    except Exception:
        pass

    conn = await db.connect(s.DB_PATH)
    try:
        user_id = await db.get_or_create_user(conn, tg_uid, tz_name="")
        tz_name = await db.get_user_tz(conn, tg_uid)
        if _is_tz_unset(tz_name):
            context.user_data.pop("tz_back_data", None)
            context.user_data["onboarding"] = "tz"
            await _show_timezone_picker(update, context, 0)
            return

        if not s.OPENAI_API_KEY:
            await show(
                update,
                context,
                "Начальный подбор недоступен: добавь OPENAI_API_KEY в .env.",
                back(),
                route="menu:ai",
            )
            return

        route = "onboard:goal"
        await show(update, context, "🎙️ Принял голосовое. Расшифровываю...", back(), route=route)
        try:
            audio_bytes, filename = await _download_voice_bytes(update, context)
        except Exception:
            await show(
                update,
                context,
                "Не смог скачать голосовое. Попробуй ещё раз.",
                back(),
                route=route,
            )
            return

        transcribe_cfg = OpenAITranscribeConfig(
            api_key=s.OPENAI_API_KEY,
            model=s.OPENAI_STT_MODEL,
        )
        transcription = ""
        try:
            transcription = await transcribe_voice_v1(transcribe_cfg, audio_bytes, filename)
        except Exception:
            await show(
                update,
                context,
                "Сейчас недоступна расшифровка. Попробуй позже.",
                back(),
                route=route,
            )
            return

        transcription = transcription.strip()
        if not transcription:
            await show(
                update,
                context,
                "Не смог распознать речь. Попробуй ещё раз.",
                back(),
                route=route,
            )
            return
        await db.start_onboarding_state(conn, tg_uid, goal_text=transcription, prefix_line=f"🎙️ Понял цель: <b>{transcription}</b>")
        sups = await db.list_user_supplements(conn, tg_uid, limit=100)
        user_supp_text = ", ".join([r["name"] for r in sups]) or "нет"
        cfg = OpenAIConfig(api_key=s.OPENAI_API_KEY, model=s.OPENAI_MODEL)
        await show(update, context, "🤖 Думаю… Это может занять 1–3 минуты.", None, route="onboard:goal")
        raw = None
        try:
            raw = await suggest_stack_v1(cfg, transcription, user_supp_text)
        except Exception:
            log.exception("OpenAI request failed")
            await show(update, context, "Начальный подбор временно недоступен.", back(), route="onboard:goal")
            return

        rendered = await _render_ai_suggestions(
            update,
            context,
            conn,
            transcription,
            raw,
            prefix_line=f"🎙️ Понял цель: <b>{transcription}</b>",
        )
        if rendered:
            context.user_data.pop(WAITING_KEY, None)
    finally:
        await conn.close()


def _parse_utc_iso(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            return dt
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


def _fmt_avg(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _fixed_rows(rows: list[tuple[str, str]]) -> str:
    width = max((len(k) for k, _ in rows), default=0)
    return "\n".join(f"{k.ljust(width)}  {v}" for k, v in rows)


def _payment_subject_from_row(row: dict) -> tuple[str, str]:
    payload = str(row.get("payload") or "")
    parts = payload.split(":")
    if len(parts) >= 4 and parts[0] == "sub":
        plan_id = str(parts[1] or "")
        return "subscription", f"Premium {plan_id}"
    kind = str(row.get("payment_kind") or "").strip() or "payment"
    return kind, payload[:60] if payload else "—"


def _payment_user_line(row: dict) -> str:
    tg_user_id = int(row.get("tg_user_id") or 0)
    username = str(row.get("tg_username") or "").strip()
    full_name = (f"{str(row.get('tg_first_name') or '').strip()} {str(row.get('tg_last_name') or '').strip()}").strip() or "—"
    mention = f'<a href="tg://user?id={tg_user_id}">{html.escape(full_name)}</a>' if tg_user_id > 0 else html.escape(full_name)
    uname = f"@{username}" if username else "—"
    return f"{mention} · {uname} · <code>{tg_user_id}</code>"


async def _show_admin_paychecks(update: Update, context: ContextTypes.DEFAULT_TYPE, conn, *, filter_code: str = "wants_receipt", index: int = 0) -> None:
    rows = await db.admin_list_receipt_payments(conn, filter_code=filter_code, limit=500)
    total = len(rows)
    if total <= 0:
        await show(
            update,
            context,
            "💳 <b>Чеки / RUB-оплаты</b>\nПо выбранному фильтру платежей нет.",
            admin_paychecks_kb(index=0, total=1, filter_code=filter_code, current_receipt_status="pending"),
            route="admin:paychecks",
            payload={"filter": filter_code, "index": 0},
        )
        return
    safe_index = max(0, min(int(index), total - 1))
    row = rows[safe_index]
    kind, subject = _payment_subject_from_row(row)
    amount_rub = int(row.get("amount") or 0) / 100
    paid_at = str(row.get("finalized_at_utc") or row.get("created_at_utc") or "")
    email = str(row.get("receipt_email") or "").strip() or "—"
    email_status = str(row.get("receipt_email_status") or "missing")
    receipt_status = str(row.get("receipt_status") or "pending")
    provider_pid = str(row.get("provider_payment_charge_id") or "") or "—"
    lines = [
        "💳 <b>Чек / успешный RUB-платёж</b>",
        f"#{safe_index + 1} из {total}",
        f"Оплачен: <code>{paid_at or '—'}</code>",
        f"Payment record id: <code>{int(row.get('id') or 0)}</code>",
        f"Provider payment id: <code>{provider_pid}</code>",
        f"Сумма: <b>{amount_rub:.2f}</b> {str(row.get('currency') or 'RUB')}",
        f"Что куплено: {html.escape(subject)}",
        f"product_kind: <code>{html.escape(kind)}</code>",
        f"Пользователь: {_payment_user_line(row)}",
        f"Email: <code>{html.escape(email)}</code>",
        f"email_status: <code>{html.escape(email_status)}</code>",
        f"receipt_status: <code>{html.escape(receipt_status)}</code>",
    ]
    await show(
        update,
        context,
        "\n".join(lines),
        admin_paychecks_kb(index=safe_index, total=total, filter_code=filter_code, current_receipt_status=receipt_status),
        route="admin:paychecks",
        payload={"filter": filter_code, "index": safe_index, "payment_id": int(row.get("id") or 0)},
    )


async def _admin_dashboard_text(conn) -> str:
    base = await db.admin_get_global_stats(conn)
    now_iso = db.utc_now_iso()

    premium_row = await db.fetchone(conn, "SELECT COUNT(*) AS c FROM users WHERE COALESCE(premium_until_utc,'') > ?", (now_iso,))
    premium_active = int(premium_row["c"] or 0) if premium_row else 0

    starter_row = await db.fetchone(conn, "SELECT COUNT(*) AS c FROM users WHERE COALESCE(starter_trial_until_utc,'') > ?", (now_iso,))
    starter_active = int(starter_row["c"] or 0) if starter_row else 0

    access_row = await db.fetchone(
        conn,
        "SELECT COUNT(*) AS c FROM users WHERE COALESCE(premium_until_utc,'') > ? OR COALESCE(starter_trial_until_utc,'') > ?",
        (now_iso, now_iso),
    )
    webapp_access = int(access_row["c"] or 0) if access_row else 0

    avg_stack_row = await db.fetchone(
        conn,
        """SELECT AVG(cnt) AS avg_cnt FROM (
               SELECT user_id, COUNT(*) AS cnt
               FROM user_supplements
               WHERE is_active=1
               GROUP BY user_id
           )""",
    )
    avg_stack = float(avg_stack_row["avg_cnt"]) if avg_stack_row and avg_stack_row["avg_cnt"] is not None else None

    top_stack_row = await db.fetchone(
        conn,
        """SELECT COALESCE(s.name_ru, s.slug, CAST(us.supplement_id AS TEXT)) AS name, COUNT(*) AS c
           FROM user_supplements us
           LEFT JOIN supplements s ON s.id=us.supplement_id
           WHERE us.is_active=1
           GROUP BY us.supplement_id
           ORDER BY c DESC
           LIMIT 1""",
    )
    top_stack_name = str(top_stack_row["name"]) if top_stack_row else "—"
    top_stack_count = int(top_stack_row["c"] or 0) if top_stack_row else 0

    top_taken_row = None
    intake_cols = await db._table_columns(conn, "user_intake_log") if await db._table_exists(conn, "user_intake_log") else set()
    if {"supplement_id", "status", "local_date"}.issubset(intake_cols):
        top_taken_row = await db.fetchone(
            conn,
            """SELECT COALESCE(s.name_ru, s.slug, CAST(uil.supplement_id AS TEXT)) AS name, COUNT(*) AS c
               FROM user_intake_log uil
               LEFT JOIN supplements s ON s.id=uil.supplement_id
               WHERE uil.status='taken' AND COALESCE(uil.local_date,'') >= date('now','-30 day')
               GROUP BY uil.supplement_id
               ORDER BY c DESC
               LIMIT 1""",
        )
    top_taken_name = str(top_taken_row["name"]) if top_taken_row else "—"
    top_taken_count = int(top_taken_row["c"] or 0) if top_taken_row else 0

    pay_rows = await db.fetchall(conn, "SELECT payload, amount, currency FROM payments WHERE status='paid'")
    total_paid = len(pay_rows)
    paid_stars = sum(int(r["amount"] or 0) for r in pay_rows if str(r["currency"] or "") == "XTR")
    paid_rub_kop = sum(int(r["amount"] or 0) for r in pay_rows if str(r["currency"] or "") == "RUB")

    by_plan: dict[str, int] = {}
    for r in pay_rows:
        payload = str(r["payload"] or "")
        parts = payload.split(":")
        if len(parts) >= 4 and parts[0] == "sub":
            by_plan[parts[1]] = by_plan.get(parts[1], 0) + 1
    top_plan = max(by_plan.items(), key=lambda x: x[1])[0] if by_plan else "—"

    rows = [
        ("Users total", str(int(base.get("users_total") or 0))),
        ("Active 1d/7d/30d", f"{int(base.get('active_1d') or 0)} / {int(base.get('active_7d') or 0)} / {int(base.get('active_30d') or 0)}"),
        ("Premium active", str(premium_active)),
        ("Starter trial active", str(starter_active)),
        ("WebApp access now", str(webapp_access)),
        ("Avg active supplements", _fmt_avg(avg_stack)),
        ("Avg reminders", _fmt_avg(base.get("avg_enabled_reminders"))),
        ("Check-ins 30d", str(int(base.get("checkins_30d_total") or 0))),
        ("Intakes 30d", str(int(base.get("intake_30d_total") or 0))),
        ("Top supplement in stacks", f"{top_stack_name} ({top_stack_count})"),
        ("Top taken supplement 30d", f"{top_taken_name} ({top_taken_count})"),
        ("Paid subscriptions total", str(total_paid)),
        ("Paid by top plan", top_plan),
        ("Revenue RUB", f"{paid_rub_kop / 100:.2f} ₽"),
        ("Revenue Stars", f"{paid_stars} ⭐"),
    ]
    return "📊 <b>Admin Stats</b>\n<pre>" + _fixed_rows(rows) + "</pre>"


async def _ensure_admin_for_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return False
    if _is_admin(user.id):
        return True
    role, _ = await _get_role_perms_ctx(context, user.id)
    if role != db.ROLE_NONE:
        return True
    await (update.effective_message or update.message).reply_text("Эта команда доступна только администратору.")
    return False


async def paychecks_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard_not_banned(update, context):
        return
    if not await _ensure_admin_for_command(update, context):
        return
    user = update.effective_user
    if not user:
        return
    conn = await db.connect(context.application.bot_data["settings"].DB_PATH)
    try:
        role, perms = await _get_role_perms_ctx(context, user.id)
        if role == db.ROLE_NONE or not perms.get("payments_view", False):
            await (update.effective_message or update.message).reply_text("Недостаточно прав для просмотра оплат.")
            return
        await _show_admin_paychecks(update, context, conn, filter_code="wants_receipt", index=0)
    finally:
        await conn.close()


async def admin_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard_not_banned(update, context):
        return
    if not await _ensure_admin_for_command(update, context):
        return
    s = context.application.bot_data["settings"]
    conn = await db.connect(s.DB_PATH)
    try:
        text = await _admin_dashboard_text(conn)
    finally:
        await conn.close()
    await show(update, context, text, admin_stats_kb(), route="admin:stats")


async def grant_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard_not_banned(update, context):
        return
    if not await _ensure_admin_for_command(update, context):
        return
    args = list(context.args or [])
    if len(args) != 2 or (not args[0].isdigit()) or (not args[1].isdigit()):
        await (update.effective_message or update.message).reply_text("Использование: /grant <tg_user_id> <days>")
        return
    target_tg_id = int(args[0])
    days = int(args[1])
    if days <= 0 or days > 3650:
        await (update.effective_message or update.message).reply_text("days должен быть в диапазоне 1..3650")
        return

    s = context.application.bot_data["settings"]
    conn = await db.connect(s.DB_PATH)
    try:
        await db.get_or_create_user(conn, target_tg_id)
        current_iso = await db.get_premium_until(conn, target_tg_id)
        now = datetime.utcnow()
        current_dt = _parse_utc_iso(current_iso)
        base = current_dt if current_dt and current_dt > now else now
        new_until = base + timedelta(days=days)
        until_iso = await db.set_premium_until(conn, target_tg_id, new_until, source=f"grant:{update.effective_user.id}", plan_id=f"grant_{days}d")
    finally:
        await conn.close()

    await (update.effective_message or update.message).reply_text(
        f"✅ Premium выдан пользователю {target_tg_id} на {days} дн.\nАктивен до: {until_iso}"
    )


async def degrant_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard_not_banned(update, context):
        return
    if not await _ensure_admin_for_command(update, context):
        return
    args = list(context.args or [])
    if len(args) != 2 or (not args[0].isdigit()) or (not args[1].isdigit()):
        await (update.effective_message or update.message).reply_text("Использование: /degrant <tg_user_id> <days>")
        return
    target_tg_id = int(args[0])
    days = int(args[1])
    if days <= 0 or days > 3650:
        await (update.effective_message or update.message).reply_text("days должен быть в диапазоне 1..3650")
        return

    s = context.application.bot_data["settings"]
    conn = await db.connect(s.DB_PATH)
    try:
        await db.get_or_create_user(conn, target_tg_id)
        current_iso = await db.get_premium_until(conn, target_tg_id)
        now = datetime.utcnow()
        current_dt = _parse_utc_iso(current_iso)
        if not current_dt or current_dt <= now:
            until_iso = await db.set_premium_until(conn, target_tg_id, now, source=f"degrant:{update.effective_user.id}", plan_id="manual_zero")
        else:
            new_until = current_dt - timedelta(days=days)
            if new_until < now:
                new_until = now
            until_iso = await db.set_premium_until(conn, target_tg_id, new_until, source=f"degrant:{update.effective_user.id}", plan_id=f"degrant_{days}d")
    finally:
        await conn.close()

    await (update.effective_message or update.message).reply_text(
        f"✅ Premium скорректирован для {target_tg_id}.\nНовый срок: {until_iso}"
    )


async def purge_me_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard_not_banned(update, context):
        return
    if not update.effective_user:
        return
    s = context.application.bot_data["settings"]
    conn = await db.connect(s.DB_PATH)
    try:
        out = await db.purge_user_data(conn, update.effective_user.id)
    finally:
        await conn.close()

    context.user_data.clear()
    deleted = bool(out.get("deleted"))
    if deleted:
        await (update.effective_message or update.message).reply_text(
            "🧹 Ваши записи и профиль очищены. Оплаченный доступ сохранён.\nНажмите /start, чтобы пройти начальный подбор заново."
        )
    else:
        await (update.effective_message or update.message).reply_text(
            "Данных для очистки не найдено. Нажмите /start."
        )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    frames = traceback.extract_tb(error.__traceback__) if isinstance(error, BaseException) else []
    trace = " > ".join(f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}" for frame in frames)
    log.error("Unhandled update error update_id=%s type=%s trace=%s",
              getattr(update, "update_id", None), type(error).__name__, trace)

async def _supplements_count(db_path: str) -> int:
    conn = await db.connect(db_path)
    try:
        row = await db.fetchone(conn, "SELECT COUNT(*) FROM supplements")
        return int(row[0]) if row else 0
    finally:
        await conn.close()

async def _maybe_seed_catalog(s) -> None:
    if not s.AUTO_SEED_ON_START:
        return

    existing = await _supplements_count(s.DB_PATH)
    supp_path = s.SEED_SUPPLEMENTS_PATH
    inter_path = s.SEED_INTERACTIONS_PATH
    if not supp_path or not os.path.exists(supp_path):
        log.warning("Supplements seed file missing, skipping auto-seed: %s", supp_path)
        return

    if inter_path and not os.path.exists(inter_path):
        log.warning("Interactions seed file missing, seeding supplements only: %s", inter_path)
        inter_path = None

    await seed_import._import(supp_path, inter_path, only_missing=existing > 0)
    seeded = await _supplements_count(s.DB_PATH)
    log.info("Seeded OK: %s supplements", seeded)

async def _bootstrap() -> Application:
    s = get_settings()
    await db.init_db(s.DB_PATH)
    await _maybe_seed_catalog(s)

    app = Application.builder().token(s.TG_TOKEN).build()
    app.bot_data["settings"] = s
    app.bot_data["render_route"] = _render_route

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("premium", premium_command))
    app.add_handler(CommandHandler("stats", admin_stats_command))
    app.add_handler(CommandHandler("paychecks", paychecks_command))
    app.add_handler(CommandHandler("grant", grant_command))
    app.add_handler(CommandHandler("degrant", degrant_command))
    app.add_handler(CommandHandler("purge_me", purge_me_command))
    app.add_handler(CallbackQueryHandler(
        on_menu,
        pattern=r"^(main$|menu:|settings:|gift:|schedule:|cx:|supplement:|admin:|add_pick:|compat_pick:|cat:|curated:|supp:|card:|mysupp:|today:|intake:|premium:|onboard:|starter:|consent:|rem:(home|edit|set:|toggle:))",
    ))
    app.add_handler(CallbackQueryHandler(on_ai_add, pattern=r"^aiadd:"))
    app.add_handler(CallbackQueryHandler(on_ai_checkin, pattern=r"^aicheck:"))
    app.add_handler(CallbackQueryHandler(on_noop, pattern=r"^noop$"))
    app.add_handler(CallbackQueryHandler(on_timezone, pattern=r"^tz:"))
    app.add_handler(CallbackQueryHandler(on_reminder_action, pattern=r"^rem:(done|skip|snooze|done_all|skip_all|snooze_all)"))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(PreCheckoutQueryHandler(on_precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, on_success_payment))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    app.job_queue.run_repeating(payments_reconcile_tick, interval=60, first=10, name="payments_reconcile")
    app.job_queue.run_repeating(reminders_tick, interval=60, first=5, name="reminders_tick")
    app.job_queue.run_repeating(nutrient_lifecycle_tick, interval=300, first=20, name="nutrient_lifecycle_tick")
    app.job_queue.run_repeating(daily_rollover_job, interval=600, first=30, name="daily_rollover")
    return app

def build_app() -> Application:
    return asyncio.run(_bootstrap())

async def _show_catalog_home(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    *,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    tg_uid = update.effective_user.id if update.effective_user else 0
    await show(
        update,
        context,
        (
            "📚 <b>Каталог БАДов</b>\n"
            "Простой старт: поиск, цель или система организма."
        ),
        catalog_home(is_admin=(await _get_role_perms_ctx(context, tg_uid))[0] != db.ROLE_NONE),
        route="cat:home",
        payload={"mode": "home", "list_mode": "", "page": 0, "query": "", "class_key": "", "body_token": "", "return_page": 0},
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )


async def _require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, perm: str | None = None) -> bool:
    tg_uid = update.effective_user.id if update.effective_user else 0
    role, perms = await _get_role_perms_ctx(context, tg_uid)
    if role != db.ROLE_NONE and (perm is None or perms.get(perm, False)):
        return True
    await show(
        update,
        context,
        "Недостаточно прав",
        InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")]]),
        route="menu:home",
    )
    return False


async def _show_admin_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_uid = update.effective_user.id if update.effective_user else 0
    _, perms = await _get_role_perms_ctx(context, tg_uid)
    await show(
        update,
        context,
        "🛠 <b>Админ-меню</b>\n"
        "- Управление ссылками на покупку\n"
        "(позже: правки карточек, доказательность, тексты и т.д.)",
        admin_hub_kb(perms=perms),
        route="admin:home",
    )


async def _show_admin_supp_results(update: Update, context: ContextTypes.DEFAULT_TYPE, conn, q: str, offset: int = 0) -> None:
    query = (q or "").strip()
    if len(query) < 2:
        await show(
            update,
            context,
            "Запрос слишком короткий. Напиши минимум 2 символа.",
            InlineKeyboardMarkup([[InlineKeyboardButton("↩️ В админ-меню", callback_data="admin:home")]]),
            route="admin_wait_query",
            payload={"admin_query": "", "offset": 0},
        )
        return
    offset = max(0, int(offset))
    rows = await db.search_supplements(conn, query, limit=10, offset=offset)
    has_next = len(rows) == 10
    lines = [f"🔎 <b>Результаты поиска</b>: <code>{_escaped_text_chunks(_admin_preview(query, 300), 500)[0]}</code>"]
    if not rows:
        lines.append("\nНичего не найдено.")
    else:
        for idx, item in enumerate(rows, start=offset + 1):
            title = (item.get("name_ru") or item.get("name_en") or item.get("slug") or "Без названия").strip()
            grade = (item.get("evidence_grade") or "?").strip() or "?"
            mark = "🔗" if item.get("has_link") else "—"
            lines.append(f"{idx}) {html.escape(title)} ({html.escape(grade)}) {mark}")
    await show(
        update,
        context,
        "\n".join(lines),
        admin_supp_results_kb(rows, offset=offset, has_prev=offset > 0, has_next=has_next),
        route="admin:supp_list",
        payload={"admin_query": query, "offset": offset},
    )


async def _show_admin_supp_card(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    supplement_id: int,
    *,
    admin_query: str = "",
    offset: int = 0,
) -> None:
    row = await db.get_supplement_admin(conn, supplement_id)
    if not row:
        await show(update, context, "Добавка не найдена.", admin_hub_kb(), route="admin:home")
        return
    name = (row.get("name_ru") or row.get("name_en") or row.get("slug") or "Без названия").strip()
    purchase_url = str(row.get("purchase_url") or "").strip()
    active = int(row.get("is_active") or 0)
    active_label = "🟢 В каталоге" if active else "⚪ Скрыт"
    await show(
        update,
        context,
        "\n".join([
            f"🧾 <b>{html.escape(name)}</b>",
            f"EN: {html.escape(str(row.get('name_en') or '—'))}",
            f"slug: <code>{html.escape(str(row.get('slug') or ''))}</code>",
            f"Статус: {active_label}",
            f"Evidence / Confidence: {(row.get('evidence_grade') or '—')} / {(row.get('confidence') or '—')}",
            f"Ссылка покупки: {html.escape(purchase_url) if purchase_url else 'нет'}",
        ]),
        admin_supp_card_kb(int(row["id"]), purchase_url),
        route=f"admin:supp_open:{int(row['id'])}",
        payload={"supplement_id": int(row["id"]), "admin_query": admin_query, "offset": int(offset)},
    )


async def _show_admin_supp_edit_picker(update: Update, context: ContextTypes.DEFAULT_TYPE, conn, supplement_id: int) -> None:
    row = await db.get_supplement_admin(conn, int(supplement_id))
    if not row:
        await show(update, context, "Добавка не найдена.", admin_hub_kb(), route="admin:home")
        return
    name = (row.get("name_ru") or row.get("name_en") or row.get("slug") or "Без названия").strip()
    await show(
        update,
        context,
        f"✏️ <b>Редактирование полей</b>\n{html.escape(name)}\nВыбери поле для изменения.",
        admin_supp_field_picker_kb(int(supplement_id)),
        route=f"admin:supp_edit:{int(supplement_id)}",
        payload={"supplement_id": int(supplement_id)},
    )


async def _show_admin_supp_field_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    supplement_id: int,
    field: str,
) -> None:
    row = await db.get_supplement_admin(conn, int(supplement_id))
    if not row:
        await show(update, context, "Добавка не найдена.", admin_hub_kb(), route="admin:home")
        return
    if field not in db.ADMIN_EDITABLE_SUPPLEMENT_FIELDS:
        await show(update, context, "Поле недоступно.", admin_supp_field_picker_kb(int(supplement_id)), route=f"admin:supp_edit:{int(supplement_id)}")
        return
    current_value = str(row.get(field) or "")
    lines = [
        f"✍️ <b>{_admin_field_label(field)}</b>",
        f"Текущее значение: <code>{html.escape(_admin_preview(current_value))}</code>",
        "Пришли новое значение сообщением.",
    ]
    if field in ADMIN_JSON_FIELDS:
        lines.append("Пример JSON: <code>[\"brain\",\"gut\"]</code>")
    await show(
        update,
        context,
        "\n".join(lines),
        admin_supp_edit_wait_kb(int(supplement_id)),
        route="admin_edit_field",
        payload={"supplement_id": int(supplement_id), "field_name": field},
    )


async def _show_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE, conn) -> None:
    text = await _admin_dashboard_text(conn)
    await show(update, context, text, admin_stats_kb(), route="admin:stats")


async def _show_admin_stacks(update: Update, context: ContextTypes.DEFAULT_TYPE, conn, page: int = 0) -> None:
    page = max(0, int(page))
    page_size = 10
    offset = page * page_size
    rows = await db.admin_list_curated_stacks(conn, limit=page_size + 1, offset=offset)
    has_next = len(rows) > page_size
    stacks = [dict(r) for r in rows[:page_size]]
    await show(
        update,
        context,
        "🧩 <b>Готовые стеки (админ)</b>\n"
        "Создавай, редактируй, публикуй. Пользователи видят только опубликованные.\n"
        "Нажми на стек, чтобы открыть.",
        admin_stacks_list_kb(stacks, page, has_prev=page > 0, has_next=has_next),
        route="admin:stacks",
        payload={"page": page},
    )


def _short_line(value: str, limit: int = 90) -> str:
    clean = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


ADMIN_FIELD_LABELS = {
    "name_ru": "Название RU",
    "name_en": "Название EN",
    "summary_ru_verified": "Summary (verified)",
    "summary_ru_theoretical": "Summary (theoretical)",
    "dose_ru": "Dose",
    "timing_ru": "Timing",
    "safety_ru": "Safety",
    "evidence_grade": "Evidence grade",
    "confidence": "Confidence",
    "tags_json": "Tags",
    "body_targets_json": "Targets",
    "body_burdens_json": "Burdens",
    "purchase_url": "Purchase URL",
}

ADMIN_JSON_FIELDS = {"tags_json", "body_targets_json", "body_burdens_json"}


def _admin_field_label(field: str) -> str:
    return ADMIN_FIELD_LABELS.get(field, field)




def _render_diag_summary(payload: dict) -> str:
    lines = [f"🧰 <b>Диагностика</b> ({'apply' if payload.get('apply') else 'dry-run'})"]
    for issue in payload.get('issues', []):
        lines.append(f"• <code>{issue.get('code')}</code>: найдено {int(issue.get('count') or 0)}, исправлено {int(issue.get('fixed') or 0)}")
    if len(lines) == 1:
        lines.append('Проблем не найдено.')
    return "\n".join(lines)

def _admin_preview(value: str, limit: int = 280) -> str:
    text = str(value or "").strip()
    if not text:
        return "—"
    text = re.sub(r"\s+", " ", text)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


async def _show_admin_stack_editor(update: Update, context: ContextTypes.DEFAULT_TYPE, conn, stack_id: int, *, target_chat_id: int | None = None, target_message_id: int | None = None) -> None:
    stack = await db.admin_get_curated_stack_by_id(conn, stack_id)
    if not stack:
        await show(update, context, "Стек не найден.", admin_stacks_kb(), route="admin:stacks", target_chat_id=target_chat_id, target_message_id=target_message_id)
        return
    tags = []
    try:
        tags = json.loads(stack["tags_json"] or "[]")
    except Exception:
        tags = []
    if not isinstance(tags, list):
        tags = []
    tags_text = ", ".join([str(t).strip() for t in tags if str(t).strip()]) if tags else "—"
    status = "🟢" if int(stack["is_active"] or 0) else "⚪"
    desc = _short_line(str(stack["description_ru"] or "—"), 120)
    def preview(value, budget):
        return _escaped_text_chunks(_short_line(value, budget), budget)[0]
    text = (
        f"🧩 <b>{preview(stack['title_ru'] or 'Без названия', 120)}</b> {status}\n"
        f"slug: <code>{preview(stack['slug'], 160)}</code>\n"
        f"Автор: {preview(stack['author_ru'] or '—', 120)}\n"
        f"Теги: {preview(tags_text, 200)}\n"
        f"Источник: {preview(stack['source_url'] or '—', 240)}\n"
        f"Описание: {preview(desc, 120)}"
    )
    await show(
        update,
        context,
        text,
        admin_stack_editor_kb(int(stack["id"]), int(stack["is_active"] or 0)),
        route=f"admin:stack:open:{int(stack['id'])}",
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )


async def _show_admin_stack_items(update: Update, context: ContextTypes.DEFAULT_TYPE, conn, stack_id: int, page: int = 0, *, target_chat_id: int | None = None, target_message_id: int | None = None) -> None:
    if not await db.admin_get_curated_stack_by_id(conn, int(stack_id)):
        await show(update, context, "Стек не найден.", admin_stacks_kb(), route="admin:stacks", target_chat_id=target_chat_id, target_message_id=target_message_id)
        return
    items = await db.list_curated_stack_items(conn, int(stack_id))
    entries = []
    for idx, item in enumerate(items, start=1):
        raw = dict(item)
        raw['display_index'] = idx
        entries.append({'title': f"{idx}. {item['supplement_name'] or item['supplement_slug'] or 'Без названия'}", 'reason_ru': f"{item['dose_ru'] or '—'} — {item['timing_ru'] or '—'}", 'raw':raw})
    pages = _compatibility_pages(entries)
    page = min(max(0,int(page)),len(pages)-1)
    lines = ['📦 <b>Состав стека</b>']
    lines.extend(entry['text'] for entry in pages[page])
    if not items:
        lines.append('Пока пусто. Добавь элементы.')
    if len(pages)>1:
        lines.append(f"Страница {page+1}/{len(pages)}")
    page_items = {entry['item']['raw']['id']:entry['item']['raw'] for entry in pages[page]}
    await show(update, context, '\n'.join(lines), admin_stack_items_kb(int(stack_id), list(page_items.values()), page=page, has_prev=page>0, has_next=page+1<len(pages)), route=f"admin:stack:items:{int(stack_id)}:{page}", target_chat_id=target_chat_id, target_message_id=target_message_id)


async def _show_admin_stack_item(update: Update, context: ContextTypes.DEFAULT_TYPE, conn, stack_id: int, item_id: int, page: int = 0, *, target_chat_id: int | None = None, target_message_id: int | None = None) -> None:
    item = await db.admin_get_curated_stack_item(conn, int(item_id))
    if not item or int(item['stack_id']) != int(stack_id):
        await _show_admin_stack_items(update, context, conn, stack_id, target_chat_id=target_chat_id, target_message_id=target_message_id)
        return
    name = str(item['supplement_name'] or item['supplement_slug'] or 'Без названия')
    content = (
        f"{name}\n"
        f"Доза: {item['dose_ru'] or '—'}\n"
        f"Время: {item['timing_ru'] or '—'}\n"
        f"Заметка: {item['notes_ru'] or '—'}"
    )
    pages = _escaped_text_chunks(content, 2800)
    page = min(max(0,int(page)),len(pages)-1)
    text = pages[page]
    rows = list(admin_stack_item_editor_kb(stack_id, int(item_id)).inline_keyboard)
    if len(pages)>1:
        text += f"\n\nСтраница {page+1}/{len(pages)}"
        nav = []
        if page>0:
            nav.append(InlineKeyboardButton('⬅️', callback_data=f'admin:stack:item:open:{stack_id}:{item_id}:{page-1}'))
        if page+1<len(pages):
            nav.append(InlineKeyboardButton('➡️', callback_data=f'admin:stack:item:open:{stack_id}:{item_id}:{page+1}'))
        rows.insert(-1, nav)
    await show(
        update,
        context,
        text,
        InlineKeyboardMarkup(rows),
        route=f"admin:stack:item:open:{stack_id}:{int(item_id)}:{page}",
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )


async def _show_catalog_alpha_list(update: Update, context: ContextTypes.DEFAULT_TYPE, conn, page: int = 0) -> None:
    page = max(page, 0)
    limit = 30
    offset = page * limit
    rows = await db.list_supplements_alpha(conn, limit=limit + 1, offset=offset)
    has_next = len(rows) > limit
    display_rows = rows[:limit]

    if not display_rows and page > 0:
        await _show_catalog_alpha_list(update, context, conn, page=page - 1)
        return

    lines = ["📃 <b>Список добавок</b>"]
    if display_rows:
        for idx, row in enumerate(display_rows, start=offset + 1):
            name_ru = str(row["name_ru"] or row["slug"] or "Без названия")
            grade = str(row["evidence_grade"] or "—").strip() or "—"
            lines.append(f"{idx}. {name_ru} — {grade}")
    else:
        lines.append("Список пока пуст.")

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️", callback_data=f"cat:list:{page - 1}"))
    if has_next:
        nav_row.append(InlineKeyboardButton("▶️", callback_data=f"cat:list:{page + 1}"))
    kb_rows: list[list[InlineKeyboardButton]] = []
    if nav_row:
        kb_rows.append(nav_row)
    kb_rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="cat:home")])

    await show(
        update,
        context,
        "\n".join(lines),
        InlineKeyboardMarkup(kb_rows),
        route=f"cat:list:{page}",
        payload={"mode": "alpha_list", "page": page},
    )

async def _load_catalog_payload(conn, tg_uid: int) -> dict:
    view, payload = await db.get_user_last_view(conn, tg_uid)
    if view != "catalog":
        return {
            "mode": "list",
            "list_mode": "alpha",
            "page": 0,
            "query": "",
            "class_key": "",
            "body_token": "",
            "return_page": 0,
        }
    try:
        payload_data = json.loads(payload) if payload else {}
    except json.JSONDecodeError:
        payload_data = {}
    if not isinstance(payload_data, dict):
        payload_data = {}
    payload_data.setdefault("mode", "list")
    payload_data.setdefault("list_mode", "alpha")
    payload_data.setdefault("page", 0)
    payload_data.setdefault("query", "")
    payload_data.setdefault("class_key", "")
    payload_data.setdefault("type_token", "")
    payload_data.setdefault("body_token", "")
    payload_data.setdefault("group_token", "")
    payload_data.setdefault("return_page", int(payload_data.get("page") or 0))
    return payload_data

async def _show_catalog_class_picker(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    page: int,
    *,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    page = max(page, 0)
    classes = rules.CATALOG_CLASSES
    start = page * CATALOG_CLASS_PAGE_SIZE
    end = start + CATALOG_CLASS_PAGE_SIZE
    sliced = classes[start:end]
    await show(
        update,
        context,
        "🎯 <b>По цели</b>\n\nЧто хочешь улучшить?",
        catalog_class_picker(
            sliced,
            page=page,
            has_prev=page > 0,
            has_next=end < len(classes),
        ),
        route="cat:goal",
        payload={"mode": "goal", "page": page, "list_mode": "class", "query": "", "class_key": "", "body_token": "", "return_page": 0},
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )


async def _show_catalog_type_picker(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await show(
        update,
        context,
        "🧪 <b>Каталог — По типу</b>\nВыбери категорию. Дальше покажу список.",
        catalog_type_kb(),
        route="cat:mode:type",
        payload={"mode": "home"},
    )


async def _show_catalog_body_picker(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    mode: str,
) -> None:
    _ = mode
    await show(
        update,
        context,
        "🫀 <b>По органу / системе</b>\n\nВыбери систему, которую хочешь поддержать.",
        catalog_body_kb(),
        route="cat:organ",
        payload={"mode": "organ", "list_mode": "body_benefit", "page": 0, "query": "", "class_key": "", "body_token": "", "return_page": 0},
    )


async def _show_catalog_group_picker(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await show(
        update,
        context,
        "🧩 <b>Каталог — По группе</b>\nВыбери категорию. Дальше покажу список.",
        catalog_group_kb(),
        route="cat:mode:group",
        payload={"mode": "home"},
    )

async def _show_catalog_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    tg_uid: int,
    payload: dict,
    *,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    list_mode = payload.get("list_mode") or "alpha"
    page = max(int(payload.get("page") or 0), 0)
    offset = page * CATALOG_PAGE_SIZE
    query = str(payload.get("query") or "").strip()
    class_key = str(payload.get("class_key") or "").strip()
    type_token = str(payload.get("type_token") or "").strip()
    body_token = str(payload.get("body_token") or "").strip()
    group_token = str(payload.get("group_token") or "").strip()
    return_page = int(payload.get("return_page") or page or 0)

    if list_mode == "search":
        rows, total = await db.search_supplements(conn, query, page=page, page_size=CATALOG_PAGE_SIZE)
    elif list_mode == "class":
        total = await db.count_supplements_by_class(conn, class_key)
        rows = await db.list_supplements_by_class(conn, class_key, limit=CATALOG_PAGE_SIZE, offset=offset)
    elif list_mode == "alpha":
        total = await db.count_supplements(conn)
        rows = await db.list_supplements_sorted(conn, "alpha", limit=CATALOG_PAGE_SIZE, offset=offset)
    elif list_mode == "type":
        rows = await db.list_supplements_by_type(conn, type_token, limit=CATALOG_PAGE_SIZE + 1, offset=offset)
        total = offset + len(rows)
    elif list_mode == "body_benefit":
        rows = await db.list_supplements_by_body_target(conn, body_token, limit=CATALOG_PAGE_SIZE + 1, offset=offset)
        total = offset + len(rows)
    elif list_mode == "body_burden":
        rows = await db.list_supplements_by_body_burden(conn, body_token, limit=CATALOG_PAGE_SIZE + 1, offset=offset)
        total = offset + len(rows)
    elif list_mode == "group":
        rows = await db.list_supplements_by_functional_group(conn, group_token, limit=CATALOG_PAGE_SIZE + 1, offset=offset)
        total = offset + len(rows)
    else:
        total = await db.count_supplements(conn)
        rows = await db.list_supplements_sorted(conn, "alpha", limit=CATALOG_PAGE_SIZE, offset=offset)
        list_mode = "alpha"

    if list_mode in {"type", "body_benefit", "body_burden", "group"}:
        has_next = len(rows) > CATALOG_PAGE_SIZE
        rows = rows[:CATALOG_PAGE_SIZE]
    else:
        has_next = (offset + len(rows)) < total

    display_rows = rows
    if rows:
        intake_stats = await db.get_user_last_intake_stats(conn, tg_uid, [int(r["id"]) for r in rows if int(r["id"] or 0) > 0])
        display_rows = []
        for row in rows:
            row_data = dict(row)
            stats = intake_stats.get(int(row["id"]), {})
            row_data["last_taken_day"] = str(stats.get("last_taken_day") or "")
            display_rows.append(row_data)

    if not rows:
        if total == 0 and list_mode in {"alpha"}:
            await show(
                update,
                context,
                "Каталог пуст. Похоже, сиды ещё не импортированы…",
                catalog_home(is_admin=(await _get_role_perms_ctx(context, tg_uid))[0] != db.ROLE_NONE),
                route="cat:home",
                payload={"mode": "home"},
                target_chat_id=target_chat_id,
                target_message_id=target_message_id,
            )
            return
        await show(
            update,
            context,
            "Ничего не найдено.",
            catalog_home(is_admin=(await _get_role_perms_ctx(context, tg_uid))[0] != db.ROLE_NONE),
            route="cat:home",
            payload={"mode": "home"},
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return

    lines = ["📚 <b>Каталог</b>"]
    if list_mode == "search":
        lines.append(f"Поиск: «{_escaped_text_chunks(_admin_preview(query, 300), 500)[0]}»")
    elif list_mode == "class":
        lines.append(f"Цель: {rules.get_catalog_class_title(class_key)}")
    elif list_mode == "type":
        lines.append(f"По типу: {CATALOG_TYPE_TITLES.get(type_token, type_token or 'Не задано')}")
    elif list_mode == "body_benefit":
        lines.append(f"Система: {CATALOG_BODY_TITLES.get(body_token, body_token or 'Не задано')} · ✅ Поддержка")
    elif list_mode == "body_burden":
        lines.append(f"Система: {CATALOG_BODY_TITLES.get(body_token, body_token or 'Не задано')} · ⚠️ Нагрузка")
    elif list_mode == "group":
        lines.append(f"По группе: {CATALOG_GROUP_TITLES.get(group_token, group_token or 'Не задано')}")
    elif list_mode == "alpha":
        lines.append("Режим: весь каталог A–Я")
    shown_total = total
    if list_mode in {"type", "body_benefit", "body_burden", "group"} and has_next:
        shown_total = offset + len(rows) + 1
    lines.append(f"Найдено: {shown_total}")
    lines.append("Показываю самые релевантные. Открой карточку, чтобы увидеть детали.")

    list_back_cb = "cat:home"
    if list_mode == "class":
        list_back_cb = "cat:goal"
    elif list_mode in {"body_benefit", "body_burden"}:
        list_back_cb = "cat:organ"

    await show(
        update,
        context,
        "\n".join(lines),
        catalog_list(
            display_rows,
            has_prev=page > 0,
            has_next=has_next,
            back_cb=list_back_cb,
            mode_switch_body_token=body_token if list_mode in {"body_benefit", "body_burden"} else "",
            mode_switch_current=list_mode,
        ),
        route=f"cat:list:{page}",
        payload={
            "mode": "list",
            "list_mode": list_mode,
            "page": page,
            "query": query,
            "class_key": class_key,
            "type_token": type_token,
            "body_token": body_token,
            "group_token": group_token,
            "return_page": return_page,
        },
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )

async def _show_catalog_card(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    tg_uid: int,
    supplement_id: int,
    *,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    row = await db.get_supplement_by_id(conn, supplement_id)
    if not row:
        await show(update, context, "Не нашёл в каталоге.", back(), route="menu:catalog")
        return
    name_ru = str(row["name_ru"] or "").strip()
    name_en = str(row["name_en"] or "").strip()
    title = name_ru or name_en or row["name"]
    if name_ru and name_en:
        title = f"{name_ru} ({name_en})"
    summary = (
        row["summary_verified"]
        or row["summary_ru"]
        or row["summary_theoretical"]
        or row["molecular_mechanisms"]
        or row["mechanisms"]
        or ""
    )
    summary = summary.strip() or "описание будет добавлено"

    evidence_text = engine.evidence_grade_label(row.get("evidence_grade"))
    confidence_text = engine.confidence_label(row.get("confidence"))
    human_trials = engine.metric_or_no_data(row.get("human_trials_count_est"))
    participants = engine.metric_or_no_data(row.get("participants_total_est"))

    intake_stats = await db.get_user_last_intake_stats(conn, tg_uid, [int(row["id"])])
    card_stats = intake_stats.get(int(row["id"]), {})
    last_taken_day = str(card_stats.get("last_taken_day") or "")
    taken_days = int(card_stats.get("taken_days") or 0)

    text = (
        f"💊 <b>{html.escape(str(title))}</b>\n\n"
        f"Кратко: {html.escape(summary)}\n\n"
        f"Доказательность: {evidence_text}\n"
        f"Уверенность: {confidence_text}\n"
        f"Исследования на людях: {human_trials}\n"
        f"Участников суммарно: {participants}"
    )
    if last_taken_day:
        text += f"\n\nПоследний приём: {_format_day_ru(last_taken_day)}"
        if taken_days > 0:
            text += f"\nПринимался: {taken_days} дн."

    in_my = await db.user_has_supplement(conn, tg_uid, int(row["id"]))
    btns: list[list[InlineKeyboardButton]] = []
    if in_my:
        btns.append([InlineKeyboardButton("⚙️ Управлять", callback_data=f"mysupp:open:{row['id']}")])
    else:
        btns.append([InlineKeyboardButton("🟢 Добавить в мои БАДы", callback_data=f"card:add:{row['id']}")])
    btns.append([InlineKeyboardButton("💊 Как принимать", callback_data=f"supp:open:{row['id']}")])
    btns.append([InlineKeyboardButton("🧩 Совместимость", callback_data=f"card:compat:{row['id']}")])
    btns.append([InlineKeyboardButton("📊 Доказательность", callback_data=f"card:evidence:{row['id']}")])
    if is_http_url(row.get("purchase_url")):
        btns.append([InlineKeyboardButton("🛒 Где купить", url=str(row.get("purchase_url") or "").strip())])
    btns.append([InlineKeyboardButton("⬅️ Назад", callback_data="cat:back")])

    payload = await _load_catalog_payload(conn, tg_uid)
    payload['supplement_id'] = int(row['id'])
    await show(
        update,
        context,
        text,
        InlineKeyboardMarkup(btns),
        route=f"cat:card:{int(row['id'])}",
        payload=payload,
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )

async def _show_catalog_evidence(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    tg_uid: int,
    supplement_id: int,
    *,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    row = await db.get_supplement_by_id(conn, supplement_id)
    if not row:
        await show(update, context, "Не нашёл в каталоге.", back(), route="menu:catalog")
        return
    title = row["name_ru"] or row["name_en"] or row["name"]
    if row["name_ru"] and row["name_en"]:
        title = f"{row['name_ru']} ({row['name_en']})"
    evidence_text = engine.evidence_grade_label(row.get("evidence_grade"))
    confidence_text = engine.confidence_label(row.get("confidence"))
    human_trials = engine.metric_or_no_data(row.get("human_trials_count_est"))
    participants = engine.metric_or_no_data(row.get("participants_total_est"))

    text = (
        f"📊 <b>Доказательность</b>\n"
        f"{html.escape(str(title))}\n\n"
        f"Уровень базы: {evidence_text}\n"
        f"Уверенность: {confidence_text}\n"
        f"Исследования на людях: {human_trials}\n"
        f"Участников суммарно: {participants}"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"cat:open:{row['id']}")]])
    payload = await _load_catalog_payload(conn, tg_uid)
    payload['supplement_id'] = int(row['id'])
    await show(
        update,
        context,
        text,
        kb,
        route=f"cat:evidence:{int(row['id'])}",
        payload=payload,
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )

async def _show_catalog_search_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    *,
    return_page: int,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    context.user_data.pop(WAITING_KEY, None)
    text = (
        "🔎 Поиск БАДов\n\n"
        "Напиши название (RU/EN), например: магний, omega3"
    )
    msg = await show(
        update,
        context,
        text,
        InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Назад", callback_data=f"cat:page:{return_page}")]]
        ),
        route=None,
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )
    await db.save_ui_state(
        conn,
        update.effective_user.id,
        msg.message_id,
        "cat_search_wait",
        json.dumps({"return_page": return_page}, ensure_ascii=False),
    )

async def render_search_results(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    *,
    tg_uid: int,
    query: str,
    page: int,
    return_page: int,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    page_size = 10
    page = max(int(page), 0)
    items, total = await db.search_supplements(conn, query, page=page, page_size=page_size)
    has_prev = page > 0
    has_next = (page + 1) * page_size < total
    text = (
        "🔎 Результаты поиска: "
        f"{_escaped_text_chunks(_admin_preview(query, 300), 500)[0]}\n"
        f"Найдено: {total}\n\n"
        "Выбери БАД:"
    )
    msg = await show(
        update,
        context,
        text,
        catalog_search_results(
            items,
            page=page,
            has_prev=has_prev,
            has_next=has_next,
            return_page=return_page,
        ),
        route=None,
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )
    await db.save_ui_state(
        conn,
        tg_uid,
        msg.message_id,
        "cat_search_results",
        json.dumps(
            {"query": query, "page": page, "return_page": return_page},
            ensure_ascii=False,
        ),
    )

async def render_catalog(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    page: int,
    *,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    page_size = 10
    page = max(int(page), 0)
    items, total = await db.list_supplements(conn, page, page_size=page_size)
    pages = max(1, (total + page_size - 1) // page_size) if total else 0
    if total == 0:
        msg = await show(
            update,
            context,
            "Каталог пуст. Загрузите сиды.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 В меню", callback_data="menu:home")]]),
            route=None,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        await db.save_ui_state(
            conn,
            update.effective_user.id,
            msg.message_id,
            "catalog",
            json.dumps({"page": 0}, ensure_ascii=False),
        )
        return
    if not items and page >= pages and pages > 0:
        page = pages - 1
        items, total = await db.list_supplements(conn, page, page_size=page_size)
    lines = [
        "📚 <b>Каталог БАДов</b>",
        "Выберите добавку:",
        f"Стр. {page + 1}/{pages}",
    ]
    msg = await show(
        update,
        context,
        "\n".join(lines),
        catalog_kb(items, page, pages),
        route=None,
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )
    await db.save_ui_state(
        conn,
        update.effective_user.id,
        msg.message_id,
        "catalog",
        json.dumps({"page": page}, ensure_ascii=False),
    )

async def render_supplement_card(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    supplement_id: int,
    return_page: int,
    *,
    tab: str = "brief",
    page: int = 0,
    return_mode: str = "catalog",
    search_query: str | None = None,
    prev_payload: dict | None = None,
    show_added: bool = False,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    row = await db.get_supplement_by_id(conn, supplement_id)
    if not row:
        back_data = (
            f"cat:sres:{return_page}" if return_mode == "search" else f"cat:page:{return_page}"
        )
        msg = await show(
            update,
            context,
            "Не найдено.",
            InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("⬅️ Назад", callback_data=back_data)],
                    [InlineKeyboardButton("🏠 В меню", callback_data="menu:home")],
                ]
            ),
            route=None,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        await db.save_ui_state(
            conn,
            update.effective_user.id,
            msg.message_id,
            "catalog",
            json.dumps({"page": return_page}, ensure_ascii=False),
        )
        return
    tab = (tab or "brief").strip().lower()
    if tab == "time":
        tab = "evidence"
    if tab not in {"brief", "dose", "synergy", "antag", "evidence"}:
        tab = "brief"

    name_ru = str(row.get("name_ru") or "").strip()
    name_en = str(row.get("name_en") or "").strip()
    title = name_ru or name_en or str(row.get("name") or "").strip() or "Без названия"
    evidence_grade_text = engine.evidence_grade_label(row.get("evidence_grade"))
    confidence_text = engine.confidence_label(row.get("confidence"))
    human_trials = engine.metric_or_no_data(row.get("human_trials_count_est"))
    participants = engine.metric_or_no_data(row.get("participants_total_est"))
    short_desc = str(row.get("summary_ru") or "").strip()
    def esc(value) -> str:
        return html.escape(str(value or "").strip())

    dose_raw = row.get("dose_ru")
    dose_typical = ""
    dose_range = ""
    course_ru = ""
    dose_notes = ""
    timing_hint = ""
    timing_comment = ""
    with_food = ""
    payload_json = row.get("payload_json")
    payload_data = {}
    if payload_json:
        try:
            payload_data = json.loads(payload_json)
        except json.JSONDecodeError:
            payload_data = {}
    if isinstance(dose_raw, str) and dose_raw:
        try:
            parsed = json.loads(dose_raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            dose_typical = str(parsed.get("typical_range") or parsed.get("typical") or "").strip()
            dose_range = str(parsed.get("range") or parsed.get("dose_range") or "").strip()
            course_ru = str(parsed.get("course") or "").strip()
            dose_notes = str(parsed.get("notes") or "").strip()
            timing_hint = str(parsed.get("timing") or "").strip()
            timing_comment = str(
                parsed.get("timing_comment")
                or parsed.get("timing_notes")
                or parsed.get("comment")
                or ""
            ).strip()
            with_food = str(
                parsed.get("with_food")
                or parsed.get("with_food_ru")
                or parsed.get("food")
                or ""
            ).strip()
        else:
            dose_typical = dose_raw.strip()
    elif dose_raw:
        dose_typical = str(dose_raw).strip()

    timing_ru = str(row.get("timing_ru") or "").strip() or timing_hint
    if not with_food:
        with_food = str(payload_data.get("with_food_ru") or payload_data.get("with_food") or "").strip()
    if not timing_comment:
        timing_comment = str(
            payload_data.get("timing_comment_ru")
            or payload_data.get("timing_notes_ru")
            or payload_data.get("timing_notes")
            or payload_data.get("comment_ru")
            or ""
        ).strip()

    safety_ru = str(row.get("safety_ru") or "").strip()
    lines: list[str] = []
    top_synergy = []
    top_antag = []
    if tab == "brief":
        links = await db.get_supplement_links(conn, supplement_id)
        top_limit = 5
        top_synergy = await db.list_linked_supplements(conn, links["synergy"][:top_limit])
        top_antag = await db.list_linked_supplements(conn, links["antagonists"][:top_limit])
        lines = [f"<b>{esc(title)}</b>"]
        if show_added:
            lines.append("✅ Добавлено в «Мои БАДы»")
        lines.extend(["", f"Доказательность: {evidence_grade_text}"])
        lines.extend(["", "Что делает:"])
        summary_verified = str(row.get("summary_verified") or "").strip()
        summary_theoretical = str(row.get("summary_theoretical") or "").strip()
        molecular_mechanisms = str(row.get("molecular_mechanisms") or "").strip()
        effect_text = summary_verified or short_desc or summary_theoretical or molecular_mechanisms
        lines.append(esc(effect_text) or "нет данных")
        lines.extend(["", "На что обратить внимание:"])
        safety_block = safety_ru or timing_comment or "нет данных"
        lines.append(esc(safety_block))
        lines.extend([
            "",
            "Топ совместимости:",
            _format_link_names(top_synergy),
            "",
            "Топ несовместимости:",
            _format_link_names(top_antag),
        ])
    elif tab == "dose":
        lines = [f"<b>{esc(title)}</b> — 💊 Как принимать"]
        if timing_ru:
            lines.append(f"Время приёма: {esc(timing_ru)}")
        if with_food:
            lines.append(f"С едой/натощак: {esc(with_food)}")
        if dose_typical:
            lines.append(f"Рекомендуемая доза: {esc(dose_typical)}")
        if dose_range:
            lines.append(f"Диапазон: {esc(dose_range)}")
        if course_ru:
            lines.append(f"Курс: {esc(course_ru)}")
        if dose_notes:
            lines.append(f"Примечания: {esc(dose_notes)}")
        if safety_ru:
            lines.append(f"Безопасность: {esc(safety_ru)}")
        if len(lines) == 1:
            lines.append("нет данных")
    elif tab == "evidence":
        lines = [f"<b>{esc(title)}</b> — 📊 Доказательность"]
        lines.append(f"Уровень базы: {evidence_grade_text}")
        lines.append(f"Уверенность: {confidence_text}")
        lines.append(f"Исследования на людях: {human_trials}")
        lines.append(f"Участников суммарно: {participants}")
    else:
        links = await db.get_supplement_links(conn, supplement_id)
        link_ids = links["synergy"] if tab == "synergy" else links["antagonists"]
        page_size = 10
        page = max(int(page), 0)
        pages = max(1, (len(link_ids) + page_size - 1) // page_size) if link_ids else 1
        if page >= pages:
            page = max(pages - 1, 0)
        start = page * page_size
        end = start + page_size
        slice_ids = link_ids[start:end]
        linked_items = await db.list_linked_supplements(conn, slice_ids)
        has_links = bool(link_ids)
        list_lines = []
        for item in linked_items:
            label = item.get("name_ru") or item.get("name_en") or "Без названия"
            list_lines.append(f"• {esc(label)}")
        if tab == "synergy":
            lines = [
                f"<b>{esc(title)}</b> — 🤝 Совместимость",
                "С чем чаще всего сочетают:",
            ]
            if list_lines:
                lines.extend(list_lines)
            else:
                lines.append("Пока нет данных. Добавим по мере наполнения базы.")
        else:
            lines = [
                f"<b>{esc(title)}</b> — ⚠️ Антагонисты",
                "С чем лучше НЕ сочетать:",
            ]
            if list_lines:
                lines.extend(list_lines)
            else:
                lines.append("Пока нет данных…")

    if tab not in {"synergy", "antag"}:
        pages = 1
        has_links = False
        linked_items = None

    kb = supp_tabs_kb(
        supplement_id,
        tab,
        return_mode,
        return_page,
        page=page,
        pages=pages,
        has_links=has_links,
        linked_items=linked_items,
        purchase_url=str(row.get("purchase_url") or "").strip() or None,
        in_stack=await db.user_has_supplement(conn, update.effective_user.id, supplement_id),
    )
    msg = await show(
        update,
        context,
        "\n".join(lines),
        kb,
        route=None,
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )
    payload = {
        "id": supplement_id,
        "tab": tab,
        "page": page,
        "return_page": return_page,
        "return_mode": return_mode,
    }
    if return_mode == "search" and search_query:
        payload["query"] = search_query
    if prev_payload:
        payload["prev_payload"] = prev_payload
    await db.save_ui_state(
        conn,
        update.effective_user.id,
        msg.message_id,
        "supp_card",
        json.dumps(payload, ensure_ascii=False),
    )

async def _show_curated_page(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    page: int,
    *,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    tg_uid = update.effective_user.id if update.effective_user else 0
    page = max(page, 0)
    offset = page * CURATED_PAGE_SIZE
    rows = await db.list_curated_stacks(conn, limit=CURATED_PAGE_SIZE + 1, offset=offset)
    has_next = len(rows) > CURATED_PAGE_SIZE
    display_rows = rows[:CURATED_PAGE_SIZE]
    context.user_data["curated_page"] = page
    if not display_rows:
        await show(
            update,
            context,
            "Пока нет готовых стеков.",
            back(),
            route="menu:curated",
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    text = f"🧠 <b>Готовые стеки</b> (стр. {page + 1})"
    await show(
        update,
        context,
        text,
        curated_stacks_list(display_rows, page, has_prev=page > 0, has_next=has_next, is_admin=_is_admin(tg_uid)),
        route=f"curated:page:{page}",
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )

async def _show_curated_stack(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    slug: str,
    *,
    page: int = 0,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    tg_uid = update.effective_user.id if update.effective_user else 0
    stack = await db.get_curated_stack(conn, slug)
    if not stack:
        await show(
            update,
            context,
            "Стек не найден.",
            back(),
            route="menu:curated",
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return

    items = await db.list_curated_stack_items(conn, int(stack["id"]))
    existing_ids = await db.get_user_stack_supplement_ids(conn, tg_uid) if tg_uid else set()
    entries = []
    title_raw = str(stack['title_ru'] or '')
    title_parts = _escaped_text_chunks(title_raw,700)
    intro = []
    if len(title_parts)>1:
        intro.append(title_raw)
    author = str(stack['author_ru'] or '').strip()
    description = str(stack['description_ru'] or '').strip()
    if author:
        intro.append(f"Автор: {author}")
    if description:
        intro.append(description)
    if intro:
        entries.append({'text':'\n\n'.join(intro)})
    for item in items:
        name = str(item['supplement_name'] or item['supplement_slug'] or 'Без названия')
        details = [str(item[key] or '').strip() for key in ('dose_ru','timing_ru','notes_ru') if str(item[key] or '').strip()]
        entries.append({'title':name, 'reason_ru':' — '.join(details), 'raw':dict(item)})
    pages = _compatibility_pages(entries)
    page = min(max(0,int(page)),len(pages)-1)
    lines = [f"🧠 <b>{title_parts[0]}</b>", '']
    item_buttons = {}
    for entry in pages[page]:
        raw = entry['item'].get('raw')
        mark = ''
        if raw:
            supplement = await db.find_supplement_by_slug_or_name(conn,raw['supplement_slug'],raw['supplement_name'])
            if supplement:
                sid = int(supplement['id'])
                if sid in existing_ids:
                    mark = ' ✅ уже в ваших БАДах'
                item_buttons[sid] = {'supplement_id':sid, 'label':f"🔎 {str(raw['supplement_name'] or raw['supplement_slug'] or 'Добавка')[:34]}"}
        lines.append(entry['text'] + mark)
    if item_buttons:
        lines.extend(['', 'Нажми на кнопку «🔎 …», чтобы открыть карточку БАДа.'])
    if len(pages)>1:
        lines.extend(['', f"Страница {page+1}/{len(pages)} · Позиций: {len(items)}"])
    lines.extend(['', '<i>Это не медсовет.</i>'])
    back_page = int(context.user_data.get('curated_page') or 0)
    await show(update, context, '\n'.join(lines), curated_stack_card(slug,stack_id=int(stack['id']),source_url=stack['source_url'],back_page=back_page,item_buttons=list(item_buttons.values()),page=page,has_prev=page>0,has_next=page+1<len(pages)),route=f"curated:detail_id:{int(stack['id'])}:{page}",target_chat_id=target_chat_id,target_message_id=target_message_id)

async def _show_my_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    tg_uid: int,
    page: int,
    *,
    part: int = 0,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    await db.get_or_create_user(conn, tg_uid)
    local_date = await db.ensure_daily_rollover(conn, tg_uid)
    page = max(int(page), 0)
    rows, total = await db.list_user_supplements_page(
        conn,
        tg_uid,
        q=None,
        page=page,
        page_size=MY_PAGE_SIZE,
        local_date=local_date,
    )
    if total and page > (total - 1) // MY_PAGE_SIZE:
        page = (total - 1) // MY_PAGE_SIZE
        rows, total = await db.list_user_supplements_page(
            conn, tg_uid, q=None, page=page, page_size=MY_PAGE_SIZE, local_date=local_date,
        )
    has_next = (page + 1) * MY_PAGE_SIZE < total
    if not rows:
        await show(
            update,
            context,
            "💊 <b>Мои БАДы</b>\nПока список пуст.",
            my_supplements_list([], 0, False, False, show_clear=False),
            route="mysupp:list:0",
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    entries = []
    for idx, row in enumerate(rows, start=1):
        dose = str(row["dose_text"] or "").strip() or "—"
        entries.append({"row": row, "text": f"{idx}) {row['name']} — доза: {dose}"})
    pages = _compatibility_pages(entries, max_entries=MY_PAGE_SIZE)
    # -1 means the last part when moving back from the next database page.
    part = len(pages) - 1 if int(part) < 0 else min(int(part), len(pages) - 1)
    suffix = f" (стр. {page + 1})" if total > MY_PAGE_SIZE else ""
    if len(pages) > 1:
        suffix += f" · Часть {part + 1}/{len(pages)}"
    lines = [
        f"💊 <b>Мои БАДы</b>{suffix}",
        "",
        "<b>Текущий стек</b>",
    ]
    for idx, entry in enumerate(pages[part]):
        if idx and entry['item'] is pages[part][idx - 1]['item']:
            lines[-1] += entry['text']
        else:
            lines.append(entry['text'])
    page_rows = {int(entry['item']['row']['supplement_id']): entry['item']['row'] for entry in pages[part]}

    await show(
        update,
        context,
        "\n".join(lines),
        my_supplements_overview_kb(list(page_rows.values()), page, has_prev=page > 0, has_next=has_next, part=part, part_count=len(pages)),
        route=f"mysupp:list:{page}" + (f":{part}" if part else ""),
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )

async def show_my_supp_hub(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    supplement_id: int,
    *,
    conn=None,
    part: int = 0,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    close_conn = False
    if conn is None:
        s = context.application.bot_data["settings"]
        conn = await db.connect(s.DB_PATH)
        close_conn = True
    try:
        await db.get_or_create_user(conn, user_id)
        row = await db.get_user_supplement(conn, user_id, supplement_id)
        if not row:
            await show(
                update,
                context,
                "БАД не найден в твоём списке.",
                my_supplements_list([], 0, False, False, show_clear=False),
                route="mysupp:list:0",
                target_chat_id=target_chat_id,
                target_message_id=target_message_id,
            )
            return
        await db.ensure_daily_rollover(conn, user_id)
        today_status = await db.get_today_status(conn, user_id, supplement_id)

        name_ru = (row["name_ru"] or row["name"] or "").strip()
        name_en = (row["name_en"] or "").strip()
        name_line = f"💊 <b>{_escaped_text_chunks(name_ru, 500)[0]}</b>"
        if name_en and name_en != name_ru:
            name_line = f"{name_line} ({_escaped_text_chunks(name_en, 500)[0]})"
        dose_text = (row["dose_text"] or "").strip() or "—"
        dose_parts = _escaped_text_chunks(dose_text, 2600)
        part = min(max(int(part), 0), len(dose_parts) - 1)
        start_date, duration = _format_start_date_and_duration(row["created_at"])
        taken_count = int(row["taken_count"] or 0)
        status_text = "не отмечено"
        if today_status == "taken":
            status_text = "выпил"
        elif today_status == "skipped":
            status_text = "не выпил"

        text = (
            "⚙️ <b>Управление добавкой</b>\n\n"
            f"{name_line}\n\n"
            f"Доза: {dose_parts[part]}\n"
            f"Дата начала: {start_date}\n"
            f"Уже принимается: {duration}\n"
            f"Всего принял: {taken_count} раз\n"
            f"Статус на сегодня: {status_text}\n"
            "Напоминание: автоматически по времени приёма\n"
        )
        if len(dose_parts) > 1:
            text += f"\nДоза · Часть {part + 1}/{len(dose_parts)}"
        await show(
            update,
            context,
            text,
            my_supplement_hub(supplement_id, part=part, part_count=len(dose_parts)),
            route=f"mysupp:hub:{supplement_id}" + (f":{part}" if part else ""),
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
    finally:
        if close_conn:
            await conn.close()


async def _show_add_to_stack_slot_picker(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    tg_uid: int,
    supplement_id: int,
) -> None:
    row = await db.get_supplement_by_id(conn, int(supplement_id))
    if not row:
        await show(update, context, "Не нашёл БАД в каталоге.", back(), route="menu:catalog")
        return
    user_id = await db.get_or_create_user(conn, tg_uid, tz_name="")
    slots = await db.get_user_slots(conn, user_id)
    enabled_slots = [slot for slot in slots if bool(slot.get("is_enabled", True))]
    title = html.escape(str(row.get("name_ru") or row.get("name_en") or row.get("name") or "Добавка"))
    pending = context.user_data.get("add_to_stack_pending") if isinstance(context.user_data.get("add_to_stack_pending"), dict) else {}
    back_cb = str(pending.get("back_cb") or "cat:back")
    text = (
        "🧭 <b>Выбор слота</b>\n\n"
        f"Добавка: <b>{title}</b>\n"
        "Выберите слот, в который хотите добавить БАД."
    )
    await show(
        update,
        context,
        text,
        add_to_stack_slot_kb(int(supplement_id), enabled_slots or slots, back_cb),
        route=f"mysupp:addslot:{int(supplement_id)}",
    )


async def _complete_add_to_stack_with_slot(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    tg_uid: int,
    supplement_id: int,
    slot_id: str,
) -> None:
    pending = context.user_data.get("add_to_stack_pending") if isinstance(context.user_data.get("add_to_stack_pending"), dict) else {}
    try:
        async with db.transaction(conn) as tx:
            user_id = await db.get_or_create_user(tx, tg_uid, tz_name="")
            slots = await db.get_user_slots(tx, user_id)
            if not any(str(slot.get('slot_id') or '') == str(slot_id) for slot in slots):
                raise ValueError('slot_not_found')
            existing = await db.fetchone(tx, "SELECT 1 FROM user_supplements WHERE user_id=? AND supplement_id=? AND is_active=1", (user_id, int(supplement_id)))
            if not existing:
                await db.add_user_supplement(tx, tg_uid, int(supplement_id))
                await db.set_schedule_slot(tx, user_id, int(supplement_id), str(slot_id))
                await db.set_schedule_time_override(tx, user_id, int(supplement_id), None)
    except ValueError:
        await _show_add_to_stack_slot_picker(update, context, conn, tg_uid, supplement_id)
        return
    if pending and context.user_data.get("add_to_stack_pending") is not pending:
        return
    mode = str(pending.get("mode") or "")
    context.user_data.pop("add_to_stack_pending", None)
    context.user_data.pop(WAITING_KEY, None)

    if mode == "card":
        await _show_catalog_card(update, context, conn, tg_uid, int(supplement_id))
        return
    if mode == "mysupp_hub":
        await show_my_supp_hub(update, context, tg_uid, int(supplement_id), conn=conn)
        return
    if mode == "supp_card":
        return_tag = str(pending.get("return_tag") or "retcat")
        return_page = int(pending.get("return_page") or 0)
        tab = str(pending.get("tab") or "brief")
        page = int(pending.get("page") or 0)
        query = pending.get("query")
        prev_payload = pending.get("prev_payload")
        if return_tag == "retsearch":
            await render_supplement_card(
                update, context, conn, int(supplement_id), return_page,
                tab=tab, page=page, return_mode="search", search_query=(str(query or "").strip() or None), show_added=True, prev_payload=prev_payload,
            )
            return
        if return_tag == "retai":
            await render_supplement_card(update, context, conn, int(supplement_id), 0, tab=tab, page=page, return_mode="onboard_back", show_added=True)
            return
        if return_tag == "ret_onboard":
            await db.add_user_initial_stack_id(conn, tg_uid, int(supplement_id))
            await render_supplement_card(update, context, conn, int(supplement_id), 0, tab=tab, page=page, return_mode="onboard_back", show_added=True)
            return
        if return_tag == "retback":
            await render_supplement_card(
                update, context, conn, int(supplement_id), return_page,
                tab=tab, page=page, return_mode="supp_back", prev_payload=prev_payload, show_added=True,
            )
            return
        await render_supplement_card(
            update, context, conn, int(supplement_id), return_page,
            tab=tab, page=page, show_added=True, return_mode=str(pending.get("return_mode") or "catalog"),
            search_query=query, prev_payload=prev_payload,
        )
        return
    await show_my_supp_hub(update, context, tg_uid, int(supplement_id), conn=conn)

async def _show_my_supp_reminders(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    tg_uid: int,
    supplement_id: int,
    *,
    page: int = 0,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    if not await db.user_has_supplement(conn, tg_uid, supplement_id):
        await show(update, context, "БАД не найден в твоём списке.", my_list_back(), route="mysupp:list:0")
        return
    supplement = await db.get_supplement_by_id(conn, supplement_id)
    if not supplement:
        await show(update, context, "БАД не найден.", my_list_back(), route="mysupp:list:0")
        return
    reminders = await db.list_reminders_for_user(conn, tg_uid, supplement_id)
    reminder_times = [str(r["time_local"]) for r in reminders]
    per_page = 8
    pages = max(1, (len(reminder_times) + per_page - 1) // per_page)
    page = min(max(page, 0), pages - 1)
    visible_times = reminder_times[page * per_page:(page + 1) * per_page]
    name = (supplement["name_ru"] or supplement["name"] or "").strip()
    title = f"⏰ <b>Напоминания</b>\n{_reminder_preview(name, 800)}"
    if reminder_times:
        lines = [
            title,
            "",
            "Добавь новое время или удали существующие напоминания.",
            f"Всего: {len(reminder_times)} · Страница {page + 1}/{pages}",
            "",
            "Текущие напоминания:",
            *[f"• {html.escape(t)}" for t in visible_times],
        ]
    else:
        lines = [
            title,
            "",
            "Добавь удобное тебе время напоминания.",
            "",
            "Текущие напоминания: нет",
        ]
    await show(
        update,
        context,
        "\n".join(lines),
        my_supplement_reminders(
            supplement_id,
            reminder_times,
            [],
            page,
            per_page=per_page,
        ),
        route=f"mysupp:rem:page:{supplement_id}:{page}",
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )


def _render_month_history_calendar(month_iso: str, days: list[dict]) -> str:
    taken = {str(d.get("day") or "") for d in days if str(d.get("status") or "") == "taken"}
    skipped = {str(d.get("day") or "") for d in days if str(d.get("status") or "") == "skipped"}
    try:
        year, month = [int(x) for x in str(month_iso).split("-")[:2]]
    except Exception:
        return "Календарь недоступен."
    weeks = calendar.monthcalendar(year, month)
    lines = ["Пн Вт Ср Чт Пт Сб Вс"]
    for week in weeks:
        cells = []
        for day_num in week:
            if day_num <= 0:
                cells.append("  ")
                continue
            day_iso = f"{year:04d}-{month:02d}-{day_num:02d}"
            mark = "🟢" if day_iso in taken else ("🔴" if day_iso in skipped else "⚪")
            cells.append(f"{day_num:02d}{mark}")
        lines.append(" ".join(cells))
    return "\n".join(lines)


async def _show_my_supp_history(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    tg_uid: int,
    supplement_id: int,
    month_offset: int = 0,
) -> None:
    payload = await db.get_user_supplement_intake_history(conn, tg_uid, supplement_id, limit=40, month_offset=month_offset)
    calendar_payload = payload.get("calendar") or {}
    cal_text = _render_month_history_calendar(str(calendar_payload.get("month") or ""), list(calendar_payload.get("days") or []))
    summary = payload.get("summary") or {}
    text = (
        "📊 <b>История приёма</b>\n"
        f"Месяц: {calendar_payload.get('month') or '—'}\n\n"
        f"{cal_text}\n\n"
        f"✅ Дней с приёмом: {int(summary.get('taken_days') or 0)}\n"
        f"Последний приём: {summary.get('last_taken_day') or '—'}"
    )
    await show(
        update,
        context,
        text,
        my_supplement_history_kb(supplement_id, int(month_offset)),
        route=f"mysupp:hist:{supplement_id}:{int(month_offset)}",
    )

async def _show_supplement_page(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    tg_uid: int,
    supplement_id: int,
    *,
    back_route: str | None = None,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    row = await db.get_supplement_by_id(conn, supplement_id)
    if not row:
        await show(update, context, "Не нашёл в каталоге.", back(), route="menu:catalog")
        return
    text = engine.format_supplement_details(row)
    in_stack = await db.user_has_supplement(conn, tg_uid, int(row["id"]))
    fallback_route = "menu:catalog"
    if back_route and isinstance(back_route, str):
        fallback_route = back_route
    await show(
        update,
        context,
        text,
        supplement_page(int(row["id"]), in_stack, row["purchase_url"], fallback_route),
        route=f"supp:open:{row['id']}",
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )

def _escaped_text_chunks(text: str, limit: int) -> list[str]:
    # Count conservatively after escaping, including UTF-16 surrogate pairs.
    chunks, part, size = [], [], 0
    for char in text:
        escaped = html.escape(char)
        width = len(escaped.encode("utf-16-le")) // 2
        if part and size + width > limit:
            chunks.append("".join(part))
            part, size = [], 0
        part.append(escaped)
        size += width
    if part:
        chunks.append("".join(part))
    return chunks or [""]


def _compatibility_pages(items: list[dict], *, max_entries: int = 8) -> list[list[dict]]:
    pages, current, size = [], [], 0
    for item in items:
        reason = str(item.get("reason_ru") or "").strip()
        text = str(item['text']) if 'text' in item else f"• {item.get('title') or 'Неизвестно'}" + (f" — {reason}" if reason else "")
        for chunk in _escaped_text_chunks(text, 2600):
            width = len(chunk.encode("utf-16-le")) // 2
            if current and (size + width + 1 > 2800 or len(current) >= max_entries):
                pages.append(current)
                current, size = [], 0
            current.append({"item": item, "text": chunk})
            size += width + 1
    if current:
        pages.append(current)
    return pages or [[]]


async def _show_supplement_compat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    supplement_id: int,
    kind: str,
    page: int,
    *,
    back_data: str | None = None,
    route_prefix: str | None = None,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    kind = kind if kind in {"synergy", "antagonism", "caution"} else "synergy"
    row = await db.get_supplement_by_id(conn, supplement_id)
    if not row:
        await home(update, context, target_chat_id=target_chat_id, target_message_id=target_message_id)
        return
    compat = await engine.build_compatibility(conn, supplement_id)
    items = compat.get(kind) or []
    pages = _compatibility_pages(items)
    page = min(max(page, 0), len(pages) - 1)
    page_entries = pages[page]
    page_items = []
    seen_ids = set()
    for entry in page_entries:
        item = entry['item']
        sid = item.get('other_id')
        if sid is not None and sid not in seen_ids:
            page_items.append(item)
            seen_ids.add(sid)
    label = {"synergy": "✅ Совместимо", "antagonism": "⛔ Несовместимо", "caution": "⚠️ Осторожно"}[kind]
    title = _escaped_text_chunks(engine._supp_title(row), 500)[0]
    lines = [f"<b>{label} с {title}</b>", ""]
    if not items:
        lines.append("Нет надёжных данных о взаимодействиях с другими БАДами из базы.")
    else:
        lines.extend(entry['text'] for entry in page_entries)
    source_map = {"db": "DB", "rules": "Правила", "mixed": "Смешано"}
    lines.extend(["", f"Источник: {source_map.get(compat.get('source'), 'DB')}", f"Страница {page + 1}/{len(pages)} · Связей: {len(items)}"])
    await show(
        update, context, "\n".join(lines),
        supplement_compat_list(
            supplement_id, page_items, kind=kind, page=page,
            has_prev=page > 0, has_next=page + 1 < len(pages),
            back_data=back_data, nav_prefix=route_prefix,
        ),
        route=f"{route_prefix}:{kind}:{page}" if route_prefix else f"supp:compat:{supplement_id}:{kind}:{page}",
        target_chat_id=target_chat_id, target_message_id=target_message_id,
    )

def _ensure_event_loop() -> None:
    # asyncio.run resets event loop; PTB run_polling needs a current loop on Python 3.13
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)


def main() -> None:
    app = build_app()
    _ensure_event_loop()
    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
