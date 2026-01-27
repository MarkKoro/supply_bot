from __future__ import annotations
from telegram import Update, InlineKeyboardMarkup, Message
from telegram.error import BadRequest
from telegram.ext import ContextTypes

import db

_last_screen: dict[int, int] = {}

def _payload_from_route(route: str) -> dict | None:
    if route == "menu:home":
        return {}
    if route == "menu:settings":
        return {}
    if route.startswith("cat:page:"):
        return {"page": int(route.split(":")[-1])}
    if route.startswith("my:list:"):
        return {"page": int(route.split(":")[-1])}
    if route.startswith("my:open:"):
        return {"supplement_id": int(route.split(":")[-1])}
    if route.startswith("supp:view:"):
        _, _, slug, page = route.split(":", 3)
        return {"slug": slug, "page": int(page)}
    if route.startswith("supp:open:"):
        return {"supplement_id": int(route.split(":")[-1])}
    if route.startswith("supp:compat:"):
        _, _, sid, kind, page = route.split(":", 4)
        return {"supplement_id": int(sid), "kind": kind, "page": int(page)}
    if route.startswith("my:setdose:") or route.startswith("my:setnote:") or route.startswith("my:settime:"):
        return {"supplement_id": int(route.split(":")[-1])}
    if route == "menu:curated":
        return {"page": 0}
    if route.startswith("curated:page:"):
        return {"page": int(route.split(":")[-1])}
    if route.startswith("curated:open:"):
        return {"slug": route.split(":")[-1]}
    if route.startswith("curated:add:"):
        return {"slug": route.split(":")[-1]}
    if route.startswith("tz:page:"):
        _, _, mode, page = route.split(":", 3)
        return {"mode": mode, "page": int(page)}
    if route.startswith("add_pick:"):
        return {"slug": route.split(":", 1)[1]}
    if route.startswith("compat_pick:"):
        return {"slug": route.split(":", 1)[1]}
    return None

def _set_last_menu(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    route: str | None,
    payload: dict | None,
    chat_id: int,
    message_id: int,
) -> None:
    if route is None or not isinstance(context.user_data, dict):
        return
    context.user_data["last_route"] = route
    context.user_data["last_payload"] = payload if payload is not None else _payload_from_route(route)
    context.user_data["last_menu_message_id"] = message_id
    context.user_data["last_menu_chat_id"] = chat_id
    context.user_data["last_menu"] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "route": route,
        "payload": context.user_data.get("last_payload"),
    }

async def show(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    kb: InlineKeyboardMarkup | None = None,
    *,
    parse_mode: str = "HTML",
    route: str | None = None,
    payload: dict | None = None,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> Message:
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is None:
        raise RuntimeError("No chat_id")

    target_chat = target_chat_id or chat_id
    target_message = target_message_id

    if target_message is not None:
        try:
            msg = await context.bot.edit_message_text(
                chat_id=target_chat,
                message_id=target_message,
                text=text,
                reply_markup=kb,
                parse_mode=parse_mode,
                disable_web_page_preview=True,
            )
            _last_screen[target_chat] = target_message
            _set_last_menu(context, route=route, payload=payload, chat_id=target_chat, message_id=target_message)
            return msg
        except BadRequest:
            pass

    q = update.callback_query
    if q and q.message and target_message is None:
        try:
            await q.message.edit_text(text=text, reply_markup=kb, parse_mode=parse_mode, disable_web_page_preview=True)
            _last_screen[chat_id] = q.message.message_id
            _set_last_menu(context, route=route, payload=payload, chat_id=chat_id, message_id=q.message.message_id)
            return q.message
        except BadRequest:
            pass

    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=kb,
        parse_mode=parse_mode,
        disable_web_page_preview=True,
    )
    prev = _last_screen.get(chat_id)
    _last_screen[chat_id] = msg.message_id
    _set_last_menu(context, route=route, payload=payload, chat_id=chat_id, message_id=msg.message_id)
    if prev and prev != msg.message_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=prev)
        except Exception:
            pass
    return msg

async def restore_last_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not isinstance(context.user_data, dict):
        return
    last_route = context.user_data.get("last_route")
    last_payload = context.user_data.get("last_payload")
    target_chat_id = context.user_data.get("last_menu_chat_id")
    target_message_id = context.user_data.get("last_menu_message_id")
    if not last_route:
        last_menu = context.user_data.get("last_menu") if isinstance(context.user_data.get("last_menu"), dict) else {}
        last_route = last_menu.get("route")
        last_payload = last_payload or last_menu.get("payload")
        target_chat_id = target_chat_id or last_menu.get("chat_id")
        target_message_id = target_message_id or last_menu.get("message_id")

    render_route = context.application.bot_data.get("render_route")
    if not callable(render_route):
        return

    tg_uid = update.effective_user.id if update.effective_user else None
    if tg_uid is None:
        return

    route = last_route or "menu:home"
    s = context.application.bot_data.get("settings")
    if not s:
        return

    conn = await db.connect(s.DB_PATH)
    try:
        await render_route(
            update,
            context,
            conn,
            int(tg_uid),
            route,
            target_chat_id,
            target_message_id,
            payload=last_payload,
        )
    finally:
        await conn.close()
