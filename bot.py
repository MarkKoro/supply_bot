from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

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
    supp_tabs_kb,
    my_supplement_hub,
    my_supplement_reminders,
    timezone_picker,
    settings_menu,
    ai_checkin_entry_kb,
    ai_checkin_voice_kb,
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

AI_CHECKIN_ROUTE = "menu:checkin_ai"
AI_CHECKIN_STATE = "ai_checkin_state"
AI_CHECKIN_NOTE_WAIT = "ai_checkin_note"
AI_CHECKIN_VOICE_ROUTE = "checkin_voice"

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

CHECKIN_VOICE_MAX_SECONDS = 90
CHECKIN_VOICE_MAX_BYTES = 10 * 1024 * 1024

def _is_tz_unset(tz_name: str | None) -> bool:
    return not tz_name or str(tz_name).strip().upper() in {"UTC", "GMT"}

async def _download_voice_bytes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[bytes, str]:
    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)
    data = await file.download_as_bytearray()
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

def _format_link_names(items: list[dict]) -> str:
    if not items:
        return "Пока нет данных."
    labels = []
    for item in items:
        label = item.get("name_ru") or item.get("name_en") or "Без названия"
        labels.append(f"• {label}")
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
    return f"📝 <b>Фраза дня</b>\n\nШаг {step}/5"

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

def _ai_checkin_voice_payload() -> dict:
    return {"note_pending": True, "return_screen": "main", "date_local": ""}

