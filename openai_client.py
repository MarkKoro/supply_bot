from __future__ import annotations
import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("supply-bot")

@dataclass(frozen=True)
class OpenAIConfig:
    api_key: str
    model: str

@dataclass(frozen=True)
class OpenAITranscribeConfig:
    api_key: str
    model: str

def _sync_call(
    cfg: OpenAIConfig,
    *,
    instructions: str,
    input_text: str,
    max_output_tokens: int | None = None,
    timeout_s: float | None = None,
) -> str:
    from openai import OpenAI
    if timeout_s is not None:
        client = OpenAI(api_key=cfg.api_key, timeout=timeout_s)
    else:
        client = OpenAI(api_key=cfg.api_key)
    params = {
        "model": cfg.model,
        "instructions": instructions,
        "input": input_text,
    }
    if max_output_tokens is not None:
        params["max_output_tokens"] = max_output_tokens
    resp = client.responses.create(**params)
    return (resp.output_text or "").strip()

def _sync_transcribe_call(
    cfg: OpenAITranscribeConfig,
    *,
    audio_bytes: bytes,
    filename: str,
    language: str | None = None,
    timeout_s: float | None = None,
) -> str:
    from openai import OpenAI
    if timeout_s is not None:
        client = OpenAI(api_key=cfg.api_key, timeout=timeout_s)
    else:
        client = OpenAI(api_key=cfg.api_key)
    params = {
        "model": cfg.model,
        "file": (filename, audio_bytes, "audio/ogg"),
    }
    if language:
        params["language"] = language
    resp = client.audio.transcriptions.create(**params)
    return (getattr(resp, "text", "") or "").strip()

def _sync_call_messages(
    cfg: OpenAIConfig,
    *,
    messages: list[dict],
    max_output_tokens: int | None = None,
    timeout_s: float | None = None,
) -> str:
    from openai import OpenAI
    if timeout_s is not None:
        client = OpenAI(api_key=cfg.api_key, timeout=timeout_s)
    else:
        client = OpenAI(api_key=cfg.api_key)
    params = {
        "model": cfg.model,
        "input": messages,
    }
    if max_output_tokens is not None:
        params["max_output_tokens"] = max_output_tokens
    resp = client.responses.create(**params)
    return (resp.output_text or "").strip()

async def daily_advice(
    cfg: Optional[OpenAIConfig],
    *,
    user_summary: str,
    checkins_text: str,
    user_supplements: str,
    interactions_hint: str,
) -> str:
    if not cfg:
        return "AI отключен: добавь OPENAI_API_KEY в .env."

    instructions = (
        "Ты помощник по ведению дневника добавок. "
        "НЕ давай медицинских назначений и НЕ ставь диагнозов. "
        "Говори аккуратно и указывай, что любые изменения стоит обсудить с врачом. "
        "Не обещай чудес."
    )

    prompt = f"""ДАННЫЕ ПОЛЬЗОВАТЕЛЯ:
{user_summary}

ЧЕКИНЫ (последние):
{checkins_text}

ДОБАВКИ ПОЛЬЗОВАТЕЛЯ:
{user_supplements}

ПОДСКАЗКА ПО ВЗАИМОДЕЙСТВИЯМ (из базы):
{interactions_hint}

ЗАДАЧА:
1) 6–12 пунктов: что сегодня оставить/убрать/сдвинуть по времени (если уместно).
2) 3–6 гипотез “что могло повлиять” (стресс/сон/кофеин/добавки) — без категоричности.
3) Для каждой гипотезы: уверенность Low/Medium/High (НЕ проценты).
4) Короткий дисклеймер.

Язык: русский. Стиль: коротко, практично.
"""

    return await asyncio.to_thread(_sync_call, cfg, instructions=instructions, input_text=prompt)

async def suggest_stack_v1(
    cfg: Optional[OpenAIConfig],
    goal_text: str,
    user_supplements_text: str,
) -> str:
    if not cfg:
        return "AI отключен: добавь OPENAI_API_KEY в .env."

    instructions = (
        "Ты помощник по подбору добавок. "
        "НЕ назначай лечение и НЕ ставь диагнозы. "
        "Формат ответа: строго JSON, без Markdown и без лишнего текста. "
        "Поля *name_ru*, *why_ru*, *dose_ru*, *timing_ru*, *warnings_ru* — на русском. "
        "Вопросы пользователю задавать нельзя: поле *questions_ru* всегда возвращай пустым массивом []. "
        "Используй для *name_ru* самые распространённые русские названия БАДов, "
        "которые обычно встречаются в каталогах и ботах (без редких синонимов). "
        "Поле *name* оставляй латиницей/английским названием."
    )

    prompt = f"""ЦЕЛЬ ПОЛЬЗОВАТЕЛЯ:
{goal_text}

ТЕКУЩИЕ ДОБАВКИ ПОЛЬЗОВАТЕЛЯ:
{user_supplements_text}

ЗАДАЧА:
Сформируй список кандидатов для стека (обычно 8–15 пунктов, при широком запросе можно до 25).
Если пользователь явно просит "много", "максимум", "полный список" — верни максимально полный релевантный список (до 25).
Верни СТРОГО JSON по схеме:
{{
  "candidates": [
    {{
      "name": "L-theanine",
      "name_ru": "L-теанин",
      "why_ru": "кратко почему подходит",
      "dose_ru": "рекомендуемая доза текстом",
      "timing_ru": "утро/день/вечер/перед сном/с едой/натощак/по ситуации",
      "confidence": 0
    }}
  ],
  "warnings_ru": ["..."],
  "questions_ru": ["..."]
}}
Если данных мало — НЕ задавай вопросы, просто снизь уверенность и добавь предупреждения в warnings_ru. В confidence используй число 0–100.
"""

    return await asyncio.to_thread(
        _sync_call,
        cfg,
        instructions=instructions,
        input_text=prompt,
        max_output_tokens=900,
        timeout_s=20,
    )

