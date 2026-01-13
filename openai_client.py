from __future__ import annotations
import asyncio
from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class OpenAIConfig:
    api_key: str
    model: str

def _sync_call(cfg: OpenAIConfig, *, instructions: str, input_text: str) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=cfg.api_key)
    resp = client.responses.create(
        model=cfg.model,
        instructions=instructions,
        input=input_text,
    )
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
