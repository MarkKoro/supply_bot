from __future__ import annotations
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
)

from settings import get_settings
import db
import engine
from ux import show
from keyboards import main_menu, back, pick_search_results, my_supplements
from openai_client import OpenAIConfig, daily_advice

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("supply-bot")

WAITING_KEY = "waiting"

async def home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show(
        update, context,
        "💊 <b>Supply Bot</b>\n\n"
        "Я справочник + дневник добавок.\n"
        "— Совместимость из базы\n"
        "— AI-совет (опционально) с лимитом в день\n\n"
        "<i>Не медицинский совет.</i>",
        main_menu(),
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = context.application.bot_data["settings"]
    conn = await db.connect(s.DB_PATH)
    try:
        await db.get_or_create_user(conn, update.effective_user.id, tz_name=s.APP_TZ)
    finally:
        await conn.close()
    await home(update, context)

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
        user_id = await db.get_or_create_user(conn, tg_uid, tz_name=s.APP_TZ)

        if data == "menu:home":
            await home(update, context)
            return

        if data == "menu:add":
            context.user_data[WAITING_KEY] = "add_query"
            await show(update, context, "Введи название добавки (поиск по каталогу).\nНапример: <b>caffeine</b> или <b>magnesium</b>", back())
            return

        if data == "menu:mine":
            rows = await db.list_user_supplements(conn, user_id, limit=100)
            if not rows:
                await show(update, context, "Пока пусто. Нажми “Добавить добавку”.", back())
            else:
                await show(update, context, "📦 <b>Твои добавки</b> (нажми чтобы удалить):", my_supplements(rows))
            return

        if data.startswith("mine:del:"):
            slug = data.split(":")[-1]
            row = await db.get_supplement_by_slug(conn, slug)
            if row:
                await db.remove_user_supplement(conn, user_id, int(row["id"]))
            await show(update, context, "Удалено.", back())
            return

        if data == "menu:compat":
            context.user_data[WAITING_KEY] = "compat_query"
            await show(update, context, "Введи добавку, чтобы показать совместимость (синергии/антагонисты).", back())
            return

        if data.startswith("compat_pick:"):
            slug = data.split(":", 1)[1]
            row = await db.get_supplement_by_slug(conn, slug)
            if not row:
                await show(update, context, "Не нашёл в каталоге.", back())
                return
            groups = await engine.describe_interactions(conn, row, limit_each=8)
            text = engine.format_interactions(row["name"], groups)
            await show(update, context, text, back())
            return

        if data.startswith("add_pick:"):
            slug = data.split(":", 1)[1]
            row = await db.get_supplement_by_slug(conn, slug)
            if row:
                await db.add_user_supplement(conn, user_id, int(row["id"]))
                await show(update, context, f"✅ Добавлено: <b>{row['name']}</b>", back())
            else:
                await show(update, context, "Не нашёл в каталоге.", back())
            return

        if data == "menu:checkin_demo":
            tz = await db.get_user_tz(conn, user_id)
            today = datetime.now(ZoneInfo(tz)).date().isoformat()
            await db.upsert_checkin(conn, user_id, today, {
                "energy": 7, "sleep": 7, "mood": 7, "stress": 4, "training": 0, "note": "demo"
            })
            await show(update, context, "📝 Чекин записан (демо). Потом сделаем нормальный опрос.", back())
            return

        if data == "menu:ai":
            if not s.OPENAI_API_KEY:
                await show(update, context, "AI отключен: добавь OPENAI_API_KEY в .env.", back())
                return

            tz = await db.get_user_tz(conn, user_id)
            today = datetime.now(ZoneInfo(tz)).date().isoformat()
            can = await db.ai_can_use_today(conn, user_id, today, s.AI_MAX_PER_DAY)
            if not can:
                await show(update, context, f"Лимит AI на сегодня исчерпан (max={s.AI_MAX_PER_DAY}/день).", back())
                return

            sups = await db.list_user_supplements(conn, user_id, limit=100)
            checkins = await db.list_checkins(conn, user_id, limit=7)

            user_supp_text = ", ".join([r["name"] for r in sups]) or "нет"
            chk = []
            for c in reversed(checkins):
                chk.append(f"{c['date']}: energy={c['energy']} sleep={c['sleep']} mood={c['mood']} stress={c['stress']} training={c['training']} note={c['note']}")
            checkins_text = "\n".join(chk) or "нет"

            interactions_hint = "Подсказки по конфликтам/синергиям по набору добавим позже (агрегация по твоему стэку)."

            cfg = OpenAIConfig(api_key=s.OPENAI_API_KEY, model=s.OPENAI_MODEL)
            await show(update, context, "🤖 Думаю… (AI)", back())

            try:
                advice = await daily_advice(
                    cfg,
                    user_summary="Пользователь ведёт дневник добавок в Telegram.",
                    checkins_text=checkins_text,
                    user_supplements=user_supp_text,
                    interactions_hint=interactions_hint,
                )
            except Exception:
                log.exception("OpenAI request failed")
                await show(update, context, "AI временно недоступен.", back())
                return

            if not advice or not advice.strip():
                await show(update, context, "AI не вернул ответ. Попробуй позже.", back())
                return

            await db.ai_inc_today(conn, user_id, today)
            await show(update, context, f"🤖 <b>AI-совет</b>\n\n{advice}", back())
            return

    finally:
        await conn.close()

    await home(update, context)

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    txt = update.message.text.strip()
    waiting = context.user_data.get(WAITING_KEY)
    if not waiting:
        return

    s = context.application.bot_data["settings"]
    tg_uid = update.effective_user.id
    conn = await db.connect(s.DB_PATH)
    try:
        user_id = await db.get_or_create_user(conn, tg_uid, tz_name=s.APP_TZ)

        if waiting == "add_query":
            rows = await db.search_supplements(conn, txt, limit=10)
            if not rows:
                await show(update, context, "Не нашёл в каталоге.", back())
            else:
                await show(update, context, "Выбери добавку для добавления:", pick_search_results(rows, "add_pick"))
            context.user_data.pop(WAITING_KEY, None)

        elif waiting == "compat_query":
            rows = await db.search_supplements(conn, txt, limit=10)
            if not rows:
                await show(update, context, "Не нашёл в каталоге.", back())
            else:
                await show(update, context, "Выбери добавку:", pick_search_results(rows, "compat_pick"))
            context.user_data.pop(WAITING_KEY, None)

    finally:
        await conn.close()

    try:
        await update.message.delete()
    except Exception:
        pass

async def ai_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = context.application.bot_data["settings"]
    tg_uid = update.effective_user.id
    if tg_uid not in s.ADMIN_IDS:
        await show(update, context, "Недостаточно прав.")
        return

    conn = await db.connect(s.DB_PATH)
    try:
        user_id = await db.get_or_create_user(conn, tg_uid, tz_name=s.APP_TZ)
        args = context.args or []
        if args and args[0].lower() == "all":
            await db.ai_reset_all_usage(conn)
            await show(update, context, "AI-лимиты сброшены для всех пользователей.")
            return

        tz = await db.get_user_tz(conn, user_id)
        today = datetime.now(ZoneInfo(tz)).date().isoformat()
        await db.ai_reset_user_today(conn, user_id, today)
        await show(update, context, f"AI-лимит сброшен на сегодня ({today}).")
    finally:
        await conn.close()

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error while processing update: %s", update, exc_info=context.error)

def build_app() -> Application:
    s = get_settings()
    app = Application.builder().token(s.TG_TOKEN).build()
    app.bot_data["settings"] = s

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ai_reset", ai_reset))
    app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^(menu:|add_pick:|compat_pick:|mine:del:)"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    return app

def main() -> None:
    app = build_app()
    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