async def transcribe_voice_v1(
    cfg: Optional[OpenAITranscribeConfig],
    audio_bytes: bytes,
    filename: str,
) -> str:
    if not cfg:
        return ""
    try:
        return await asyncio.to_thread(
            _sync_transcribe_call,
            cfg,
            audio_bytes=audio_bytes,
            filename=filename,
            language="ru",
            timeout_s=20,
        )
    except Exception:
        log.exception("OpenAI transcription failed")
        raise

async def _read_file_bytes(path: str) -> bytes:
    def _read() -> bytes:
        with open(path, "rb") as handle:
            return handle.read()
    return await asyncio.to_thread(_read)

async def transcribe_audio(
    cfg: Optional[OpenAITranscribeConfig],
    path: str,
) -> str:
    if not cfg:
        return ""
    audio_bytes = await _read_file_bytes(path)
    filename = os.path.basename(path)
    try:
        return await asyncio.to_thread(
            _sync_transcribe_call,
            cfg,
            audio_bytes=audio_bytes,
            filename=filename,
            language="ru",
            timeout_s=20,
        )
    except Exception:
        log.exception("OpenAI transcription failed")
        raise

def _parse_json_dict(raw: str) -> dict | None:
    if not raw:
        return None
    content = raw.strip()
    if content.startswith("```"):
        content = content.strip("`")
        content = content.replace("json", "", 1).strip()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None

async def extract_checkin_from_text(
    text_ru: str,
    cfg: Optional[OpenAIConfig] = None,
) -> dict:
    if not cfg:
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
        if not api_key:
            return {}
        cfg = OpenAIConfig(api_key=api_key, model=model)

    instructions = (
        "Ты извлекаешь структурированные поля чек-ина из русского текста. "
        "Верни только JSON без Markdown. "
        "Используй только сведения из текста, иначе null/unknown."
    )
    schema = (
        "{\n"
        '  "sleep_range": "4–5"|"5–6"|"6–7"|"7–8"|"8+"|"unknown",\n'
        '  "energy": 1|2|3|4|null,\n'
        '  "stress": 1|2|3|null,\n'
        '  "training_today": true|false|null,\n'
        '  "goals": ["focus","energy","sleep","gut","mood","recovery","calm","workout"],\n'
        '  "note_text": "краткое резюме по-русски до 250 символов"\n'
        "}\n"
    )
    mapping = (
        "КАРТА ЦЕЛЕЙ:\n"
        "- концентрация/фокус/учёба/работа -> focus\n"
        "- энергия/бодрость -> energy\n"
        "- сон/заснуть/выспаться -> sleep\n"
        "- жкт/живот/пищеварение -> gut\n"
        "- настроение/тревога/депресс -> mood или calm\n"
        "- восстановление -> recovery\n"
        "- тренировка/спорт -> workout\n"
    )
    prompt = "\n".join(
        [
            "ТЕКСТ:",
            text_ru.strip(),
            "",
            mapping,
            "",
            "СХЕМА (строго JSON):",
            schema,
        ]
    )
    raw = await asyncio.to_thread(
        _sync_call,
        cfg,
        instructions=instructions,
        input_text=prompt,
        max_output_tokens=400,
        timeout_s=20,
    )
    parsed = _parse_json_dict(raw)
    return parsed or {}

async def checkin_plan_v1(
    cfg: Optional[OpenAIConfig],
    checkin_text: str,
    context_json: str,
) -> str:
    if not cfg:
        return "AI отключен: добавь OPENAI_API_KEY в .env."

    instructions = (
        "Ты помощник по ведению дневника добавок. "
        "НЕ ставь диагнозы, НЕ давай медицинских назначений и не делай медицинских утверждений. "
        "Будь консервативен в изменениях. "
        "Если симптомы серьезные, предложи обратиться к врачу. "
        "Формат ответа: строго JSON, без Markdown и без лишнего текста. "
        "Все текстовые поля на русском. "
        "supplement_name указывай латиницей/английским названием."
    )

    prompt = f"""ЧЕКИН ПОЛЬЗОВАТЕЛЯ:
{checkin_text}

КОНТЕКСТ (JSON):
{context_json}

ЗАДАЧА:
Сформируй краткий план изменений только на сегодня.
Строго соблюдай JSON-схему:
{{
  "summary_ru": "коротко что заметил",
  "changes": [
    {{
      "type": "add" | "remove" | "adjust_dose" | "adjust_timing",
      "supplement_name": "string (latin name)",
      "reason_ru": "почему",
      "confidence": 0,
      "dose_ru": "если применимо",
      "timing_ru": "если применимо (утро/день/вечер/перед сном/с едой/натощак)"
    }}
  ],
  "warnings_ru": ["..."],
  "questions_ru": ["..."]
}}
Верни ТОЛЬКО JSON. Если данных мало — добавь вопросы в questions_ru.
"""

    return await asyncio.to_thread(
        _sync_call,
        cfg,
        instructions=instructions,
        input_text=prompt,
        max_output_tokens=900,
        timeout_s=20,
    )

async def ai_checkin_json(
    cfg: Optional[OpenAIConfig],
    messages: list[dict],
    *,
    max_output_tokens: int = 900,
    timeout_s: float = 30,
) -> str:
    if not cfg:
        return "AI отключен: добавь OPENAI_API_KEY в .env."
    return await asyncio.to_thread(
        _sync_call_messages,
        cfg,
        messages=messages,
        max_output_tokens=max_output_tokens,
        timeout_s=timeout_s,
    )
