from __future__ import annotations
import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
)

from settings import get_settings
import db
import engine
import seed_import
import rules
from ux import show
from keyboards import (
    main_menu,
    back,
    my_list_back,
    my_hub_back,
    pick_search_results,
    my_supplements_list,
    catalog_kb,
    catalog_home,
    catalog_list,
    catalog_search_results,
    catalog_class_picker,
    curated_stacks_list,
    curated_stack_card,
    supplement_page,
    supplement_compat_list,
    my_supplement_hub,
    my_supplement_reminders,
    timezone_picker,
    reminder_actions,
    settings_menu,
)
import tz
from openai_client import (
    OpenAIConfig,
    OpenAITranscribeConfig,
    suggest_stack_v1,
    transcribe_voice_v1,
    checkin_plan_v1,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("supply-bot")

WAITING_KEY = "waiting"
CATALOG_PAGE_SIZE = 8
CATALOG_CLASS_PAGE_SIZE = 8
MY_PAGE_SIZE = 10
CURATED_PAGE_SIZE = 10

def _is_tz_unset(tz_name: str | None) -> bool:
    return not tz_name or str(tz_name).strip().upper() == "UTC"

async def _download_voice_bytes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[bytes, str]:
    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)
    data = await file.download_as_bytearray()
    filename = f"voice_{update.message.message_id}.ogg"
    return bytes(data), filename

