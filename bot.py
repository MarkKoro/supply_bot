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
from ux import show, restore_last_screen
from keyboards import (
    main_menu,
    back,
    my_list_back,
    my_hub_back,
    pick_search_results,
    my_supplements_list,
    catalog_page,
    curated_stacks_list,
    curated_stack_card,
    supplement_page,
    supplement_compat_list,
    my_supplement_hub,
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
MY_SEARCH_KEY = "my_search_q"
CATALOG_PAGE_SIZE = 8
MY_PAGE_SIZE = 10
CURATED_PAGE_SIZE = 10

def _format_tz_label(tz_name: str | None) -> str:
    if not tz_name:
        return "UTC"
    match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", tz_name)
    if match:
        sign, hours, minutes = match.groups()
        hours_int = int(hours)
        if minutes == "00":
            return f"UTC{sign}{hours_int}"
        return f"UTC{sign}{hours_int}:{minutes}"
    return tz_name

def _job_name(tg_user_id: int, supplement_id: int) -> str:
    return f"rem:{tg_user_id}:{supplement_id}"

def _debug_job_name(tg_user_id: int, supplement_id: int) -> str:
    return f"dbg:{tg_user_id}:{supplement_id}:{int(datetime.utcnow().timestamp())}"

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
            rows = await db.search_supplements(conn, name, limit=1)
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
        rows = await db.search_supplements(conn, name, limit=1)
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
    day = await db.get_user_local_day(conn, user_id)
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
    day = await db.get_user_local_day(conn, user_id)
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
        if not tz:
            context.user_data.pop("tz_back_data", None)
            await _show_timezone_picker(update, context, "ru", 0)
            return
    finally:
        await conn.close()
    await home(update, context)

async def _show_timezone_picker(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    mode: str,
    page: int,
    *,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
    back_data: str | None = None,
) -> None:
    utc_now = tz.now_utc_hhmm()
    await show(
        update,
        context,
        "Выбери часовой пояс.\n\n"
        f"Сейчас по UTC: {utc_now}\n"
        "После выбора мы будем присылать напоминания по твоему местному времени.",
        timezone_picker(mode, page, back_data=back_data),
        route=f"tz:page:{mode}:{page}",
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )

def _is_debug_user(settings, tg_uid: int) -> bool:
    return bool(settings.DEBUG or (tg_uid in settings.ADMIN_IDS))

async def _remove_reminder_job(job_queue, tg_user_id: int, supplement_id: int) -> None:
    name = _job_name(tg_user_id, supplement_id)
    for job in job_queue.get_jobs_by_name(name):
        job.schedule_removal()

async def _schedule_reminder_job(
    job_queue,
    tg_user_id: int,
    supplement_id: int,
    time_hhmm: str,
    tz_name: str,
    *,
    reschedule_daily: bool = True,
) -> None:
    await _remove_reminder_job(job_queue, tg_user_id, supplement_id)
    run_at = db.next_run_datetime_for_hhmm(time_hhmm, tz_name)
    job_queue.run_once(
        _send_reminder_job,
        when=run_at,
        name=_job_name(tg_user_id, supplement_id),
        data={
            "tg_user_id": tg_user_id,
            "supplement_id": supplement_id,
            "hhmm": time_hhmm,
            "reschedule_daily": reschedule_daily,
        },
    )

async def schedule_debug_reminder(
    job_queue,
    tg_user_id: int,
    supplement_id: int,
    delay_seconds: int,
) -> None:
    job_queue.run_once(
        _send_reminder_job,
        when=timedelta(seconds=delay_seconds),
        name=_debug_job_name(tg_user_id, supplement_id),
        data={
            "tg_user_id": tg_user_id,
            "supplement_id": supplement_id,
            "delay_seconds": delay_seconds,
            "debug": True,
            "reschedule_daily": False,
        },
    )

async def _schedule_all_reminders(app: Application) -> None:
    s = app.bot_data["settings"]
    conn = await db.connect(s.DB_PATH)
    try:
        rows = await db.list_enabled_schedules(conn)
        for row in rows:
            await _schedule_reminder_job(
                app.job_queue,
                int(row["tg_user_id"]),
                int(row["supplement_id"]),
                str(row["time_hhmm"]),
                str(row["tz_name"]),
            )
    finally:
        await conn.close()

async def _send_reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data or {}
    tg_user_id = int(data.get("tg_user_id"))
    supplement_id = int(data.get("supplement_id"))
    debug = bool(data.get("debug"))
    hhmm = str(data.get("hhmm")) if data.get("hhmm") is not None else None
    tz_name: str | None = None
    s = context.application.bot_data["settings"]
    conn = await db.connect(s.DB_PATH)
    try:
        tz_name = await db.get_user_tz(conn, tg_user_id)
        if not tz_name:
            return
        supplement_name = await db.get_supplement_name(conn, supplement_id)
        if not supplement_name:
            return

        if debug:
            delay_seconds = int(data.get("delay_seconds") or 0)
            delay_label = f"через {delay_seconds} сек" if delay_seconds else "скоро"
            text = (
                "🧪 Тестовое напоминание\n"
                f"Пора принять: <b>{supplement_name}</b>\n"
                f"Время: {delay_label}"
            )
        else:
            schedule = await db.get_user_schedule(conn, tg_user_id, supplement_id)
            if not schedule or not schedule["time_hhmm"] or int(schedule["is_enabled"]) != 1:
                return
            if hhmm is None or str(schedule["time_hhmm"]) != hhmm:
                return
            tz_label = _format_tz_label(tz_name)
            text = f"Пора принять: <b>{supplement_name}</b>\nВремя: {hhmm} ({tz_label})"
        await context.bot.send_message(
            chat_id=tg_user_id,
            text=text,
            reply_markup=reminder_actions(supplement_id),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    finally:
        await conn.close()

    if not debug and data.get("reschedule_daily", True) and tz_name and hhmm:
        await _schedule_reminder_job(
            context.application.job_queue,
            tg_user_id,
            supplement_id,
            hhmm,
            tz_name,
            reschedule_daily=True,
        )

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
        if not tz_name:
            context.user_data.pop("tz_back_data", None)
            await _show_timezone_picker(update, context, "ru", 0)
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
            await _show_timezone_picker(update, context, "ru", 0, back_data="menu:settings")
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

        if data == "menu:catalog":
            await _show_catalog_page(update, context, conn, 0)
            return

        if data == "menu:curated":
            await _show_curated_page(update, context, conn, 0)
            return

        if data == "menu:my":
            context.user_data.pop(MY_SEARCH_KEY, None)
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

        if data.startswith("cat:page:"):
            page = int(data.split(":")[-1])
            await _show_catalog_page(update, context, conn, page)
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

        if data.startswith("mine:list:"):
            page = int(data.split(":")[-1])
            await _show_my_list(update, context, conn, tg_uid, page)
            return

        if data == "mine:search":
            context.user_data[WAITING_KEY] = "my_search_query"
            await show(
                update,
                context,
                "Введи название БАДА для поиска в твоём списке.",
                my_list_back(),
                route="mine:search",
            )
            return

        if data == "mine:clear":
            context.user_data.pop(MY_SEARCH_KEY, None)
            await _show_my_list(update, context, conn, tg_uid, 0)
            return

        if data.startswith("mine:open:") or data.startswith("mine:hub:"):
            supplement_id = int(data.split(":")[-1])
            await show_my_supp_hub(update, context, tg_uid, supplement_id, conn=conn)
            return

        if data.startswith("my:add:"):
            supplement_id = int(data.split(":")[-1])
            await db.add_user_supplement(conn, tg_uid, supplement_id)
            await show_my_supp_hub(update, context, tg_uid, supplement_id, conn=conn)
            return

        if data == "mine:back":
            await _show_my_list(update, context, conn, tg_uid, 0)
            return

        if data.startswith("mine:hub:toggle:"):
            supplement_id = int(data.split(":")[-1])
            internal_user_id = await db.get_or_create_user(conn, tg_uid)
            settings = await db.get_user_supp_settings(conn, internal_user_id, supplement_id)
            await db.set_user_supp_reminder_enabled(
                conn,
                internal_user_id,
                supplement_id,
                not bool(settings.get("reminder_enabled")),
            )
            await show_my_supp_hub(update, context, tg_uid, supplement_id, conn=conn)
            return

        if data.startswith("mine:hub:set_time:"):
            supplement_id = int(data.split(":")[-1])
            context.user_data[WAITING_KEY] = f"set_time:{supplement_id}"
            await show(
                update,
                context,
                "Напиши время в формате ЧЧ:ММ, например 09:30",
                my_hub_back(supplement_id),
                route=f"mine:hub:set_time:{supplement_id}",
            )
            return

        if data.startswith("mine:hub:set_dose:"):
            supplement_id = int(data.split(":")[-1])
            context.user_data[WAITING_KEY] = f"set_dose:{supplement_id}"
            await show(
                update,
                context,
                "Напиши дозу (до 80 символов).",
                my_hub_back(supplement_id),
                route=f"mine:hub:set_dose:{supplement_id}",
            )
            return

        if data.startswith("mine:hub:taken:"):
            supplement_id = int(data.split(":")[-1])
            internal_user_id = await db.get_or_create_user(conn, tg_uid)
            tz_name = await db.get_user_tz(conn, tg_uid) or "UTC"
            try:
                today = datetime.now(db.parse_offset_tz(tz_name)).date().isoformat()
            except Exception:
                today = datetime.utcnow().date().isoformat()
            await db.set_user_supp_taken_date(conn, internal_user_id, supplement_id, today)
            await db.set_user_supp_skip_date(conn, internal_user_id, supplement_id, "")
            await show_my_supp_hub(update, context, tg_uid, supplement_id, conn=conn)
            return

        if data.startswith("mine:hub:skip:"):
            supplement_id = int(data.split(":")[-1])
            internal_user_id = await db.get_or_create_user(conn, tg_uid)
            tz_name = await db.get_user_tz(conn, tg_uid) or "UTC"
            try:
                today = datetime.now(db.parse_offset_tz(tz_name)).date().isoformat()
            except Exception:
                today = datetime.utcnow().date().isoformat()
            await db.set_user_supp_skip_date(conn, internal_user_id, supplement_id, today)
            await db.set_user_supp_taken_date(conn, internal_user_id, supplement_id, "")
            await show_my_supp_hub(update, context, tg_uid, supplement_id, conn=conn)
            return

        if data.startswith("mine:hub:remove:"):
            supplement_id = int(data.split(":")[-1])
            internal_user_id = await db.get_or_create_user(conn, tg_uid)
            await db.remove_user_supplement(conn, tg_uid, supplement_id)
            await db.delete_user_supp_settings(conn, internal_user_id, supplement_id)
            await _show_my_list(update, context, conn, tg_uid, 0)
            return

        if data.startswith("mine:hub:compat:"):
            parts = data.split(":")
            supplement_id = int(parts[3])
            kind = "synergy"
            page = 0
            if len(parts) >= 6:
                kind = parts[4]
                page = int(parts[5])
            await _show_supplement_compat(
                update,
                context,
                conn,
                supplement_id,
                kind,
                page,
                back_data=f"mine:hub:{supplement_id}",
                route_prefix=f"mine:hub:compat:{supplement_id}",
            )
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
        _, _, mode, page = data.split(":", 3)
        back_data = context.user_data.get("tz_back_data") if isinstance(context.user_data, dict) else None
        await _show_timezone_picker(update, context, mode, int(page), back_data=back_data)
        return
    if data.startswith("tz:pick:"):
        tz_value = data.split(":", 2)[2]
        s = context.application.bot_data["settings"]
        tg_uid = update.effective_user.id
        conn = await db.connect(s.DB_PATH)
        try:
            await db.get_or_create_user(conn, tg_uid, tz_name="")
            await db.set_user_tz(conn, tg_uid, tz_value)
            rows = await db.list_user_schedules(conn, tg_uid)
            for row in rows:
                if not row["time_hhmm"] or int(row["is_enabled"]) != 1:
                    continue
                await _schedule_reminder_job(
                    context.application.job_queue,
                    tg_uid,
                    int(row["supplement_id"]),
                    str(row["time_hhmm"]),
                    tz_value,
                )
        finally:
            await conn.close()
        context.user_data.pop("tz_back_data", None)
        local_now = tz.now_local_hhmm(tz_value)
        label = tz.format_confirmation_label(tz_value)
        await show(
            update,
            context,
            "✅ Часовой пояс установлен: "
            f"{label}\n"
            f"Сейчас у тебя будет: {local_now}\n\n"
            f"{_home_text()}",
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
    supplement_id = int(parts[2])
    s = context.application.bot_data["settings"]
    tg_uid = update.effective_user.id
    conn = await db.connect(s.DB_PATH)
    try:
        if action in ("taken", "skip"):
            tz = await db.get_user_tz(conn, tg_uid)
            local_date = db.get_user_local_date(tz)
            status = "taken" if action == "taken" else "skipped"
            await db.set_today_intake_status(conn, tg_uid, supplement_id, local_date, status)
        elif action == "snooze":
            tz_name = await db.get_user_tz(conn, tg_uid)
            schedule = await db.get_user_schedule(conn, tg_uid, supplement_id)
            hhmm = str(schedule["time_hhmm"]) if schedule and schedule["time_hhmm"] else None
            if tz_name and hhmm:
                snooze_name = f"snooze:{tg_uid}:{supplement_id}:{int(datetime.utcnow().timestamp())}"
                context.application.job_queue.run_once(
                    _send_reminder_job,
                    when=timedelta(minutes=15),
                    name=snooze_name,
                    data={
                        "tg_user_id": tg_uid,
                        "supplement_id": supplement_id,
                        "hhmm": hhmm,
                        "reschedule_daily": False,
                    },
                )
    finally:
        await conn.close()

    if q.message:
        try:
            await q.message.delete()
        except Exception:
            pass

    await restore_last_screen(update, context)

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
    if route == "menu:todayplan":
        await _show_today_plan(update, context, conn, tg_uid)
        return
    if route == "mine:search":
        context.user_data[WAITING_KEY] = "my_search_query"
        await show(
            update,
            context,
            "Введи название БАДА для поиска в твоём списке.",
            my_list_back(),
            route=route,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route.startswith("cat:page:"):
        page = int(route.split(":")[-1])
        await _show_catalog_page(
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
    if route.startswith("mine:list:"):
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
    if route.startswith("mine:open:") or route.startswith("mine:hub:"):
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
    if route.startswith("mine:hub:set_dose:"):
        supplement_id = int(route.split(":")[-1])
        context.user_data[WAITING_KEY] = f"set_dose:{supplement_id}"
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
    if route.startswith("mine:hub:set_time:"):
        supplement_id = int(route.split(":")[-1])
        context.user_data[WAITING_KEY] = f"set_time:{supplement_id}"
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
    if route.startswith("mine:hub:compat:"):
        _, _, _, supplement_id, kind, page = route.split(":", 5)
        await _show_supplement_compat(
            update,
            context,
            conn,
            int(supplement_id),
            kind,
            int(page),
            back_data=f"mine:hub:{supplement_id}",
            route_prefix=f"mine:hub:compat:{supplement_id}",
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route.startswith("tz:page:"):
        _, _, mode, page = route.split(":", 3)
        back_data = context.user_data.get("tz_back_data") if isinstance(context.user_data, dict) else None
        await _show_timezone_picker(
            update,
            context,
            mode,
            int(page),
            back_data=back_data,
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
    txt = update.message.text.strip()
    waiting = context.user_data.get(WAITING_KEY)
    if waiting and waiting.startswith(("set_time:", "set_dose:")):
        _, supplement_id_text = waiting.split(":", 1)
        supplement_id = int(supplement_id_text)
        s = context.application.bot_data["settings"]
        tg_uid = update.effective_user.id
        conn = await db.connect(s.DB_PATH)
        try:
            internal_user_id = await db.get_or_create_user(conn, tg_uid)
            if waiting.startswith("set_time:"):
                if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", txt):
                    await show(
                        update,
                        context,
                        "Напиши время в формате ЧЧ:ММ, например 09:30",
                        my_hub_back(supplement_id),
                        route=f"mine:hub:set_time:{supplement_id}",
                    )
                else:
                    await db.set_user_supp_reminder_time(conn, internal_user_id, supplement_id, txt)
                    context.user_data.pop(WAITING_KEY, None)
                    await show_my_supp_hub(update, context, tg_uid, supplement_id, conn=conn)
            else:
                trimmed = txt.strip()
                if not trimmed or len(trimmed) > 80:
                    await show(
                        update,
                        context,
                        "Напиши дозу (до 80 символов).",
                        my_hub_back(supplement_id),
                        route=f"mine:hub:set_dose:{supplement_id}",
                    )
                else:
                    await db.set_user_supp_dose(conn, internal_user_id, supplement_id, trimmed)
                    context.user_data.pop(WAITING_KEY, None)
                    await show_my_supp_hub(update, context, tg_uid, supplement_id, conn=conn)
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
        if not tz_name:
            context.user_data.pop("tz_back_data", None)
            await _show_timezone_picker(update, context, "ru", 0)
            return

        if waiting == "add_query":
            rows = await db.search_supplements(conn, txt, limit=10)
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
            rows = await db.search_supplements(conn, txt, limit=10)
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

        elif waiting == "my_search_query":
            context.user_data.pop(WAITING_KEY, None)
            if txt:
                context.user_data[MY_SEARCH_KEY] = txt
                await _show_my_list(update, context, conn, tg_uid, 0, q=txt)
            else:
                context.user_data.pop(MY_SEARCH_KEY, None)
                await _show_my_list(update, context, conn, tg_uid, 0)
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
        if not tz_name:
            context.user_data.pop("tz_back_data", None)
            await _show_timezone_picker(update, context, "ru", 0)
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
        pattern=r"^(menu:|settings:|add_pick:|compat_pick:|cat:page:|curated:|supp:|my:|mine:)",
    ))
    app.add_handler(CallbackQueryHandler(on_ai_add, pattern=r"^aiadd:"))
    app.add_handler(CallbackQueryHandler(on_checkin_action, pattern=r"^chk(apply|deny):"))
    app.add_handler(CallbackQueryHandler(on_timezone, pattern=r"^tz:"))
    app.add_handler(CallbackQueryHandler(on_reminder_action, pattern=r"^rem:"))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    await _schedule_all_reminders(app)
    return app

def build_app() -> Application:
    return asyncio.run(_bootstrap())

async def _show_catalog_page(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    page: int,
    *,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    page = max(page, 0)
    offset = page * CATALOG_PAGE_SIZE
    rows = await db.list_supplements_page(conn, offset=offset, limit=CATALOG_PAGE_SIZE + 1)
    has_next = len(rows) > CATALOG_PAGE_SIZE
    display_rows = rows[:CATALOG_PAGE_SIZE]
    if not display_rows:
        await show(update, context, "Каталог пуст. Похоже, сиды ещё не импортированы…", back(), route="menu:catalog")
        return
    text = f"📚 <b>Каталог</b> (стр. {page + 1})"
    await show(
        update,
        context,
        text,
        catalog_page(display_rows, page, has_prev=page > 0, has_next=has_next),
        route=f"cat:page:{page}",
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
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
    q: str | None = None,
    *,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    if q is None:
        q = context.user_data.get(MY_SEARCH_KEY)
    user_id = await db.get_or_create_user(conn, tg_uid)
    tz = await db.get_user_tz(conn, tg_uid)
    local_date = db.get_user_local_date(tz)
    rows, total = await db.list_user_supplements_page(
        conn,
        tg_uid,
        q=q,
        page=page,
        page_size=MY_PAGE_SIZE,
        local_date=local_date,
    )
    has_next = (page + 1) * MY_PAGE_SIZE < total
    if not rows:
        if q:
            await show(
                update,
                context,
                f"Ничего не найдено по запросу: <b>{q}</b>",
                my_supplements_list([], 0, False, False, show_clear=True),
                route="mine:list:0",
                target_chat_id=target_chat_id,
                target_message_id=target_message_id,
            )
        else:
            await show(
                update,
                context,
                "Пока список пуст. Добавь БАД из каталога.",
                my_supplements_list([], 0, False, False, show_clear=False),
                route="mine:list:0",
                target_chat_id=target_chat_id,
                target_message_id=target_message_id,
            )
        return

    suffix = f" (стр. {page + 1})" if total > MY_PAGE_SIZE else ""
    title = "📦 <b>Мои БАДы</b>\nНажми на добавку, чтобы открыть управление."
    if q:
        title = f"📦 <b>Мои БАДы</b>\nПоиск: <b>{q}</b>\nНажми на добавку, чтобы открыть управление."
    await show(
        update,
        context,
        f"{title}{suffix}",
        my_supplements_list(rows, page, has_prev=page > 0, has_next=has_next, show_clear=bool(q)),
        route=f"mine:list:{page}",
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
                route="mine:list:0",
                target_chat_id=target_chat_id,
                target_message_id=target_message_id,
            )
            return
        settings = await db.get_user_supp_settings(conn, internal_user_id, supplement_id)
        tz_name = await db.get_user_tz(conn, user_id) or "UTC"
        try:
            today = datetime.now(db.parse_offset_tz(tz_name)).date().isoformat()
        except Exception:
            today = datetime.utcnow().date().isoformat()

        name_ru = (row.get("name_ru") or row.get("name") or "").strip()
        name_en = (row.get("name_en") or "").strip()
        name_line = f"💊 <b>{name_ru}</b>"
        if name_en and name_en != name_ru:
            name_line = f"{name_line} ({name_en})"
        dose_text = (settings.get("dose_text") or "").strip() or "не задана"
        reminder_enabled = bool(settings.get("reminder_enabled"))
        reminder_time = (settings.get("reminder_time") or "").strip() or "не задано"
        taken_today = settings.get("taken_date") == today
        skip_today = settings.get("skip_date") == today

        text = (
            "⚙️ <b>Управление добавкой</b>\n\n"
            f"{name_line}\n\n"
            f"Доза: {dose_text}\n"
            f"Напоминание: {'включено' if reminder_enabled else 'выключено'}\n"
            f"Время: {reminder_time}\n\n"
            f"✅ Выпил сегодня: {'да' if taken_today else 'нет'}\n"
            f"🚫 Не пью сегодня: {'да' if skip_today else 'нет'}\n"
        )
        await show(
            update,
            context,
            text,
            my_supplement_hub(supplement_id, reminder_enabled),
            route=f"mine:hub:{supplement_id}",
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
    finally:
        if close_conn:
            await conn.close()

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