async def _render_ai_checkin_entry(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    text = "📝 <b>Фраза дня</b>\n\nКак хочешь пройти опрос?"
    await show(
        update,
        context,
        text,
        ai_checkin_entry_kb(),
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

def build_ai_checkin_prompt(user: dict, stack: list[dict], checkin: dict, candidates: list[dict]) -> list[dict]:
    system_msg = (
        "Ты — осторожный помощник по добавкам. "
        "Никаких диагнозов и назначений. "
        "Рекомендации только по предоставленным данным. "
        "Нельзя предлагать добавки вне списка кандидатов и текущего стека. "
        "Не предлагай рецептурные препараты. "
        "Будь консервативным, максимум 6 действий. "
        "Если уверенность низкая — явно скажи. "
        "Верни строго JSON без Markdown."
    )
    def _val_or_unknown(value: object, label: str = "не указано") -> str:
        return label if value is None or value == "" else str(value)

    training_value = checkin.get("training")
    if training_value is None:
        training_text = "не указано"
    else:
        training_text = "да" if training_value else "нет"

    checkin_lines = [
        f"Дата: {checkin.get('date_local')}",
        f"Сон: {_val_or_unknown(checkin.get('sleep_range'), 'unknown')}",
        f"Энергия (1-4): {_val_or_unknown(checkin.get('energy'))}",
        f"Стресс (1-3): {_val_or_unknown(checkin.get('stress'))}",
        f"Тренировка: {training_text}",
        f"Цели: {', '.join(checkin.get('goals_ru') or []) or 'не указаны'}",
    ]
    note_text = str(checkin.get("note") or "").strip()
    if note_text:
        checkin_lines.append(f"Заметка: {note_text}")
    stack_lines = []
    for item in stack:
        base = f"- {item.get('slug')} / {item.get('ru_name')}"
        dose = item.get("dose_text")
        time_local = item.get("time_local")
        bits = []
        if dose:
            bits.append(f"доза: {dose}")
        if time_local:
            bits.append(f"время: {time_local}")
        if bits:
            base = f"{base} ({', '.join(bits)})"
        stack_lines.append(base)
    if not stack_lines:
        stack_lines = ["— нет"]

    cand_lines = []
    for cand in candidates:
        hint = cand.get("hint")
        line = f"- {cand.get('slug')} / {cand.get('ru_name')}"
        if hint:
            line = f"{line} — {hint}"
        cand_lines.append(line)
    if not cand_lines:
        cand_lines = ["— нет"]

    schema = (
        "{\n"
        '  "actions": [\n'
        "    {\n"
        '      "id": "a1",\n'
        '      "type": "add" | "remove" | "adjust",\n'
        '      "slug": "caffeine",\n'
        '      "dose_text": "200 mg",\n'
        '      "time_hint": "утро",\n'
        '      "confidence": 0.0,\n'
        '      "reason_short_ru": "коротко почему",\n'
        '      "safety_ru": "короткая фраза о рисках"\n'
        "    }\n"
        "  ],\n"
        '  "summary_ru": "резюме на 2-4 строки",\n'
        '  "disclaimer_ru": "Не медсовет..."\n'
        "}"
    )

    user_msg = "\n".join(
        [
            "ПОЛЬЗОВАТЕЛЬ:",
            f"- tz: {user.get('tz_name')}",
            "",
            "ЧЕКИН:",
            *checkin_lines,
            "",
            "ТЕКУЩИЙ СТЕК:",
            *stack_lines,
            "",
            "КАНДИДАТЫ ИЗ КАТАЛОГА:",
            *cand_lines,
            "",
            "ФОРМАТ ОТВЕТА (строго JSON):",
            schema,
        ]
    )
    return [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_msg},
    ]

def _parse_ai_json(raw: str) -> dict | None:
    if not raw:
        return None
    content = raw.strip()
    if content.startswith("```"):
        content = content.strip("`")
        content = content.replace("json", "", 1).strip()
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None

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
    lines = [
        f"🤖 AI-рекомендации на сегодня ({date_local})",
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
            time_hint = str(action.get("time_hint") or "").strip()
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
            [InlineKeyboardButton("✅ Применить всё на сегодня", callback_data="aicheck:apply:all")],
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
    lines = [f"🎯 Выбор действий ({date_local})", ""]
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
                "Начальный подбор вернул неожиданный формат. Попробуй ещё раз.",
                back(),
                route="menu:ai",
            )
            format_error_shown = True
    else:
        await show(update, context, "Начальный подбор не вернул ответ. Попробуй ещё раз.", back(), route="menu:ai")

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
            "✨ <b>Начальный подбор</b>",
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
            "Начальный подбор вернул неожиданный формат. Попробуй ещё раз.",
            back(),
            route="menu:ai",
        )
    return False

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
    day = await db.ensure_daily_rollover(conn, tg_uid)
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
    }
    await db.upsert_daily_checkin(
        conn,
        user_id=user_id,
        date_local=day,
        tz_name=tz_name,
        answers={
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
    candidates = await db.get_catalog_candidates(conn, goals, limit=60)
    messages = build_ai_checkin_prompt(
        {"tz_name": tz_name},
        stack,
        checkin_payload,
        candidates,
    )
    cfg = OpenAIConfig(api_key=s.OPENAI_API_KEY, model=s.OPENAI_MODEL)
    await show(
        update,
        context,
        "🤖 Думаю… (AI)",
        back(),
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

    allowed_slugs = {item.get("slug") for item in candidates} | {item.get("slug") for item in stack}
    allowed_slugs = {slug for slug in allowed_slugs if slug}
    actions = data.get("actions") if isinstance(data.get("actions"), list) else []
    clean_actions = []
    missing_count = 0
    for idx, action in enumerate(actions, start=1):
        if len(clean_actions) >= 6:
            break
        if not isinstance(action, dict):
            continue
        action_type = str(action.get("type") or "").strip()
        if action_type not in {"add", "remove", "adjust"}:
            continue
        slug = str(action.get("slug") or "").strip()
        if not slug or (allowed_slugs and slug not in allowed_slugs):
            missing_count += 1
            continue
        row = await db.get_supplement_by_slug(conn, slug)
        if not row:
            missing_count += 1
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
                "time_hint": _truncate_text(action.get("time_hint"), 60),
                "confidence": confidence,
                "reason_short_ru": _truncate_text(action.get("reason_short_ru"), 200),
                "safety_ru": _truncate_text(action.get("safety_ru"), 160),
            }
        )

    payload = {
        "actions": clean_actions,
        "summary_ru": _truncate_text(data.get("summary_ru"), 600) or "Короткое резюме недоступно.",
        "disclaimer_ru": _truncate_text(data.get("disclaimer_ru"), 300),
    }
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
        "— Начальный подбор (опционально)\n\n"
        "<i>Не медицинский совет.</i>"
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
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
) -> None:
    context.user_data[WAITING_KEY] = "initial_pick_goal"
    await show(
        update,
        context,
        _initial_pick_prompt_text(prefix),
        back(),
        route="menu:ai",
        target_chat_id=target_chat_id,
        target_message_id=target_message_id,
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
                    "Пример: Москва — GMT+03."
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
        batches: dict[tuple[int, str, str], list[int]] = {}
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
            batches.setdefault((tg_user_id, time_local, day), []).append(supplement_id)
        for (tg_user_id, _time_local, _day), supplement_ids in batches.items():
            await reminder_fire_batch(context, tg_user_id, supplement_ids)
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
    supplement_ids = data.get("supplement_ids")
    if isinstance(supplement_ids, list) and supplement_ids:
        await reminder_fire_batch(context, tg_user_id, supplement_ids, is_snooze=True)
        return
    supplement_id = int(data.get("supplement_id"))
    await reminder_fire(context, tg_user_id, supplement_id, is_snooze=True)

def _reminder_batch_actions() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Выпил", callback_data="rem:done_all"),
                InlineKeyboardButton("❌ Не выпил", callback_data="rem:skip_all"),
            ],
            [
                InlineKeyboardButton("⏱ 15 мин", callback_data="rem:snooze_all:15"),
                InlineKeyboardButton("⏱ 30 мин", callback_data="rem:snooze_all:30"),
                InlineKeyboardButton("⏱ 60 мин", callback_data="rem:snooze_all:60"),
            ],
        ]
    )

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
            """SELECT tg_user_id, tz_name, last_chat_id
               FROM users WHERE tg_user_id=?""",
            (tg_user_id,),
        )
        if not user_row:
            return
        tz_name = str(user_row["tz_name"] or "").strip()
        if _is_tz_unset(tz_name):
            return
        ui_message_id, ui_screen, ui_payload = await db.get_ui_state(conn, tg_user_id)
        if ui_message_id:
            last_chat_id = int(user_row["last_chat_id"] or 0) or tg_user_id
            try:
                await context.bot.delete_message(chat_id=last_chat_id, message_id=ui_message_id)
            except Exception:
                pass

        entries: list[dict] = []
        for supplement_id in supplement_ids:
            supplement = await db.get_supplement_by_id(conn, supplement_id)
            if not supplement:
                continue
            name_ru = (supplement["name_ru"] or supplement["name"] or "").strip()
            name_en = (supplement["name_en"] or "").strip()
            name_line = name_ru
            if name_en and name_en != name_ru:
                name_line = f"{name_line} ({name_en})"
            dose_text = "—"
            user_supp = await db.get_user_supplement(conn, tg_user_id, supplement_id)
            if user_supp and str(user_supp["dose_text"] or "").strip():
                dose_text = str(user_supp["dose_text"]).strip()
            entries.append({"id": supplement_id, "name": name_line, "dose": dose_text})

        if not entries:
            return

        lines = [
            "⏰ <b>Напоминание</b>",
            "Пора принять:",
            *[f"• <b>{item['name']}</b> — {item['dose']}" for item in entries],
        ]
        text = "\n".join(lines)
        target_chat_id = int(user_row["last_chat_id"] or 0) or tg_user_id
        msg = await context.bot.send_message(
            chat_id=target_chat_id,
            text=text,
            reply_markup=_reminder_batch_actions(),
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
        payload_data["reminder_batch_ids"] = [item["id"] for item in entries]
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
        await db.get_or_create_user(conn, tg_uid, tz_name="")
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
        if data == "settings:initial_pick":
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
                kb = InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("✅ Да, начать заново", callback_data="settings:initial_pick:confirm")],
                        [InlineKeyboardButton("⬅️ Назад", callback_data="menu:settings")],
                    ]
                )
                await show(
                    update,
                    context,
                    "Уверены, что хотите начать заново? "
                    "Предыдущий начальный подбор будет удалён.",
                    kb,
                    route="menu:settings",
                )
                return
            await _start_initial_pick(update, context)
            return
        if data == "settings:initial_pick:confirm":
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
            await _start_initial_pick(
                update,
                context,
                prefix="🔄 Начинаем новый подбор. Предыдущий список очищен.",
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
                    await db.add_user_supplement(conn, tg_uid, supplement_id)
                    await render_supplement_card(
                        update,
                        context,
                        conn,
                        supplement_id,
                        return_page,
                        tab=tab,
                        page=page,
                        show_added=True,
                        return_mode=payload_data.get("return_mode") or "catalog",
                        search_query=payload_data.get("query"),
                        prev_payload=prev_payload,
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
                    tab = payload_data.get("tab") or "brief"
                    page = int(payload_data.get("page") or 0)
                    prev_payload = payload_data.get("prev_payload") if payload_data.get("return_mode") == "supp_back" else None
                    await db.add_user_supplement(conn, tg_uid, supplement_id)
                    await render_supplement_card(
                        update,
                        context,
                        conn,
                        supplement_id,
                        return_page,
                        tab=tab,
                        page=page,
                        return_mode="search",
                        search_query=query or None,
                        show_added=True,
                        prev_payload=prev_payload,
                    )
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
                    await db.add_user_supplement(conn, tg_uid, supplement_id)
                    await render_supplement_card(
                        update,
                        context,
                        conn,
                        supplement_id,
                        return_page,
                        tab=tab,
                        page=page,
                        return_mode="supp_back",
                        prev_payload=prev_payload,
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

        if data.startswith("mysupp:quick:"):
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
                    f"Рекомендуемая доза: {recommended_dose}"
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

        if data.startswith("mysupp:rem:suggest:"):
            parts = data.split(":")
            if len(parts) >= 6:
                supplement_id = int(parts[3])
                time_local = parts[4].replace("-", ":")
                page = int(parts[5])
                await db.upsert_reminder(conn, tg_uid, supplement_id, time_local, enabled=1)
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
            context.user_data.pop(WAITING_KEY, None)
            await _render_ai_checkin_entry(update, context)
            return

        if data == "menu:todayplan":
            await _show_today_plan(update, context, conn, tg_uid)
            return

        if data == "menu:ai":
            if not s.OPENAI_API_KEY:
                await show(
                    update,
                    context,
                    "Начальный подбор недоступен: добавь OPENAI_API_KEY в .env.",
                    back(),
                    route="menu:ai",
                )
                return
            existing_ids = await db.get_user_initial_stack_ids(conn, tg_uid)
            if existing_ids:
                kb = InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("✅ Да, начать заново", callback_data="settings:initial_pick:confirm")],
                        [InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")],
                    ]
                )
                await show(
                    update,
                    context,
                    "Уверены, что хотите начать заново? "
                    "Предыдущий начальный подбор будет удалён.",
                    kb,
                    route="menu:ai",
                )
                return
            await _start_initial_pick(update, context)
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
        await q.answer()

async def on_ai_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer()
    data = q.data or ""
    if not data.startswith("aicheck:"):
        return

    state = _get_ai_checkin_state(context)
    step = int(state.get("step") or 1)
    answers = state.get("answers", {}) if isinstance(state.get("answers"), dict) else {}

    if data == "aicheck:entry":
        context.user_data.pop(WAITING_KEY, None)
        await _render_ai_checkin_entry(update, context)
        return

    if data == "aicheck:voice":
        context.user_data.pop(WAITING_KEY, None)
        await _render_ai_checkin_voice_prompt(update, context)
        return

    if data == "aicheck:quick":
        context.user_data.pop(WAITING_KEY, None)
        state = {
            "step": 1,
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
                tz_name=tz_name or "GMT",
                state=state,
            )
        finally:
            await conn.close()
        return

    if data.startswith("aicheck:sleep:"):
        answers["sleep_range"] = data.split(":", 2)[2]
        state["answers"] = answers
        _set_ai_checkin_state(context, state)
        await _render_ai_checkin_wizard(update, context, state=state)
        return

    if data.startswith("aicheck:energy:"):
        try:
            answers["energy"] = int(data.split(":", 2)[2])
        except ValueError:
            return
        state["answers"] = answers
        _set_ai_checkin_state(context, state)
        await _render_ai_checkin_wizard(update, context, state=state)
        return

    if data.startswith("aicheck:stress:"):
        try:
            answers["stress"] = int(data.split(":", 2)[2])
        except ValueError:
            return
        state["answers"] = answers
        _set_ai_checkin_state(context, state)
        await _render_ai_checkin_wizard(update, context, state=state)
        return

    if data.startswith("aicheck:training:"):
        try:
            answers["training"] = bool(int(data.split(":", 2)[2]))
        except ValueError:
            return
        state["answers"] = answers
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
            await db.apply_day_plan_all(conn, user_id, date_local, payload)
        finally:
            await conn.close()
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📅 План на сегодня", callback_data="menu:todayplan")],
                [InlineKeyboardButton("🏠 Меню", callback_data="menu:home")],
            ]
        )
        await show(update, context, "Готово ✅ План на сегодня обновлён.", kb, route=AI_CHECKIN_ROUTE)
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
            await db.apply_day_plan_selective(conn, user_id, date_local, selected, payload)
        finally:
            await conn.close()
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📅 План на сегодня", callback_data="menu:todayplan")],
                [InlineKeyboardButton("🏠 Меню", callback_data="menu:home")],
            ]
        )
        await show(update, context, "Готово ✅ План на сегодня обновлён.", kb, route=AI_CHECKIN_ROUTE)
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
            context.user_data.pop("tz_back_data", None)
            onboarding_step = context.user_data.pop("onboarding", None)
            tzinfo = db.parse_utc_tz(tz_value)
            local_now = datetime.now(tzinfo).strftime("%H:%M")
            label = tz.format_confirmation_label(tz_value)
            if onboarding_step == "tz":
                existing_ids = await db.get_user_initial_stack_ids(conn, tg_uid)
                prefix = (
                    "✅ Часовой пояс сохранён: "
                    f"{label}\n"
                    f"Текущее время у тебя: {local_now}\n"
                    "Теперь сделаем начальный подбор."
                )
                if existing_ids:
                    kb = InlineKeyboardMarkup(
                        [
                            [InlineKeyboardButton("✅ Да, начать заново", callback_data="settings:initial_pick:confirm")],
                            [InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")],
                        ]
                    )
                    await show(
                        update,
                        context,
                        f"{prefix}\n\n"
                        "У вас уже есть начальный подбор. "
                        "При повторном запуске он будет удалён.",
                        kb,
                        route="menu:ai",
                    )
                else:
                    await _start_initial_pick(
                        update,
                        context,
                        prefix=prefix,
                    )
            else:
                await show(
                    update,
                    context,
                    "✅ Часовой пояс сохранён: "
                    f"{label}\n"
                    f"Текущее время у тебя: {local_now}",
                    main_menu(),
                    route="menu:home",
                )
        finally:
            await conn.close()

async def on_reminder_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer()
    data = q.data or ""
    parts = data.split(":")
    if len(parts) < 2:
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
    elif action in ("done_all", "skip_all"):
        supplement_id = None
    elif action == "snooze_all":
        if len(parts) >= 3:
            snooze_minutes = int(parts[2])
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

        if action == "done" and supplement_id is not None:
            await db.set_today_status(conn, tg_uid, supplement_id, "taken")
        elif action == "skip" and supplement_id is not None:
            await db.set_today_status(conn, tg_uid, supplement_id, "skipped")
        elif action == "done_all":
            for item_id in payload_data.get("reminder_batch_ids", []):
                await db.set_today_status(conn, tg_uid, int(item_id), "taken")
        elif action == "skip_all":
            for item_id in payload_data.get("reminder_batch_ids", []):
                await db.set_today_status(conn, tg_uid, int(item_id), "skipped")
        elif action in {"snooze", "snooze_all"}:
            minutes = snooze_minutes or 15
            guard = context.application.bot_data.setdefault("snooze_guard", {})
            key = f"{tg_uid}:{supplement_id or 'batch'}"
            now = datetime.utcnow()
            next_allowed = guard.get(key)
            if next_allowed and now < next_allowed:
                pass
            else:
                guard[key] = now + timedelta(minutes=minutes)
                snooze_name = f"snooze:{tg_uid}:{supplement_id or 'batch'}"
                for job in context.application.job_queue.get_jobs_by_name(snooze_name):
                    job.schedule_removal()
                if action == "snooze_all":
                    batch_ids = payload_data.get("reminder_batch_ids", [])
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

        if isinstance(payload_data, dict) and "reminder_msg_id" in payload_data:
            payload_data.pop("reminder_msg_id", None)
            payload_data.pop("reminder_batch_ids", None)
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
        if ui_screen == AI_CHECKIN_VOICE_ROUTE:
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
                sups = await db.list_user_supplements(conn, tg_uid, limit=100)
                user_supp_text = ", ".join([r["name"] for r in sups]) or "нет"
                cfg = OpenAIConfig(api_key=s.OPENAI_API_KEY, model=s.OPENAI_MODEL)
                await show(update, context, "🤖 Думаю…", back(), route="menu:ai")
                raw = None
                try:
                    raw = await suggest_stack_v1(cfg, txt, user_supp_text)
                except Exception:
                    log.exception("OpenAI request failed")
                    await show(update, context, "Начальный подбор временно недоступен.", back(), route="menu:ai")

                await _render_ai_suggestions(update, context, conn, txt, raw)
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

    try:
        await update.message.delete()
    except Exception:
        pass

async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
                "Слишком длинно. Запиши до 60 секунд, пожалуйста.",
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
            ai_checkin_voice_kb(),
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
                ai_checkin_voice_kb(),
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
                    tz_name=tz_name or "GMT",
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

        route = "menu:ai"
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
        sups = await db.list_user_supplements(conn, tg_uid, limit=100)
        user_supp_text = ", ".join([r["name"] for r in sups]) or "нет"
        cfg = OpenAIConfig(api_key=s.OPENAI_API_KEY, model=s.OPENAI_MODEL)
        await show(update, context, "🤖 Думаю…", back(), route="menu:ai")
        raw = None
        try:
            raw = await suggest_stack_v1(cfg, transcription, user_supp_text)
        except Exception:
            log.exception("OpenAI request failed")
            await show(update, context, "Начальный подбор временно недоступен.", back(), route="menu:ai")
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
        pattern=r"^(main$|menu:|settings:|add_pick:|compat_pick:|cat:|curated:|supp:|card:|mysupp:)",
    ))
    app.add_handler(CallbackQueryHandler(on_ai_add, pattern=r"^aiadd:"))
    app.add_handler(CallbackQueryHandler(on_ai_checkin, pattern=r"^aicheck:"))
    app.add_handler(CallbackQueryHandler(on_noop, pattern=r"^noop$"))
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
    if tab not in {"brief", "time", "dose", "synergy", "antag"}:
        tab = "brief"

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
    short_desc = (
        str(row.get("summary_ru") or "").strip()
        or str(row.get("summary_verified") or "").strip()
        or str(row.get("summary_theoretical") or "").strip()
        or "—"
    )

    dose_raw = row.get("dose_ru")
    dose_typical = ""
    dose_range = ""
    course_ru = ""
    dose_notes = ""
    timing_hint = ""
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
        else:
            dose_typical = dose_raw.strip()
    elif dose_raw:
        dose_typical = str(dose_raw).strip()

    timing_ru = str(row.get("timing_ru") or "").strip() or timing_hint
    with_food_ru = "—"
    timing_notes_ru = dose_notes or "—"
    if not timing_ru:
        timing_ru = "—"

    safety_ru = str(row.get("safety_ru") or "").strip() or "—"
    dose_typical = dose_typical or "—"
    dose_range = dose_range or "—"
    course_ru = course_ru or "—"

    lines: list[str] = []
    top_synergy = []
    top_antag = []
    if tab == "brief":
        links = await db.get_supplement_links(conn, supplement_id)
        top_limit = 5
        top_synergy = await db.list_linked_supplements(conn, links["synergy"][:top_limit])
        top_antag = await db.list_linked_supplements(conn, links["antagonists"][:top_limit])
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
                "Коротко:",
                short_desc,
                "",
                "Топ совместимости (с чем лучше всего сочетают):",
                _format_link_names(top_synergy),
                "",
                "Топ несовместимости (с чем лучше НЕ сочетать):",
                _format_link_names(top_antag),
                "",
                "Больше деталей — во вкладках 🤝 и ⚠️.",
            ]
        )
    elif tab == "time":
        lines = [
            f"<b>{title}</b> — 🕒 Когда принимать",
            f"Рекомендация: {timing_ru}",
            f"С едой/натощак: {with_food_ru}",
            f"Комментарии: {timing_notes_ru}",
        ]
    elif tab == "dose":
        lines = [
            f"<b>{title}</b> — 💊 Дозировка",
            f"Типичная доза: {dose_typical}",
            f"Диапазон: {dose_range}",
            f"Курс: {course_ru}",
            f"Предупреждения: {safety_ru}",
        ]
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
            list_lines.append(f"• {label}")
        if tab == "synergy":
            lines = [
                f"<b>{title}</b> — 🤝 Совместимость",
                "С чем чаще всего сочетают:",
            ]
            if list_lines:
                lines.extend(list_lines)
            else:
                lines.append("Пока нет данных. Добавим по мере наполнения базы.")
        else:
            lines = [
                f"<b>{title}</b> — ⚠️ Антагонисты",
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
    title = (
        "💊 <b>Мои БАДы</b>\n"
        "Нажми на добавку, чтобы открыть управление.\n"
        "✅ — выпил сегодня, ❌ — не выпил."
    )
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

def _parse_timing_hours(timing_text: str) -> list[int]:
    if not timing_text:
        return []
    ranges: list[tuple[int, int]] = []
    for match in re.finditer(r"с\s*(\d{1,2})(?::\d{2})?\s*до\s*(\d{1,2})(?::\d{2})?", timing_text):
        ranges.append((int(match.group(1)), int(match.group(2))))
    for match in re.finditer(r"(\d{1,2})(?::\d{2})?\s*[–-]\s*(\d{1,2})(?::\d{2})?", timing_text):
        ranges.append((int(match.group(1)), int(match.group(2))))
    hours: list[int] = []
    for start, end in ranges:
        if start < 0 or start > 23 or end < 0 or end > 23:
            continue
        if end < start:
            continue
        for hour in range(start, end + 1):
            if hour not in hours:
                hours.append(hour)
    return sorted(hours)

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
    supplement = await db.get_supplement_by_id(conn, supplement_id)
    if not supplement:
        await show(update, context, "БАД не найден.", my_list_back(), route="mysupp:list:0")
        return
    reminders = await db.list_reminders_for_user(conn, tg_uid)
    reminder_times = [str(r["time_local"]) for r in reminders if int(r["supplement_id"]) == supplement_id]
    name = (supplement["name_ru"] or supplement["name"] or "").strip()
    timing_text = str(supplement["timing_ru"] or "").strip()
    timing_label = timing_text or "—"
    suggested_hours = _parse_timing_hours(timing_text)
    suggested_times = [f"{hour:02d}:00" for hour in suggested_hours]
    title = f"⏰ <b>Напоминания</b>\n{name}"
    if reminder_times:
        lines = [
            title,
            "",
            "Здесь ты можешь поставить напоминание для этого БАДа.",
            f"Рекомендуемое время: {timing_label}",
            "Выбери время ниже или добавь своё.",
            "",
            "Текущие напоминания:",
            *[f"• {t}" for t in reminder_times],
        ]
    else:
        lines = [
            title,
            "",
            "Здесь ты можешь поставить напоминание для этого БАДа.",
            f"Рекомендуемое время: {timing_label}",
            "Выбери время ниже или добавь своё.",
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
            suggested_times,
            page,
        ),
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