def _format_day_ru(day: str) -> str:
    try:
        return datetime.strptime(day, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return day

def _checkin_intro_text() -> str:
    return (
        "📝 <b>Опрос дня</b>\n\n"
        "Можно:\n"
        "• ответить текстом (1–5 предложений)\n"
        "• или отправить голосовое\n\n"
        "Напиши: сон, стресс, тренировка, самочувствие, что беспокоит.\n\n"
        "<i>Не медицинский совет.</i>"
    )

def _format_checkin_change(idx: int, change: dict) -> str:
    change_type = str(change.get("type") or "").strip()
    name = str(change.get("supplement_name") or "").strip()
    reason = str(change.get("reason_ru") or "").strip()
    dose = str(change.get("dose_ru") or "").strip()
    timing = str(change.get("timing_ru") or "").strip()
    confidence = change.get("confidence")
    type_map = {
        "add": "➕ Добавить",
        "remove": "➖ Убрать",
        "adjust_dose": "⚖️ Изменить дозу",
        "adjust_timing": "⏰ Сдвинуть время",
    }
    label = type_map.get(change_type, change_type or "Изменение")
    line = f"{idx}) {label}: <b>{name}</b>"
    details = []
    if dose:
        details.append(f"доза: {dose}")
    if timing:
        details.append(f"время: {timing}")
    if details:
        line = f"{line} — " + " · ".join(details)
    if reason:
        line = f"{line}\n{reason}"
    try:
        confidence_int = int(confidence)
    except (TypeError, ValueError):
        confidence_int = None
    if confidence_int is not None:
        line = f"{line}\nУверенность: {confidence_int}%"
    return line

def _tzinfo_from_name(tz_name: str | None):
    return db.parse_utc_tz(tz_name) if tz_name else None

async def _render_ai_suggestions(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    goal_text: str,
    raw: str | None,
    *,
    prefix_line: str | None = None,
) -> bool:
    data = None
    format_error_shown = False
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            await show(
                update,
                context,
                "AI вернул неожиданный формат. Попробуй ещё раз.",
                back(),
                route="menu:ai",
            )
            format_error_shown = True
    else:
        await show(update, context, "AI не вернул ответ. Попробуй ещё раз.", back(), route="menu:ai")

    if isinstance(data, dict):
        candidates = data.get("candidates") or []
        warnings = data.get("warnings_ru") or []
        questions = data.get("questions_ru") or []
        if not isinstance(warnings, list):
            warnings = []
        if not isinstance(questions, list):
            questions = []
        found = []
        missing = []
        for cand in candidates:
            if not isinstance(cand, dict):
                continue
            name = str(cand.get("name") or "").strip()
            if not name:
                continue
            rows, _ = await db.search_supplements(conn, name, page=0, page_size=1)
            if rows:
                found.append({"row": rows[0], "cand": cand})
            else:
                missing.append(name)

        lines = [
            "🤖 <b>Подбор стека (AI)</b>",
            "",
        ]
        if prefix_line:
            lines.extend([prefix_line, ""])
        lines.extend([
            f"Цель: <b>{goal_text}</b>",
            "",
        ])
        if found:
            lines.append("Найдено в каталоге:")
            for idx, item in enumerate(found, start=1):
                row = item["row"]
                cand = item["cand"]
                dose = str(cand.get("dose_ru") or "").strip()
                timing = str(cand.get("timing_ru") or "").strip()
                why = str(cand.get("why_ru") or "").strip()
                confidence = cand.get("confidence")
                try:
                    confidence = int(confidence)
                except (TypeError, ValueError):
                    confidence = None
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
                if confidence is not None:
                    line = f"{line}\nУверенность: {confidence}%"
                lines.append(line)
        else:
            lines.append("Найдено в каталоге: ничего.")

        if missing:
            lines.append("")
            lines.append("Не нашёл в каталоге:")
            lines.append(", ".join(missing))

        if warnings:
            lines.append("")
            lines.append("Предупреждения:")
            for w in warnings:
                lines.append(f"• {w}")

        if questions:
            lines.append("")
            lines.append("Вопросы:")
            for q in questions:
                lines.append(f"• {q}")

        lines.append("")
        lines.append("Выбери, что добавить в «Мои БАДы».")

        buttons = []
        for item in found:
            row = item["row"]
            buttons.append(
                [InlineKeyboardButton(f"➕ Добавить: {row['name']}", callback_data=f"aiadd:{row['id']}")]
            )
        buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")])
        await show(
            update,
            context,
            "\n".join(lines),
            InlineKeyboardMarkup(buttons),
            route="menu:ai",
        )
        return True
    if raw and not format_error_shown:
        await show(
            update,
            context,
            "AI вернул неожиданный формат. Попробуй ещё раз.",
            back(),
            route="menu:ai",
        )
    return False

async def _build_checkin_context(conn, tg_uid: int, user_id: int, day: str, tz_name: str | None) -> str:
    local_date = day
    stack_rows = await db.list_user_stack_details(conn, tg_uid, local_date=local_date)
    base_stack = []
    today_marks = []
    for row in stack_rows:
        base_stack.append(
            {
                "name": row["name"],
                "dose_ru": row["dose_text"] or "",
                "timing_ru": row["time_hhmm"] or "",
                "is_active": bool(row["is_active"]),
            }
        )
        if row["today_status"]:
            today_marks.append(
                {
                    "name": row["name"],
                    "status": row["today_status"],
                }
            )

    recent_rows = await db.list_recent_checkins(conn, user_id, limit=3)
    recent_checkins = []
    for row in recent_rows:
        raw = row["payload_json"]
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        text = str(payload.get("text") or "").strip()
        if text:
            recent_checkins.append({"text": text, "day": row["day"], "created_at": row["created_at"]})

    context_payload = {
        "local_day": day,
        "user_tz": tz_name or "UTC",
        "base_stack": base_stack,
        "today_marks": today_marks,
        "recent_checkins": recent_checkins,
    }
    return json.dumps(context_payload, ensure_ascii=False)

async def _render_checkin_plan(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    day: str,
    ai_data: dict,
) -> None:
    summary = str(ai_data.get("summary_ru") or "").strip() or "Краткое резюме недоступно."
    changes = ai_data.get("changes") or []
    warnings = ai_data.get("warnings_ru") or []
    questions = ai_data.get("questions_ru") or []
    if not isinstance(changes, list):
        changes = []
    if not isinstance(warnings, list):
        warnings = []
    if not isinstance(questions, list):
        questions = []

    found_map: dict[int, int] = {}
    missing = []
    for idx, change in enumerate(changes, start=1):
        if not isinstance(change, dict):
            continue
        name = str(change.get("supplement_name") or "").strip()
        if not name:
            continue
        rows, _ = await db.search_supplements(conn, name, page=0, page_size=1)
        if rows:
            found_map[idx] = int(rows[0]["id"])
        else:
            missing.append(name)

    lines = [
        f"📅 <b>План на сегодня</b> ({_format_day_ru(day)})",
        "",
        summary,
        "",
        "Изменения:",
    ]
    if changes:
        for idx, change in enumerate(changes, start=1):
            if not isinstance(change, dict):
                continue
            lines.append(_format_checkin_change(idx, change))
    else:
        lines.append("Нет предложенных изменений.")

    if missing:
        lines.append("")
        lines.append("Не нашёл в каталоге:")
        lines.append(", ".join(missing))

    if warnings:
        lines.append("")
        lines.append("⚠️ Предупреждения:")
        for w in warnings:
            lines.append(f"• {w}")

    if questions:
        lines.append("")
        lines.append("❓ Вопросы:")
        for q in questions:
            lines.append(f"• {q}")

    buttons = []
    for idx in sorted(found_map.keys()):
        buttons.append(
            [
                InlineKeyboardButton(f"✅ Применить #{idx}", callback_data=f"chkapply:{day}:{idx}"),
                InlineKeyboardButton(f"❌ Отклонить #{idx}", callback_data=f"chkdeny:{day}:{idx}"),
            ]
        )
    buttons.append([InlineKeyboardButton("🧾 Посмотреть план на сегодня", callback_data="menu:todayplan")])
    buttons.append([InlineKeyboardButton("⬅️ В меню", callback_data="menu:home")])

    context.user_data["last_checkin_ai"] = {
        "day": day,
        "data": ai_data,
        "found_map": found_map,
    }

    await show(
        update,
        context,
        "\n".join(lines),
        InlineKeyboardMarkup(buttons),
        route="menu:checkin",
    )

async def _handle_checkin_submission(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    tg_uid: int,
    user_id: int,
    tz_name: str | None,
    checkin_text: str,
    source: str,
) -> bool:
    day = await db.ensure_daily_rollover(conn, tg_uid)
    payload = {
        "text": checkin_text,
        "source": source,
    }
    await db.save_daily_checkin(conn, user_id, day, payload)

    s = context.application.bot_data["settings"]
    if not s.OPENAI_API_KEY:
        await show(
            update,
            context,
            "AI отключен: добавь OPENAI_API_KEY в .env.",
            back(),
            route="menu:checkin",
        )
        return False

    context_json = await _build_checkin_context(conn, tg_uid, user_id, day, tz_name)
    cfg = OpenAIConfig(api_key=s.OPENAI_API_KEY, model=s.OPENAI_MODEL)
    await show(update, context, "🤖 Думаю… (AI)", back(), route="menu:checkin")
    raw = None
    try:
        raw = await checkin_plan_v1(cfg, checkin_text, context_json)
    except Exception:
        log.exception("OpenAI checkin plan request failed")
        await show(update, context, "AI временно недоступен.", back(), route="menu:checkin")
        return False

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        await show(update, context, "AI вернул неожиданный формат. Попробуй ещё раз.", back(), route="menu:checkin")
        return False

    if not isinstance(data, dict):
        await show(update, context, "AI вернул неожиданный формат. Попробуй ещё раз.", back(), route="menu:checkin")
        return False

    await _render_checkin_plan(update, context, conn, day, data)
    context.user_data.pop(WAITING_KEY, None)
    return True

async def _show_today_plan(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    tg_uid: int,
) -> None:
    user_id = await db.get_or_create_user(conn, tg_uid)
    day = await db.ensure_daily_rollover(conn, tg_uid)
    stack_rows = await db.list_user_stack_details(conn, tg_uid, local_date=day)
    override = await db.get_daily_plan_override(conn, user_id, day)
    applied = override.get("applied_changes", []) if isinstance(override, dict) else []
    denied = override.get("denied_changes", []) if isinstance(override, dict) else []

    lines = [f"📅 <b>План на сегодня</b> ({_format_day_ru(day)})", ""]
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

    if applied:
        lines.append("")
        lines.append("Применённые изменения:")
        for idx, change in enumerate(applied, start=1):
            if not isinstance(change, dict):
                continue
            label_idx = int(change.get("idx") or idx)
            lines.append(_format_checkin_change(label_idx, change))

    if denied:
        lines.append("")
        lines.append("Отклонённые изменения:")
        for idx, change in enumerate(denied, start=1):
            if not isinstance(change, dict):
                continue
            label_idx = int(change.get("idx") or idx)
            lines.append(_format_checkin_change(label_idx, change))

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
    await show(
        update, context,
        _home_text(),
        main_menu(),
        route="menu:home",
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )

def _home_text() -> str:
    return (
        "💊 <b>Supply Bot</b>\n\n"
        "Я справочник + дневник добавок.\n"
        "— Совместимость из базы\n"
        "— AI-подбор (опционально)\n\n"
        "<i>Не медицинский совет.</i>"
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = context.application.bot_data["settings"]
    conn = await db.connect(s.DB_PATH)
    try:
        await db.get_or_create_user(conn, update.effective_user.id, tz_name="")
        tz = await db.get_user_tz(conn, update.effective_user.id)
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
    finally:
        await conn.close()
    await home(update, context)

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

async def reminders_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    s = context.application.bot_data["settings"]
    conn = await db.connect(s.DB_PATH)
    try:
        due = await db.list_due_reminders(conn)
        for item in due:
            tg_user_id = int(item["user_id"])
            supplement_id = int(item["supplement_id"])
            time_local = str(item["time_local"])
            day = str(item["day"])
            today_status = await db.get_today_status(conn, tg_user_id, supplement_id)
            if today_status in {"taken", "skipped"}:
                continue
            inserted = await db.insert_reminder_fire(conn, tg_user_id, supplement_id, day, time_local)
            if not inserted:
                continue
            await reminder_fire(context, tg_user_id, supplement_id)
    finally:
        await conn.close()

async def daily_rollover_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    s = context.application.bot_data["settings"]
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
    finally:
        await conn.close()

async def reminder_fire_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data or {}
    tg_user_id = int(data.get("tg_user_id"))
    supplement_id = int(data.get("supplement_id"))
    await reminder_fire(context, tg_user_id, supplement_id, is_snooze=True)

async def reminder_fire(
    context: ContextTypes.DEFAULT_TYPE,
    tg_user_id: int,
    supplement_id: int,
    *,
    is_snooze: bool = False,
) -> None:
    s = context.application.bot_data["settings"]
    conn = await db.connect(s.DB_PATH)
    try:
        user_row = await db.fetchone(
            conn,
            """SELECT tg_user_id, tz_name, last_chat_id
               FROM users WHERE tg_user_id=?""",
            (tg_user_id,),
        )
        if not user_row:
            return
        tz_name = str(user_row["tz_name"] or "").strip()
        if _is_tz_unset(tz_name):
            return
        supplement = await db.get_supplement_by_id(conn, supplement_id)
        if not supplement:
            return
        ui_message_id, ui_screen, ui_payload = await db.get_ui_state(conn, tg_user_id)
        if ui_message_id:
            last_chat_id = int(user_row["last_chat_id"] or 0) or tg_user_id
            try:
                await context.bot.delete_message(chat_id=last_chat_id, message_id=ui_message_id)
            except Exception:
                pass

        name_ru = (supplement["name_ru"] or supplement["name"] or "").strip()
        name_en = (supplement["name_en"] or "").strip()
        name_line = name_ru
        if name_en and name_en != name_ru:
            name_line = f"{name_line} ({name_en})"
        dose_text = "—"
        user_supp = await db.get_user_supplement(conn, tg_user_id, supplement_id)
        if user_supp and str(user_supp["dose_text"] or "").strip():
            dose_text = str(user_supp["dose_text"]).strip()

        text = (
            "⏰ <b>Напоминание</b>\n"
            f"Пора принять: <b>{name_line}</b>\n"
            f"Доза: {dose_text}"
        )
        target_chat_id = int(user_row["last_chat_id"] or 0) or tg_user_id
        msg = await context.bot.send_message(
            chat_id=target_chat_id,
            text=text,
            reply_markup=reminder_actions(supplement_id),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        try:
            payload_data = json.loads(ui_payload) if ui_payload else {}
        except json.JSONDecodeError:
            payload_data = {}
        if not isinstance(payload_data, dict):
            payload_data = {}
        payload_data["reminder_msg_id"] = msg.message_id
        payload_json = json.dumps(payload_data, ensure_ascii=False)
        if ui_screen:
            await db.update_ui_state_payload(conn, tg_user_id, payload_json)
        else:
            await db.save_ui_state(conn, tg_user_id, ui_message_id, "", payload_json)
    finally:
        await conn.close()

async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer()

    data = q.data or ""
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

        if data == "main":
            await home(update, context)
            return
        if data == "menu:home":
            await home(update, context)
            return
        if data == "menu:settings":
            await show(
                update,
                context,
                "⚙️ <b>Настройки</b>",
                settings_menu(),
                route="menu:settings",
            )
            return
        if data == "settings:tz":
            context.user_data["tz_back_data"] = "menu:settings"
            await _show_timezone_picker(update, context, 0)
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
            await render_catalog(update, context, conn, 0)
            return

        if data == "menu:curated":
            await _show_curated_page(update, context, conn, 0)
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
            groups = await engine.describe_interactions(conn, row, limit_each=8)
            text = engine.format_interactions(row["name"], groups)
            await show(update, context, text, back(), route=f"compat_pick:{slug}")
            return

        if data.startswith("add_pick:"):
            slug = data.split(":", 1)[1]
            row = await db.get_supplement_by_slug(conn, slug)
            if row:
                await db.add_user_supplement(conn, tg_uid, int(row["id"]))
                await show(update, context, f"✅ Добавлено: <b>{row['name']}</b>", back(), route=f"add_pick:{slug}")
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
                    await render_supplement_card(update, context, conn, supplement_id, return_page)
                    return

        if data == "cat:home":
            await _show_catalog_home(update, context, conn)
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
            })
            return

        if data == "cat:mode:class":
            await _show_catalog_class_picker(update, context, conn, 0)
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
                    return_mode="search",
                    search_query=query or None,
                )
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
            items = await db.list_curated_stack_items(conn, int(stack["id"]))
            not_found: list[str] = []
            to_add: list[dict] = []
            for item in items:
                supplement = await db.find_supplement_by_slug_or_name(
                    conn,
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
                to_add.append(
                    {
                        "supplement_id": int(supplement["id"]),
                        "dose_ru": item["dose_ru"],
                        "timing_ru": item["timing_ru"],
                        "notes_ru": item["notes_ru"],
                    }
                )
            summary = await db.add_stack_items_to_user(conn, user_id, to_add)
            lines = [f"✅ Добавлено: {summary['total']}"]
            if not_found:
                lines.append(f"⚠️ Не найдено в каталоге: {len(not_found)}")
                lines.extend([f"• {name}" for name in not_found[:10]])
                if len(not_found) > 10:
                    lines.append("…и ещё")
            else:
                lines.append("⚠️ Не найдено в каталоге: 0")
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

        if data.startswith("supp:open:"):
            supplement_id = int(data.split(":")[-1])
            back_route = context.user_data.get("last_route")
            await _show_supplement_page(update, context, conn, tg_uid, supplement_id, back_route=back_route)
            return

        if data.startswith("card:add:"):
            supplement_id = int(data.split(":")[-1])
            await db.add_user_supplement(conn, tg_uid, supplement_id)
            await _show_catalog_card(update, context, conn, tg_uid, supplement_id)
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
            page = int(data.split(":")[-1])
            await _show_my_list(update, context, conn, tg_uid, page)
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
                    await db.add_user_supplement(conn, tg_uid, supplement_id)
                    await render_supplement_card(
                        update,
                        context,
                        conn,
                        supplement_id,
                        return_page,
                        show_added=True,
                    )
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
                    await db.add_user_supplement(conn, tg_uid, supplement_id)
                    await render_supplement_card(
                        update,
                        context,
                        conn,
                        supplement_id,
                        return_page,
                        return_mode="search",
                        search_query=query or None,
                        show_added=True,
                    )
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
                await db.add_user_supp(conn, tg_uid, int(row["id"]))
                await show_my_supp_hub(update, context, tg_uid, int(row["id"]), conn=conn)
            else:
                await show(update, context, "Не нашёл в каталоге.", my_list_back(), route="mysupp:add")
            return

        if data.startswith("mysupp:add_direct:"):
            supplement_id = int(data.split(":")[-1])
            await db.add_user_supp(conn, tg_uid, supplement_id)
            await show_my_supp_hub(update, context, tg_uid, supplement_id, conn=conn)
            return

        if data.startswith("mysupp:open:") or data.startswith("mysupp:hub:"):
            supplement_id = int(data.split(":")[-1])
            await show_my_supp_hub(update, context, tg_uid, supplement_id, conn=conn)
            return

        if data.startswith("mysupp:set_dose:"):
            supplement_id = int(data.split(":")[-1])
            context.user_data[WAITING_KEY] = f"mysupp:set_dose:{supplement_id}"
            await show(
                update,
                context,
                "Напиши дозу (до 80 символов).",
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

        if data.startswith("mysupp:remove:"):
            supplement_id = int(data.split(":")[-1])
            await db.remove_user_supp(conn, tg_uid, supplement_id)
            await db.delete_reminders_for_supplement(conn, tg_uid, supplement_id)
            await _show_my_list(update, context, conn, tg_uid, 0)
            return

        if data.startswith("mysupp:rem:del:"):
            _, _, _, supplement_id, time_key = data.split(":", 4)
            time_local = time_key.replace("-", ":")
            await db.delete_reminder(conn, tg_uid, int(supplement_id), time_local)
            await _show_my_supp_reminders(update, context, conn, tg_uid, int(supplement_id))
            return

        if data.startswith("mysupp:rem:add:"):
            supplement_id = int(data.split(":")[-1])
            context.user_data[WAITING_KEY] = f"mysupp:rem_add:{supplement_id}"
            await show(
                update,
                context,
                "Напиши время в формате ЧЧ:ММ, например 09:30",
                my_hub_back(supplement_id),
                route=f"mysupp:rem:add:{supplement_id}",
            )
            return

        if data.startswith("mysupp:rem:"):
            supplement_id = int(data.split(":")[-1])
            await _show_my_supp_reminders(update, context, conn, tg_uid, supplement_id)
            return

        if data == "menu:checkin":
            context.user_data[WAITING_KEY] = "checkin"
            await show(
                update,
                context,
                _checkin_intro_text(),
                back(),
                route="menu:checkin",
            )
            return

        if data == "menu:todayplan":
            await _show_today_plan(update, context, conn, tg_uid)
            return

        if data == "menu:ai":
            if not s.OPENAI_API_KEY:
                await show(update, context, "AI отключен: добавь OPENAI_API_KEY в .env.", back(), route="menu:ai")
                return

            context.user_data[WAITING_KEY] = "ai_goal"
            await show(
                update,
                context,
                "🤖 <b>Подбор стека (AI)</b>\n\n"
                "Опиши цель одной фразой.\n"
                "Пример: «Больше энергии утром, но без тревожности».\n\n"
                "Можно написать текстом или отправить голосовое сообщение.\n\n"
                "<i>Не медицинский совет.</i>",
                back(),
                route="menu:ai",
            )
            return

    finally:
        await conn.close()

    await home(update, context)

async def on_ai_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer()
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

async def on_checkin_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer()
    data = q.data or ""
    if not (data.startswith("chkapply:") or data.startswith("chkdeny:")):
        return
    parts = data.split(":", 2)
    if len(parts) != 3:
        return
    action = parts[0]
    day = parts[1]
    try:
        idx = int(parts[2])
    except ValueError:
        return

    last_ai = context.user_data.get("last_checkin_ai") if isinstance(context.user_data, dict) else None
    if not isinstance(last_ai, dict):
        await show(update, context, "Сначала пройди опрос дня.", back(), route="menu:checkin")
        return
    if last_ai.get("day") != day:
        await show(update, context, "Данные опроса устарели. Пройди опрос ещё раз.", back(), route="menu:checkin")
        return
    ai_data = last_ai.get("data") or {}
    changes = ai_data.get("changes") if isinstance(ai_data, dict) else None
    if not isinstance(changes, list) or idx < 1 or idx > len(changes):
        await show(update, context, "Не нашёл выбранное изменение.", back(), route="menu:checkin")
        return

    change = changes[idx - 1]
    if not isinstance(change, dict):
        await show(update, context, "Не нашёл выбранное изменение.", back(), route="menu:checkin")
        return
    found_map = last_ai.get("found_map") if isinstance(last_ai.get("found_map"), dict) else {}
    if action == "chkapply" and idx not in found_map:
        await show(update, context, "Этой добавки нет в каталоге, применить нельзя.", back(), route="menu:checkin")
        return

    s = context.application.bot_data["settings"]
    tg_uid = update.effective_user.id
    conn = await db.connect(s.DB_PATH)
    try:
        user_id = await db.get_or_create_user(conn, tg_uid)
        override = await db.get_daily_plan_override(conn, user_id, day) or {}
        applied = override.get("applied_changes", [])
        denied = override.get("denied_changes", [])
        if not isinstance(applied, list):
            applied = []
        if not isinstance(denied, list):
            denied = []

        change_payload = dict(change)
        change_payload["idx"] = idx
        if idx in found_map:
            change_payload["supplement_id"] = int(found_map[idx])

        def _strip_existing(items: list[dict]) -> list[dict]:
            cleaned = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                if int(item.get("idx") or -1) == idx:
                    continue
                cleaned.append(item)
            return cleaned

        applied = _strip_existing(applied)
        denied = _strip_existing(denied)

        if action == "chkapply":
            applied.append(change_payload)
            message = f"✅ Применено для {day}. Это влияет только на сегодня."
        else:
            denied.append(change_payload)
            message = "Ок, не применяю."

        override = {
            "applied_changes": applied,
            "denied_changes": denied,
            "final_notes_ru": str(override.get("final_notes_ru") or ""),
        }
        await db.upsert_daily_plan_override(conn, user_id, day, override)
    finally:
        await conn.close()

    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🧾 Посмотреть план на сегодня", callback_data="menu:todayplan")],
            [InlineKeyboardButton("⬅️ В меню", callback_data="menu:home")],
        ]
    )
    await show(update, context, message, kb, route="menu:todayplan")

