from __future__ import annotations
import asyncio
import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
)

from settings import get_settings
import db
import engine
import seed_import
from ux import show
from keyboards import (
    main_menu,
    back,
    pick_search_results,
    my_supplements,
    catalog_page,
    supplement_page,
    supplement_compat_back,
)
from openai_client import OpenAIConfig, daily_advice

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("supply-bot")

WAITING_KEY = "waiting"
CATALOG_PAGE_SIZE = 8

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

        if data == "menu:catalog":
            await _show_catalog_page(update, context, conn, 0)
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

        if data == "mine:check":
            rows = await db.list_user_supplements(conn, user_id, limit=100)
            if len(rows) < 2:
                await show(update, context, "Добавь минимум 2 добавки для проверки стека.", back())
                return
            items = await engine.stack_compatibility(conn, rows, limit=30)
            text = engine.format_stack_compatibility(items)
            await show(update, context, text, back())
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

        if data.startswith("cat:page:"):
            page = int(data.split(":")[-1])
            await _show_catalog_page(update, context, conn, page)
            return

        if data.startswith("supp:view:"):
            _, _, slug, page = data.split(":", 3)
            await _show_supplement_page(update, context, conn, user_id, slug, int(page))
            return

        if data.startswith("supp:add:"):
            _, _, slug, page = data.split(":", 3)
            row = await db.get_supplement_by_slug(conn, slug)
            if row:
                await db.add_user_supplement(conn, user_id, int(row["id"]))
            await _show_supplement_page(update, context, conn, user_id, slug, int(page))
            return

        if data.startswith("supp:del:"):
            _, _, slug, page = data.split(":", 3)
            row = await db.get_supplement_by_slug(conn, slug)
            if row:
                await db.remove_user_supplement(conn, user_id, int(row["id"]))
            await _show_supplement_page(update, context, conn, user_id, slug, int(page))
            return

        if data.startswith("supp:compat:"):
            _, _, slug, page = data.split(":", 3)
            row = await db.get_supplement_by_slug(conn, slug)
            if not row:
                await show(update, context, "Не нашёл в каталоге.", back())
                return
            groups = await engine.describe_interactions(conn, row, limit_each=8)
            text = engine.format_interactions(row["name"], groups)
            await show(update, context, text, supplement_compat_back(slug, int(page)))
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

    seed_paths = [s.SEED_SUPPLEMENTS_PATH, s.SEED_INTERACTIONS_PATH]
    missing = [path for path in seed_paths if not os.path.exists(path)]
    if missing:
        log.warning("Seed files missing, skipping auto-seed: %s", ", ".join(missing))
        return

    await seed_import._import(s.SEED_SUPPLEMENTS_PATH, s.SEED_INTERACTIONS_PATH)
    seeded = await _supplements_count(s.DB_PATH)
    log.info("Seeded OK: %s supplements", seeded)

async def _bootstrap() -> Application:
    s = get_settings()
    await db.init_db(s.DB_PATH)
    await _maybe_seed_catalog(s)

    app = Application.builder().token(s.TG_TOKEN).build()
    app.bot_data["settings"] = s

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ai_reset", ai_reset))
    app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^(menu:|add_pick:|compat_pick:|mine:|cat:page:|supp:)"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    return app

def build_app() -> Application:
    return asyncio.run(_bootstrap())

async def _show_catalog_page(update: Update, context: ContextTypes.DEFAULT_TYPE, conn, page: int) -> None:
    page = max(page, 0)
    offset = page * CATALOG_PAGE_SIZE
    rows = await db.list_supplements_page(conn, offset=offset, limit=CATALOG_PAGE_SIZE + 1)
    has_next = len(rows) > CATALOG_PAGE_SIZE
    display_rows = rows[:CATALOG_PAGE_SIZE]
    if not display_rows:
        await show(update, context, "Каталог пуст. Похоже, сиды ещё не импортированы…", back())
        return
    text = f"📚 <b>Каталог</b> (стр. {page + 1})"
    await show(
        update,
        context,
        text,
        catalog_page(display_rows, page, has_prev=page > 0, has_next=has_next),
    )

async def _show_supplement_page(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    user_id: int,
    slug: str,
    back_page: int,
) -> None:
    row = await db.get_supplement_by_slug(conn, slug)
    if not row:
        await show(update, context, "Не нашёл в каталоге.", back())
        return
    text = engine.format_supplement_details(row)
    in_stack = await db.user_has_supplement(conn, user_id, int(row["id"]))
    await show(
        update,
        context,
        text,
        supplement_page(slug, back_page, in_stack, row["purchase_url"]),
    )

def _ensure_event_loop() -> None:
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)


def main() -> None:
    _ensure_event_loop()
    app = build_app()
    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
