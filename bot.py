from __future__ import annotations
import asyncio
import json
import logging
import os
import re
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
    my_list_back,
    my_hub_back,
    pick_search_results,
    my_supplements_list,
    catalog_page,
    supplement_page,
    supplement_compat_back,
    my_supplement_hub,
)
from openai_client import OpenAIConfig, daily_advice

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("supply-bot")

WAITING_KEY = "waiting"
AWAITING_INPUT_KEY = "awaiting_input"
MY_SEARCH_KEY = "my_search_q"
CATALOG_PAGE_SIZE = 8
MY_PAGE_SIZE = 10

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

        if data == "menu:my":
            context.user_data.pop(MY_SEARCH_KEY, None)
            await _show_my_list(update, context, conn, tg_uid, 0)
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
                await db.add_user_supplement(conn, tg_uid, int(row["id"]))
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
            await _show_supplement_page(update, context, conn, tg_uid, slug, int(page))
            return

        if data.startswith("my:list:"):
            page = int(data.split(":")[-1])
            await _show_my_list(update, context, conn, tg_uid, page)
            return

        if data == "my:search":
            context.user_data[WAITING_KEY] = "my_search_query"
            await show(update, context, "Введи название БАДА для поиска в твоём списке.", my_list_back())
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
            await show(update, context, "Введи дозу (до 200 символов).", my_hub_back(supplement_id))
            return

        if data.startswith("my:setnote:"):
            supplement_id = int(data.split(":")[-1])
            context.user_data[AWAITING_INPUT_KEY] = {
                "kind": "note",
                "supplement_id": supplement_id,
                "return": f"my:open:{supplement_id}",
            }
            await show(update, context, "Введи заметку (до 200 символов).", my_hub_back(supplement_id))
            return

        if data.startswith("my:settime:"):
            supplement_id = int(data.split(":")[-1])
            context.user_data[AWAITING_INPUT_KEY] = {
                "kind": "time",
                "supplement_id": supplement_id,
                "return": f"my:open:{supplement_id}",
            }
            await show(update, context, "Введите время в формате ЧЧ:ММ, например 08:30", my_hub_back(supplement_id))
            return

        if data.startswith("my:cleartime:"):
            supplement_id = int(data.split(":")[-1])
            await db.set_user_schedule_time(conn, tg_uid, supplement_id, None)
            await _show_my_hub(update, context, conn, tg_uid, supplement_id)
            return

        if data.startswith("my:marktaken:"):
            supplement_id = int(data.split(":")[-1])
            tz = await db.get_user_tz(conn, user_id)
            local_date = db.get_user_local_date(tz)
            await db.set_today_intake_status(conn, tg_uid, supplement_id, local_date, "taken")
            await _show_my_hub(update, context, conn, tg_uid, supplement_id)
            return

        if data.startswith("my:markskipped:"):
            supplement_id = int(data.split(":")[-1])
            tz = await db.get_user_tz(conn, user_id)
            local_date = db.get_user_local_date(tz)
            await db.set_today_intake_status(conn, tg_uid, supplement_id, local_date, "skipped")
            await _show_my_hub(update, context, conn, tg_uid, supplement_id)
            return

        if data.startswith("my:markreset:"):
            supplement_id = int(data.split(":")[-1])
            tz = await db.get_user_tz(conn, user_id)
            local_date = db.get_user_local_date(tz)
            await db.set_today_intake_status(conn, tg_uid, supplement_id, local_date, None)
            await _show_my_hub(update, context, conn, tg_uid, supplement_id)
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

            sups = await db.list_user_supplements(conn, tg_uid, limit=100)
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
    awaiting = context.user_data.get(AWAITING_INPUT_KEY)
    if awaiting:
        kind = awaiting.get("kind")
        supplement_id = int(awaiting["supplement_id"])
        if kind == "time":
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", txt):
                await show(update, context, "Введите время в формате ЧЧ:ММ, например 08:30", my_hub_back(supplement_id))
            else:
                s = context.application.bot_data["settings"]
                tg_uid = update.effective_user.id
                conn = await db.connect(s.DB_PATH)
                try:
                    await db.set_user_schedule_time(conn, tg_uid, supplement_id, txt)
                    context.user_data.pop(AWAITING_INPUT_KEY, None)
                    await _show_my_hub(update, context, conn, tg_uid, supplement_id)
                finally:
                    await conn.close()
        elif len(txt) > 200:
            prompt = "Введи дозу (до 200 символов)." if kind == "dose" else "Введи заметку (до 200 символов)."
            await show(update, context, f"Слишком длинный текст. {prompt}", my_hub_back(supplement_id))
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

        elif waiting == "my_search_query":
            context.user_data.pop(WAITING_KEY, None)
            if txt:
                context.user_data[MY_SEARCH_KEY] = txt
                await _show_my_list(update, context, conn, tg_uid, 0, q=txt)
            else:
                context.user_data.pop(MY_SEARCH_KEY, None)
                await _show_my_list(update, context, conn, tg_uid, 0)

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

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ai_reset", ai_reset))
    app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^(menu:|add_pick:|compat_pick:|cat:page:|supp:|my:)"))
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

async def _show_my_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    tg_uid: int,
    page: int,
    q: str | None = None,
) -> None:
    if q is None:
        q = context.user_data.get(MY_SEARCH_KEY)
    user_id = await db.get_or_create_user(conn, tg_uid)
    tz = await db.get_user_tz(conn, user_id)
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
            await show(update, context, f"Ничего не найдено по запросу: <b>{q}</b>", my_supplements_list([], 0, False, False, show_clear=True))
        else:
            await show(update, context, "Пока список пуст. Добавь БАД из каталога.", my_supplements_list([], 0, False, False, show_clear=False))
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
    )

async def _show_my_hub(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    tg_uid: int,
    supplement_id: int,
) -> None:
    user_id = await db.get_or_create_user(conn, tg_uid)
    tz = await db.get_user_tz(conn, user_id)
    local_date = db.get_user_local_date(tz)
    row = await db.get_user_supplement_with_status(conn, tg_uid, supplement_id, local_date)
    if not row:
        await show(update, context, "БАД не найден в твоём списке.", my_supplements_list([], 0, False, False, show_clear=False))
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
    )

async def _show_supplement_page(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    tg_uid: int,
    slug: str,
    back_page: int,
) -> None:
    row = await db.get_supplement_by_slug(conn, slug)
    if not row:
        await show(update, context, "Не нашёл в каталоге.", back())
        return
    text = engine.format_supplement_details(row)
    in_stack = await db.user_has_supplement(conn, tg_uid, int(row["id"]))
    await show(
        update,
        context,
        text,
        supplement_page(int(row["id"]), slug, back_page, in_stack, row["purchase_url"]),
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