async def on_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer()
    data = q.data or ""
    if data.startswith("tz:page:"):
        _, _, page = data.split(":", 2)
        await _show_timezone_picker(update, context, int(page))
        return
    if data == "tz:back":
        if context.user_data.get("onboarding") == "tz":
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
            await show(
                update,
                context,
                "⚙️ <b>Настройки</b>",
                settings_menu(),
                route="menu:settings",
            )
        else:
            await home(update, context)
        return
    if data.startswith("tz:set:"):
        tz_tag = data.split(":", 2)[2]
        tz_value = tz.tz_name_from_tag(tz_tag)
        s = context.application.bot_data["settings"]
        tg_uid = update.effective_user.id
        conn = await db.connect(s.DB_PATH)
        try:
            await db.get_or_create_user(conn, tg_uid, tz_name="")
            await db.set_user_tz(conn, tg_uid, tz_value)
        finally:
            await conn.close()
        context.user_data.pop("tz_back_data", None)
        context.user_data.pop("onboarding", None)
        tzinfo = db.parse_utc_tz(tz_value)
        local_now = datetime.now(tzinfo).strftime("%H:%M")
        label = tz.format_confirmation_label(tz_value)
        await show(
            update,
            context,
            "✅ Часовой пояс сохранён: "
            f"{label}\n"
            f"Текущее время у тебя: {local_now}",
            main_menu(),
            route="menu:home",
        )

