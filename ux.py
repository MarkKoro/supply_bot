from __future__ import annotations
from telegram import Update, InlineKeyboardMarkup, Message
from telegram.error import BadRequest
from telegram.ext import ContextTypes

_last_screen: dict[int, int] = {}

async def show(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    kb: InlineKeyboardMarkup | None = None,
    *,
    parse_mode: str = "HTML",
) -> Message:
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is None:
        raise RuntimeError("No chat_id")

    q = update.callback_query
    if q and q.message:
        try:
            await q.message.edit_text(text=text, reply_markup=kb, parse_mode=parse_mode, disable_web_page_preview=True)
            _last_screen[chat_id] = q.message.message_id
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
    if prev and prev != msg.message_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=prev)
        except Exception:
            pass
    return msg
