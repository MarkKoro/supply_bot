from __future__ import annotations
import asyncio
import logging
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
    timeout_s: float | None = None,
) -> str:
    from openai import OpenAI
    if timeout_s is not None:
        client = OpenAI(api_key=cfg.api_key, timeout=timeout_s)
    else:
        client = OpenAI(api_key=cfg.api_key)
    resp = client.audio.transcriptions.create(
        model=cfg.model,
        file=(filename, audio_bytes, "audio/ogg"),
    )
    return (getattr(resp, "text", "") or "").strip()

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
        "Поля *why_ru*, *dose_ru*, *timing_ru*, *warnings_ru*, *questions_ru* — на русском. "
        "Названия добавок в поле *name* — только латиницей/английские."
    )

    prompt = f"""ЦЕЛЬ ПОЛЬЗОВАТЕЛЯ:
{goal_text}

ТЕКУЩИЕ ДОБАВКИ ПОЛЬЗОВАТЕЛЯ:
{user_supplements_text}

ЗАДАЧА:
Сформируй список кандидатов для стека (5–8 пунктов), если уместно.
Верни СТРОГО JSON по схеме:
{{
  "candidates": [
    {{
      "name": "L-theanine",
      "why_ru": "кратко почему подходит",
      "dose_ru": "рекомендуемая доза текстом",
      "timing_ru": "утро/день/вечер/перед сном/с едой/натощак/по ситуации",
      "confidence": 0
    }}
  ],
  "warnings_ru": ["..."],
  "questions_ru": ["..."]
}}
Если данных мало — задавай уточняющие вопросы в questions_ru. В confidence используй число 0–100.
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
            timeout_s=20,
        )
    except Exception:
        log.exception("OpenAI transcription failed")
        raise