async def on_reminder_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer()
    data = q.data or ""
    parts = data.split(":")
    if len(parts) < 3:
        return
    action = parts[1]
    supplement_id: int | None = None
    snooze_minutes = 15
    if action in ("done", "skip"):
        supplement_id = int(parts[2])
    elif action == "snooze":
        supplement_id = int(parts[2])
        if len(parts) >= 4:
            snooze_minutes = int(parts[3])
    else:
        return

    s = context.application.bot_data["settings"]
    tg_uid = update.effective_user.id
    ui_screen = ""
    payload_data: dict = {}
    ui_message_id: int | None = None
    conn = await db.connect(s.DB_PATH)
    try:
        if supplement_id is None:
            return
        if action == "done":
            await db.set_today_status(conn, tg_uid, supplement_id, "taken")
        elif action == "skip":
            await db.set_today_status(conn, tg_uid, supplement_id, "skipped")
        elif action == "snooze":
            minutes = snooze_minutes or 15
            guard = context.application.bot_data.setdefault("snooze_guard", {})
            key = f"{tg_uid}:{supplement_id}"
            now = datetime.utcnow()
            next_allowed = guard.get(key)
            if next_allowed and now < next_allowed:
                pass
            else:
                guard[key] = now + timedelta(minutes=minutes)
                snooze_name = f"snooze:{tg_uid}:{supplement_id}"
                for job in context.application.job_queue.get_jobs_by_name(snooze_name):
                    job.schedule_removal()
                context.application.job_queue.run_once(
                    reminder_fire_job,
                    when=timedelta(minutes=minutes),
                    name=snooze_name,
                    data={
                        "tg_user_id": tg_uid,
                        "supplement_id": supplement_id,
                    },
                )

        ui_message_id, ui_screen, ui_payload = await db.get_ui_state(conn, tg_uid)
        try:
            payload_data = json.loads(ui_payload) if ui_payload else {}
        except json.JSONDecodeError:
            payload_data = {}
        if isinstance(payload_data, dict) and "reminder_msg_id" in payload_data:
            payload_data.pop("reminder_msg_id", None)
            await db.update_ui_state_payload(conn, tg_uid, json.dumps(payload_data, ensure_ascii=False))
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
        if supplement_id is not None:
            await render_supplement_card(
                update,
                context,
                conn,
                int(supplement_id),
                return_page,
                return_mode=return_mode,
                search_query=search_query,
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
            await _show_my_list(update, context, conn, tg_user_id, page)
            return
        if view == "mysupp_hub":
            supplement_id = payload_data.get("supp_id")
            if supplement_id is not None:
                await show_my_supp_hub(update, context, tg_user_id, int(supplement_id), conn=conn)
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
    if route == "supp_card":
        if isinstance(payload, dict):
            supplement_id = payload.get("id")
            return_page = int(payload.get("return_page") or 0)
            return_mode = payload.get("return_mode") or "catalog"
            search_query = payload.get("query")
            if supplement_id is not None:
                await render_supplement_card(
                    update,
                    context,
                    conn,
                    int(supplement_id),
                    return_page,
                    return_mode=return_mode,
                    search_query=search_query,
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
    if route == "menu:settings":
        await show(
            update,
            context,
            "⚙️ <b>Настройки</b>",
            settings_menu(),
            route=route,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
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
    if route == "menu:checkin":
        context.user_data[WAITING_KEY] = "checkin"
        await show(
            update,
            context,
            _checkin_intro_text(),
            back(),
            route=route,
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
    if route.startswith("cat:page:"):
        payload = {"mode": "list", "list_mode": "evidence", "page": int(route.split(":")[-1])}
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
    if route.startswith("cat:list"):
        payload = await _load_catalog_payload(conn, tg_uid)
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
        page = int(route.split(":")[-1])
        await _show_my_list(
            update,
            context,
            conn,
            tg_uid,
            page,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route.startswith("mysupp:open:") or route.startswith("mysupp:hub:"):
        supplement_id = int(route.split(":")[-1])
        await show_my_supp_hub(
            update,
            context,
            tg_uid,
            supplement_id,
            conn=conn,
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
            "Напиши время в формате ЧЧ:ММ, например 09:30",
            my_hub_back(supplement_id),
            route=route,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route.startswith("mysupp:rem:"):
        supplement_id = int(route.split(":")[-1])
        await _show_my_supp_reminders(
            update,
            context,
            conn,
            tg_uid,
            supplement_id,
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
        groups = await engine.describe_interactions(conn, row, limit_each=8)
        text = engine.format_interactions(row["name"], groups)
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
    if route == "menu:ai":
        await home(update, context, target_chat_id=target_chat_id, target_message_id=target_message_id)
        return

    await home(update, context, target_chat_id=target_chat_id, target_message_id=target_message_id)

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    if context.user_data.get("onboarding") == "tz":
        try:
            await update.message.delete()
        except Exception:
            pass
        return
    txt = update.message.text.strip()
    s = context.application.bot_data["settings"]
    tg_uid = update.effective_user.id
    conn = await db.connect(s.DB_PATH)
    try:
        ui_message_id, ui_screen, ui_payload = await db.get_ui_state(conn, tg_uid)
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
    finally:
        await conn.close()
    waiting = context.user_data.get(WAITING_KEY)
    if waiting and waiting.startswith(("mysupp:set_dose:", "mysupp:rem_add:")):
        _, _, supplement_id_text = waiting.split(":", 2)
        supplement_id = int(supplement_id_text)
        s = context.application.bot_data["settings"]
        tg_uid = update.effective_user.id
        conn = await db.connect(s.DB_PATH)
        try:
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
                    await db.set_user_supp_dose(conn, tg_uid, supplement_id, trimmed)
                    context.user_data.pop(WAITING_KEY, None)
                    await show_my_supp_hub(update, context, tg_uid, supplement_id, conn=conn)
            else:
                if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", txt):
                    await show(
                        update,
                        context,
                        "Напиши время в формате ЧЧ:ММ, например 09:30",
                        my_hub_back(supplement_id),
                        route=f"mysupp:rem:add:{supplement_id}",
                    )
                else:
                    await db.upsert_reminder(conn, tg_uid, supplement_id, txt, enabled=1)
                    context.user_data.pop(WAITING_KEY, None)
                    await _show_my_supp_reminders(update, context, conn, tg_uid, supplement_id)
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
        elif waiting == "ai_goal":
            context.user_data.pop(WAITING_KEY, None)
            if not s.OPENAI_API_KEY:
                await show(update, context, "AI отключен: добавь OPENAI_API_KEY в .env.", back(), route="menu:ai")
            else:
                sups = await db.list_user_supplements(conn, tg_uid, limit=100)
                user_supp_text = ", ".join([r["name"] for r in sups]) or "нет"
                cfg = OpenAIConfig(api_key=s.OPENAI_API_KEY, model=s.OPENAI_MODEL)
                await show(update, context, "🤖 Думаю… (AI)", back(), route="menu:ai")
                raw = None
                try:
                    raw = await suggest_stack_v1(cfg, txt, user_supp_text)
                except Exception:
                    log.exception("OpenAI request failed")
                    await show(update, context, "AI временно недоступен.", back(), route="menu:ai")

                await _render_ai_suggestions(update, context, conn, txt, raw)
        elif waiting == "checkin":
            if not txt:
                await show(
                    update,
                    context,
                    "Похоже, сообщение пустое. Попробуй ещё раз.",
                    back(),
                    route="menu:checkin",
                )
            else:
                await _handle_checkin_submission(
                    update,
                    context,
                    conn,
                    tg_uid,
                    user_id,
                    tz_name,
                    txt,
                    "text",
                )

    finally:
        await conn.close()

    try:
        await update.message.delete()
    except Exception:
        pass

async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.voice:
        return
    waiting = context.user_data.get(WAITING_KEY)
    if waiting not in ("ai_goal", "checkin"):
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

        if not s.OPENAI_API_KEY:
            route = "menu:checkin" if waiting == "checkin" else "menu:ai"
            await show(update, context, "AI отключен: добавь OPENAI_API_KEY в .env.", back(), route=route)
            return

        route = "menu:checkin" if waiting == "checkin" else "menu:ai"
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
            model=s.OPENAI_TRANSCRIBE_MODEL,
        )
        transcription = ""
        try:
            transcription = await transcribe_voice_v1(transcribe_cfg, audio_bytes, filename)
        except Exception:
            await show(
                update,
                context,
                "AI сейчас недоступен для расшифровки. Попробуй позже.",
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
        if waiting == "checkin":
            await _handle_checkin_submission(
                update,
                context,
                conn,
                tg_uid,
                user_id,
                tz_name,
                transcription,
                "voice",
            )
        else:
            sups = await db.list_user_supplements(conn, tg_uid, limit=100)
            user_supp_text = ", ".join([r["name"] for r in sups]) or "нет"
            cfg = OpenAIConfig(api_key=s.OPENAI_API_KEY, model=s.OPENAI_MODEL)
            await show(update, context, "🤖 Думаю… (AI)", back(), route="menu:ai")
            raw = None
            try:
                raw = await suggest_stack_v1(cfg, transcription, user_supp_text)
            except Exception:
                log.exception("OpenAI request failed")
                await show(update, context, "AI временно недоступен.", back(), route="menu:ai")
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
        try:
            await update.message.delete()
        except Exception:
            pass

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error while processing update: %s", update, exc_info=context.error)

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
    if existing > 0:
        return

    supp_path = s.SEED_SUPPLEMENTS_PATH
    inter_path = s.SEED_INTERACTIONS_PATH
    if not supp_path or not os.path.exists(supp_path):
        log.warning("Supplements seed file missing, skipping auto-seed: %s", supp_path)
        return

    if inter_path and not os.path.exists(inter_path):
        log.warning("Interactions seed file missing, seeding supplements only: %s", inter_path)
        inter_path = None

    await seed_import._import(supp_path, inter_path)
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
    app.add_handler(CallbackQueryHandler(
        on_menu,
        pattern=r"^(menu:|settings:|add_pick:|compat_pick:|cat:|curated:|supp:|card:|mysupp:)",
    ))
    app.add_handler(CallbackQueryHandler(on_ai_add, pattern=r"^aiadd:"))
    app.add_handler(CallbackQueryHandler(on_checkin_action, pattern=r"^chk(apply|deny):"))
    app.add_handler(CallbackQueryHandler(on_timezone, pattern=r"^tz:"))
    app.add_handler(CallbackQueryHandler(on_reminder_action, pattern=r"^rem:"))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    app.job_queue.run_repeating(reminders_tick, interval=60, first=5, name="reminders_tick")
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
    await show(
        update,
        context,
        "📚 <b>Каталог БАДов</b>\nВыбери, как искать:",
        catalog_home(),
        route="cat:home",
        payload={"mode": "home"},
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )

async def _load_catalog_payload(conn, tg_uid: int) -> dict:
    view, payload = await db.get_user_last_view(conn, tg_uid)
    if view != "catalog":
        return {
            "mode": "list",
            "list_mode": "evidence",
            "page": 0,
            "query": "",
            "class_key": "",
        }
    try:
        payload_data = json.loads(payload) if payload else {}
    except json.JSONDecodeError:
        payload_data = {}
    if not isinstance(payload_data, dict):
        payload_data = {}
    payload_data.setdefault("mode", "list")
    payload_data.setdefault("list_mode", "evidence")
    payload_data.setdefault("page", 0)
    payload_data.setdefault("query", "")
    payload_data.setdefault("class_key", "")
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
        "🎯 <b>Цели/классы</b>\nВыбери категорию:",
        catalog_class_picker(
            sliced,
            page=page,
            has_prev=page > 0,
            has_next=end < len(classes),
        ),
        route=f"cat:class:page:{page}",
        payload={"mode": "home"},
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
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
    list_mode = payload.get("list_mode") or "evidence"
    page = max(int(payload.get("page") or 0), 0)
    offset = page * CATALOG_PAGE_SIZE
    query = str(payload.get("query") or "").strip()
    class_key = str(payload.get("class_key") or "").strip()

    if list_mode == "search":
        rows, total = await db.search_supplements(conn, query, page=page, page_size=CATALOG_PAGE_SIZE)
    elif list_mode == "class":
        total = await db.count_supplements_by_class(conn, class_key)
        rows = await db.list_supplements_by_class(conn, class_key, limit=CATALOG_PAGE_SIZE, offset=offset)
    else:
        total = await db.count_supplements(conn)
        rows = await db.list_supplements_sorted(conn, "evidence", limit=CATALOG_PAGE_SIZE, offset=offset)
        list_mode = "evidence"

    if not rows:
        if total == 0 and list_mode == "evidence":
            await show(
                update,
                context,
                "Каталог пуст. Похоже, сиды ещё не импортированы…",
                catalog_home(),
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
            catalog_home(),
            route="cat:home",
            payload={"mode": "home"},
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return

    total_pages = max(1, (total + CATALOG_PAGE_SIZE - 1) // CATALOG_PAGE_SIZE)
    lines = ["📚 <b>Каталог</b>"]
    if list_mode == "search":
        lines.append(f"Поиск: «{query}»")
    elif list_mode == "class":
        lines.append(f"Класс: {rules.get_catalog_class_title(class_key)}")
    else:
        lines.append("Сортировка: по доказательности")
    lines.append(f"Стр. {page + 1} из {total_pages}")

    await show(
        update,
        context,
        "\n".join(lines),
        catalog_list(
            rows,
            has_prev=page > 0,
            has_next=(offset + len(rows)) < total,
        ),
        route="cat:list",
        payload={
            "mode": "list",
            "list_mode": list_mode,
            "page": page,
            "query": query,
            "class_key": class_key,
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

    confidence = str(row["confidence"] or "").strip() or "неизвестно"
    human_trials = str(row["human_trials_count_est"] or "").strip() or "неизвестно"
    participants = str(row["participants_total_est"] or "").strip() or "неизвестно"
    avg_effect = str(row["avg_effect_percent_est"] or "").strip() or "неизвестно"

    text = (
        f"💊 <b>{title}</b>\n\n"
        f"Кратко: {summary}\n\n"
        f"Доказательность: {confidence}\n"
        f"Исследования на людях: {human_trials}\n"
        f"Участников суммарно: {participants}\n"
        f"Средний эффект: {avg_effect}"
    )

    in_my = await db.user_has_supplement(conn, tg_uid, int(row["id"]))
    btns: list[list[InlineKeyboardButton]] = []
    if in_my:
        btns.append([InlineKeyboardButton("➖ Убрать из моих", callback_data=f"card:remove:{row['id']}")])
    else:
        btns.append([InlineKeyboardButton("➕ Добавить в мои", callback_data=f"card:add:{row['id']}")])
    btns.append([InlineKeyboardButton("🧩 Совместимость", callback_data=f"card:compat:{row['id']}")])
    btns.append([InlineKeyboardButton("📊 Доказательность", callback_data=f"card:evidence:{row['id']}")])
    if in_my:
        btns.append([InlineKeyboardButton("⚙️ Управление", callback_data=f"mysupp:open:{row['id']}")])
    btns.append([InlineKeyboardButton("⬅️ Назад", callback_data="cat:back")])

    payload = await _load_catalog_payload(conn, tg_uid)
    await show(
        update,
        context,
        text,
        InlineKeyboardMarkup(btns),
        route="cat:card",
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
    confidence = str(row["confidence"] or "").strip() or "неизвестно"
    human_trials = str(row["human_trials_count_est"] or "").strip() or "неизвестно"
    participants = str(row["participants_total_est"] or "").strip() or "неизвестно"
    avg_effect = str(row["avg_effect_percent_est"] or "").strip() or "неизвестно"

    text = (
        f"📊 <b>Доказательность</b>\n"
        f"{title}\n\n"
        f"Уверенность: {confidence}\n"
        f"Исследования на людях: {human_trials}\n"
        f"Участников суммарно: {participants}\n"
        f"Средний эффект: {avg_effect}"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"cat:open:{row['id']}")]])
    payload = await _load_catalog_payload(conn, tg_uid)
    await show(
        update,
        context,
        text,
        kb,
        route="cat:evidence",
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
        f"{query}\n"
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
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 В меню", callback_data="main")]]),
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
    return_mode: str = "catalog",
    search_query: str | None = None,
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
                    [InlineKeyboardButton("🏠 В меню", callback_data="main")],
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
    name_ru = str(row.get("name_ru") or "").strip()
    name_en = str(row.get("name_en") or "").strip()
    title = name_ru or name_en or str(row.get("name") or "").strip() or "Без названия"
    evidence = (
        str(row.get("evidence_grade") or "").strip()
        or str(row.get("confidence") or "").strip()
        or "—"
    )
    human_trials = str(row.get("human_trials_count_est") or "").strip() or "—"
    participants = str(row.get("participants_total_est") or "").strip() or "—"
    avg_effect = str(row.get("avg_effect_percent_est") or "").strip() or "—"

    lines = [
        f"<b>{title}</b>",
        f"Англ: {name_en or '—'}",
    ]
    if show_added:
        lines.append("✅ Добавлено в «Мои БАДы»")
    lines.extend(
        [
            "",
            f"Доказательность: {evidence}",
            f"Исследования на людях: {human_trials}",
            f"Участников всего: {participants}",
            f"Средний эффект: {avg_effect}",
            "",
            "(Скоро: совместимость, оптимальное время, дозировки, молекулярное объяснение)",
        ]
    )
    add_callback = (
        f"mysupp:add:{supplement_id}:retsearch:{return_page}"
        if return_mode == "search"
        else f"mysupp:add:{supplement_id}:retcat:{return_page}"
    )
    back_callback = (
        f"cat:sres:{return_page}" if return_mode == "search" else f"cat:page:{return_page}"
    )
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Добавить в мои", callback_data=add_callback)],
            [InlineKeyboardButton("⬅️ Назад", callback_data=back_callback)],
            [InlineKeyboardButton("🏠 В меню", callback_data="main")],
        ]
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
        "return_page": return_page,
        "return_mode": return_mode,
    }
    if return_mode == "search" and search_query:
        payload["query"] = search_query
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
        curated_stacks_list(display_rows, page, has_prev=page > 0, has_next=has_next),
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
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
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
    lines = [f"🧠 <b>{stack['title_ru']}</b>"]
    author = str(stack["author_ru"] or "").strip()
    description = str(stack["description_ru"] or "").strip()
    if author:
        lines.append(f"Автор: {author}")
    if description:
        lines.extend(["", description])

    if items:
        lines.append("")
        lines.append("Состав:")
        max_items = 15
        for item in items[:max_items]:
            name = (
                str(item["supplement_name"] or "").strip()
                or str(item["supplement_slug"] or "").strip()
                or "Без названия"
            )
            parts = [name]
            dose = str(item["dose_ru"] or "").strip()
            timing = str(item["timing_ru"] or "").strip()
            if dose:
                parts.append(dose)
            if timing:
                parts.append(timing)
            lines.append("• " + " — ".join(parts))
        if len(items) > max_items:
            lines.append(f"…и ещё {len(items) - max_items} поз.")

    lines.append("")
    lines.append("<i>Это не медсовет.</i>")
    back_page = int(context.user_data.get("curated_page") or 0)
    await show(
        update,
        context,
        "\n".join(lines),
        curated_stack_card(slug, source_url=stack["source_url"], back_page=back_page),
        route=f"curated:open:{slug}",
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )

async def _show_my_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    tg_uid: int,
    page: int,
    *,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    await db.get_or_create_user(conn, tg_uid)
    local_date = await db.ensure_daily_rollover(conn, tg_uid)
    rows, total = await db.list_user_supplements_page(
        conn,
        tg_uid,
        q=None,
        page=page,
        page_size=MY_PAGE_SIZE,
        local_date=local_date,
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

    suffix = f" (стр. {page + 1})" if total > MY_PAGE_SIZE else ""
    title = "💊 <b>Мои БАДы</b>\nНажми на добавку, чтобы открыть управление."
    await show(
        update,
        context,
        f"{title}{suffix}",
        my_supplements_list(rows, page, has_prev=page > 0, has_next=has_next, show_clear=False),
        route=f"mysupp:list:{page}",
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
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    close_conn = False
    if conn is None:
        s = context.application.bot_data["settings"]
        conn = await db.connect(s.DB_PATH)
        close_conn = True
    try:
        internal_user_id = await db.get_or_create_user(conn, user_id)
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
        reminders = await db.list_reminders_for_user(conn, user_id)
        reminder_times = [r["time_local"] for r in reminders if int(r["supplement_id"]) == supplement_id]
        reminder_list = ", ".join(reminder_times) if reminder_times else "нет"

        name_ru = (row["name_ru"] or row["name"] or "").strip()
        name_en = (row["name_en"] or "").strip()
        name_line = f"💊 <b>{name_ru}</b>"
        if name_en and name_en != name_ru:
            name_line = f"{name_line} ({name_en})"
        dose_text = (row["dose_text"] or "").strip() or "—"
        status_text = "не отмечено"
        if today_status == "taken":
            status_text = "выпил"
        elif today_status == "skipped":
            status_text = "не выпил"

        text = (
            "⚙️ <b>Управление добавкой</b>\n\n"
            f"{name_line}\n\n"
            f"Доза: {dose_text}\n"
            f"Статус на сегодня: {status_text}\n"
            f"Напоминания: {reminder_list}\n"
        )
        await show(
            update,
            context,
            text,
            my_supplement_hub(supplement_id),
            route=f"mysupp:hub:{supplement_id}",
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
    finally:
        if close_conn:
            await conn.close()

async def _show_my_supp_reminders(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    tg_uid: int,
    supplement_id: int,
    *,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    supplement = await db.get_supplement_by_id(conn, supplement_id)
    if not supplement:
        await show(update, context, "БАД не найден.", my_list_back(), route="mysupp:list:0")
        return
    reminders = await db.list_reminders_for_user(conn, tg_uid)
    reminder_times = [str(r["time_local"]) for r in reminders if int(r["supplement_id"]) == supplement_id]
    name = (supplement["name_ru"] or supplement["name"] or "").strip()
    title = f"⏰ <b>Напоминания</b>\n{name}"
    if reminder_times:
        lines = [title, "", "Время:", *[f"• {t}" for t in reminder_times]]
    else:
        lines = [title, "", "Время: нет"]
    await show(
        update,
        context,
        "\n".join(lines),
        my_supplement_reminders(supplement_id, reminder_times),
        route=f"mysupp:rem:{supplement_id}",
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
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
    title = engine._supp_title(row)
    compat = await engine.build_compatibility(conn, supplement_id)
    items = compat.get(kind) or []
    page = max(page, 0)
    page_size = 8
    start = page * page_size
    end = start + page_size
    page_items = items[start:end]
    has_prev = page > 0
    has_next = end < len(items)
    icon = {"synergy": "✅", "antagonism": "⛔", "caution": "⚠️"}.get(kind, "✅")
    header = f"{icon} {('Совместимо' if kind == 'synergy' else 'Несовместимо' if kind == 'antagonism' else 'Осторожно')} с {title}"
    lines = [f"<b>{header}</b>"]
    if not page_items:
        lines.append("")
        lines.append("Пока нет данных. Мы добавим позже.")
    else:
        lines.append("")
        for item in page_items:
            reason = (item.get("reason_ru") or "").strip()
            reason_text = f" — {reason}" if reason else ""
            lines.append(f"• {item['title']}{reason_text}")
    source_map = {"db": "DB", "rules": "Правила", "mixed": "Смешано"}
    lines.append("")
    lines.append(f"Источник: {source_map.get(compat.get('source'), 'DB')}")
    await show(
        update,
        context,
        "\n".join(lines),
        supplement_compat_list(
            supplement_id,
            page_items,
            kind=kind,
            page=page,
            has_prev=has_prev,
            has_next=has_next,
            back_data=back_data,
            nav_prefix=route_prefix,
        ),
        route=f"{route_prefix}:{kind}:{page}" if route_prefix else f"supp:compat:{supplement_id}:{kind}:{page}",
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
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
