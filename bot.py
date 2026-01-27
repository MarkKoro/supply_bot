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
    supplement_page,
    supplement_compat_back,
    my_supplement_hub,
    timezone_picker,
    reminder_actions,
)
from openai_client import OpenAIConfig, suggest_stack_v1

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("supply-bot")

WAITING_KEY = "waiting"
AWAITING_INPUT_KEY = "awaiting_input"
MY_SEARCH_KEY = "my_search_q"
CATALOG_PAGE_SIZE = 8
MY_PAGE_SIZE = 10

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

async def home(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    await show(
        update, context,
        "💊 <b>Supply Bot</b>\n\n"
        "Я справочник + дневник добавок.\n"
        "— Совместимость из базы\n"
        "— AI-подбор (опционально)\n\n"
        "<i>Не медицинский совет.</i>",
        main_menu(),
        route="menu:home",
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = context.application.bot_data["settings"]
    conn = await db.connect(s.DB_PATH)
    try:
        await db.get_or_create_user(conn, update.effective_user.id, tz_name="")
        tz = await db.get_user_tz(conn, update.effective_user.id)
        if not tz:
            await _show_timezone_picker(update, context, 0)
            return
    finally:
        await conn.close()
    await home(update, context)

async def _show_timezone_picker(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    page: int,
    *,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    await show(
        update,
        context,
        "Выбери свой часовой пояс (это нужно для напоминаний):",
        timezone_picker(page),
        route=f"tz:page:{page}",
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )

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
        await db.get_or_create_user(conn, tg_uid, tz_name="")
        tz_name = await db.get_user_tz(conn, tg_uid)
        if not tz_name:
            await _show_timezone_picker(update, context, 0)
            return

        if data == "menu:home":
            await home(update, context)
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

        if data.startswith("supp:view:"):
            _, _, slug, page = data.split(":", 3)
            await _show_supplement_page(update, context, conn, tg_uid, slug, int(page))
            return

        if data.startswith("my:list:"):
            page = int(data.split(":")[-1])
            await _show_my_list(update, context, conn, tg_uid, page)
            return

        if data == "my:search":
            context.user_data[WAITING_KEY] = "my_search_query"
            await show(
                update,
                context,
                "Введи название БАДА для поиска в твоём списке.",
                my_list_back(),
                route="my:search",
            )
            return

        if data == "my:clear":
            context.user_data.pop(MY_SEARCH_KEY, None)
            await _show_my_list(update, context, conn, tg_uid, 0)
            return

        if data.startswith("my:open:"):
            supplement_id = int(data.split(":")[-1])
            await _show_my_hub(update, context, conn, tg_uid, supplement_id)
            return

        if data.startswith("my:add:"):
            supplement_id = int(data.split(":")[-1])
            await db.add_user_supplement(conn, tg_uid, supplement_id)
            await _show_my_hub(update, context, conn, tg_uid, supplement_id)
            return

        if data.startswith("my:remove:"):
            supplement_id = int(data.split(":")[-1])
            await db.remove_user_supplement(conn, tg_uid, supplement_id)
            await _show_my_list(update, context, conn, tg_uid, 0)
            return

        if data.startswith("my:toggle:"):
            supplement_id = int(data.split(":")[-1])
            await db.toggle_user_supplement_active(conn, tg_uid, supplement_id)
            await _show_my_hub(update, context, conn, tg_uid, supplement_id)
            return

        if data.startswith("my:setdose:"):
            supplement_id = int(data.split(":")[-1])
            context.user_data[AWAITING_INPUT_KEY] = {
                "kind": "dose",
                "supplement_id": supplement_id,
                "return": f"my:open:{supplement_id}",
            }
            await show(
                update,
                context,
                "Введи дозу (до 200 символов).",
                my_hub_back(supplement_id),
                route=f"my:setdose:{supplement_id}",
            )
            return

        if data.startswith("my:setnote:"):
            supplement_id = int(data.split(":")[-1])
            context.user_data[AWAITING_INPUT_KEY] = {
                "kind": "note",
                "supplement_id": supplement_id,
                "return": f"my:open:{supplement_id}",
            }
            await show(
                update,
                context,
                "Введи заметку (до 200 символов).",
                my_hub_back(supplement_id),
                route=f"my:setnote:{supplement_id}",
            )
            return

        if data.startswith("my:settime:"):
            supplement_id = int(data.split(":")[-1])
            context.user_data[AWAITING_INPUT_KEY] = {
                "kind": "time",
                "supplement_id": supplement_id,
                "return": f"my:open:{supplement_id}",
            }
            await show(
                update,
                context,
                "Введите время в формате ЧЧ:ММ, например 08:30",
                my_hub_back(supplement_id),
                route=f"my:settime:{supplement_id}",
            )
            return

        if data.startswith("my:cleartime:"):
            supplement_id = int(data.split(":")[-1])
            await db.set_user_schedule_time(conn, tg_uid, supplement_id, None)
            await _remove_reminder_job(context.application.job_queue, tg_uid, supplement_id)
            await _show_my_hub(update, context, conn, tg_uid, supplement_id)
            return

        if data.startswith("my:marktaken:"):
            supplement_id = int(data.split(":")[-1])
            tz = await db.get_user_tz(conn, tg_uid)
            local_date = db.get_user_local_date(tz)
            await db.set_today_intake_status(conn, tg_uid, supplement_id, local_date, "taken")
            await _show_my_hub(update, context, conn, tg_uid, supplement_id)
            return

        if data.startswith("my:markskipped:"):
            supplement_id = int(data.split(":")[-1])
            tz = await db.get_user_tz(conn, tg_uid)
            local_date = db.get_user_local_date(tz)
            await db.set_today_intake_status(conn, tg_uid, supplement_id, local_date, "skipped")
            await _show_my_hub(update, context, conn, tg_uid, supplement_id)
            return

        if data.startswith("my:markreset:"):
            supplement_id = int(data.split(":")[-1])
            tz = await db.get_user_tz(conn, tg_uid)
            local_date = db.get_user_local_date(tz)
            await db.set_today_intake_status(conn, tg_uid, supplement_id, local_date, None)
            await _show_my_hub(update, context, conn, tg_uid, supplement_id)
            return

        if data.startswith("my:debugrem:"):
            _, _, delay, supplement_id = data.split(":", 3)
            delay_seconds = int(delay)
            await schedule_debug_reminder(
                context.application.job_queue,
                tg_uid,
                int(supplement_id),
                delay_seconds,
            )
            await _show_my_hub(update, context, conn, tg_uid, int(supplement_id))
            return

        if data.startswith("supp:compat:"):
            _, _, slug, page = data.split(":", 3)
            row = await db.get_supplement_by_slug(conn, slug)
            if not row:
                await show(update, context, "Не нашёл в каталоге.", back(), route=f"supp:view:{slug}:{page}")
                return
            groups = await engine.describe_interactions(conn, row, limit_each=8)
            text = engine.format_interactions(row["name"], groups)
            await show(update, context, text, supplement_compat_back(slug, int(page)), route=f"supp:compat:{slug}:{page}")
            return

        if data == "menu:checkin_demo":
            tz = await db.get_user_tz(conn, tg_uid)
            today = db.get_user_local_date(tz)
            await db.upsert_checkin(conn, user_id, today, {
                "energy": 7, "sleep": 7, "mood": 7, "stress": 4, "training": 0, "note": "demo"
            })
            await show(update, context, "📝 Чекин записан (демо). Потом сделаем нормальный опрос.", back(), route="menu:checkin_demo")
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

async def on_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer()
    data = q.data or ""
    if data.startswith("tz:page:"):
        page = int(data.split(":")[-1])
        await _show_timezone_picker(update, context, page)
        return
    if data.startswith("tz:set:"):
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
        await home(update, context)

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
    if route == "my:search":
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
    if route.startswith("my:list:"):
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
    if route.startswith("my:open:"):
        supplement_id = int(route.split(":")[-1])
        await _show_my_hub(
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
        await _show_supplement_page(
            update,
            context,
            conn,
            tg_uid,
            slug,
            int(page),
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route.startswith("supp:compat:"):
        _, _, slug, page = route.split(":", 3)
        row = await db.get_supplement_by_slug(conn, slug)
        if not row:
            await home(update, context, target_chat_id=target_chat_id, target_message_id=target_message_id)
            return
        groups = await engine.describe_interactions(conn, row, limit_each=8)
        text = engine.format_interactions(row["name"], groups)
        await show(
            update,
            context,
            text,
            supplement_compat_back(slug, int(page)),
            route=route,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route.startswith("my:setdose:"):
        supplement_id = int(route.split(":")[-1])
        await show(
            update,
            context,
            "Введи дозу (до 200 символов).",
            my_hub_back(supplement_id),
            route=route,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route.startswith("my:setnote:"):
        supplement_id = int(route.split(":")[-1])
        await show(
            update,
            context,
            "Введи заметку (до 200 символов).",
            my_hub_back(supplement_id),
            route=route,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route.startswith("my:settime:"):
        supplement_id = int(route.split(":")[-1])
        await show(
            update,
            context,
            "Введите время в формате ЧЧ:ММ, например 08:30",
            my_hub_back(supplement_id),
            route=route,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    if route.startswith("tz:page:"):
        page = int(route.split(":")[-1])
        await _show_timezone_picker(
            update,
            context,
            page,
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
    if route == "menu:checkin_demo":
        await show(
            update,
            context,
            "📝 Чекин записан (демо). Потом сделаем нормальный опрос.",
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
    awaiting = context.user_data.get(AWAITING_INPUT_KEY)
    if awaiting:
        kind = awaiting.get("kind")
        supplement_id = int(awaiting["supplement_id"])
        if kind == "time":
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", txt):
                await show(
                    update,
                    context,
                    "Введите время в формате ЧЧ:ММ, например 08:30",
                    my_hub_back(supplement_id),
                    route=f"my:settime:{supplement_id}",
                )
            else:
                s = context.application.bot_data["settings"]
                tg_uid = update.effective_user.id
                conn = await db.connect(s.DB_PATH)
                try:
                    await db.set_user_schedule_time(conn, tg_uid, supplement_id, txt)
                    tz_name = await db.get_user_tz(conn, tg_uid)
                    if tz_name:
                        await _schedule_reminder_job(
                            context.application.job_queue,
                            tg_uid,
                            supplement_id,
                            txt,
                            tz_name,
                        )
                    context.user_data.pop(AWAITING_INPUT_KEY, None)
                    await _show_my_hub(update, context, conn, tg_uid, supplement_id)
                finally:
                    await conn.close()
        elif len(txt) > 200:
            prompt = "Введи дозу (до 200 символов)." if kind == "dose" else "Введи заметку (до 200 символов)."
            await show(
                update,
                context,
                f"Слишком длинный текст. {prompt}",
                my_hub_back(supplement_id),
                route=f"my:set{kind}:{supplement_id}",
            )
        else:
            s = context.application.bot_data["settings"]
            tg_uid = update.effective_user.id
            conn = await db.connect(s.DB_PATH)
            try:
                value = txt if txt else None
                if awaiting.get("kind") == "dose":
                    await db.set_user_supplement_dose(conn, tg_uid, supplement_id, value)
                else:
                    await db.set_user_supplement_note(conn, tg_uid, supplement_id, value)
                context.user_data.pop(AWAITING_INPUT_KEY, None)
                await _show_my_hub(update, context, conn, tg_uid, supplement_id)
            finally:
                await conn.close()

        try:
            await update.message.delete()
        except Exception:
            pass
        return

    waiting = context.user_data.get(WAITING_KEY)
    if not waiting:
        return

    s = context.application.bot_data["settings"]
    tg_uid = update.effective_user.id
    conn = await db.connect(s.DB_PATH)
    try:
        user_id = await db.get_or_create_user(conn, tg_uid, tz_name="")
        tz_name = await db.get_user_tz(conn, tg_uid)
        if not tz_name:
            await _show_timezone_picker(update, context, 0)
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
                        f"Цель: <b>{txt}</b>",
                        "",
                    ]
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
                elif raw and not format_error_shown:
                    await show(
                        update,
                        context,
                        "AI вернул неожиданный формат. Попробуй ещё раз.",
                        back(),
                        route="menu:ai",
                    )

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
    app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^(menu:|add_pick:|compat_pick:|cat:page:|supp:|my:)"))
    app.add_handler(CallbackQueryHandler(on_ai_add, pattern=r"^aiadd:"))
    app.add_handler(CallbackQueryHandler(on_timezone, pattern=r"^tz:"))
    app.add_handler(CallbackQueryHandler(on_reminder_action, pattern=r"^rem:"))
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
                route="my:list:0",
                target_chat_id=target_chat_id,
                target_message_id=target_message_id,
            )
        else:
            await show(
                update,
                context,
                "Пока список пуст. Добавь БАД из каталога.",
                my_supplements_list([], 0, False, False, show_clear=False),
                route="my:list:0",
                target_chat_id=target_chat_id,
                target_message_id=target_message_id,
            )
        return

    suffix = f" (стр. {page + 1})" if total > MY_PAGE_SIZE else ""
    title = "📦 <b>Мои БАДы</b>"
    if q:
        title = f"📦 <b>Мои БАДы</b>\nПоиск: <b>{q}</b>"
    await show(
        update,
        context,
        f"{title}{suffix}",
        my_supplements_list(rows, page, has_prev=page > 0, has_next=has_next, show_clear=bool(q)),
        route=f"my:list:{page}",
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )

async def _show_my_hub(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    tg_uid: int,
    supplement_id: int,
    *,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    user_id = await db.get_or_create_user(conn, tg_uid)
    tz = await db.get_user_tz(conn, tg_uid)
    local_date = db.get_user_local_date(tz)
    row = await db.get_user_supplement_with_status(conn, tg_uid, supplement_id, local_date)
    if not row:
        await show(
            update,
            context,
            "БАД не найден в твоём списке.",
            my_supplements_list([], 0, False, False, show_clear=False),
            route="my:list:0",
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
        )
        return
    text = engine.format_supplement_hub(
        row,
        row,
        schedule_time=row["time_hhmm"] if "time_hhmm" in row.keys() else None,
        today_status=row["today_status"] if "today_status" in row.keys() else None,
    )
    await show(
        update,
        context,
        text,
        my_supplement_hub(
            supplement_id,
            bool(row["is_active"]),
            bool(row["time_hhmm"]),
            row["today_status"],
        ),
        route=f"my:open:{supplement_id}",
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
    )

async def _show_supplement_page(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    tg_uid: int,
    slug: str,
    back_page: int,
    *,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    row = await db.get_supplement_by_slug(conn, slug)
    if not row:
        await show(update, context, "Не нашёл в каталоге.", back(), route=f"cat:page:{back_page}")
        return
    text = engine.format_supplement_details(row)
    in_stack = await db.user_has_supplement(conn, tg_uid, int(row["id"]))
    await show(
        update,
        context,
        text,
        supplement_page(int(row["id"]), slug, back_page, in_stack, row["purchase_url"]),
        route=f"supp:view:{slug}:{back_page}",
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
