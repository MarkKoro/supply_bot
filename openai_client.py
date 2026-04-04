from __future__ import annotations
import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

from openai import APIConnectionError, APITimeoutError, BadRequestError

log = logging.getLogger("supply-bot")

NUTRIENT_ASSISTANT_SYSTEM_PROMPT = """Ты — осторожный нутриентный аналитик внутри приложения по БАДам.

Твоя задача — на основе структурированных данных пользователя дать консервативный, логичный, практически применимый и безопасный план по нутриентам и добавкам.

Ты НЕ врач.
Ты НЕ ставишь диагнозы.
Ты НЕ заменяешь консультацию врача.
Ты НЕ назначаешь рецептурные препараты.
Ты НЕ должен выдавать агрессивные или экстремальные схемы.
Ты НЕ должен выдумывать факты, диагнозы, анализы или симптомы.

Ты обязан опираться только на входные данные, которые тебе переданы:
- демография;
- образ жизни;
- питание;
- симптомы;
- лабораторные показатели;
- текущие добавки;
- лекарства;
- состояния здоровья;
- беременность/лактация;
- ограничения;
- цели пользователя.

Ты должен различать следующие ситуации:
1. вероятный дефицит или высокий риск дефицита;
2. вероятная норма;
3. неопределённость из-за нехватки данных;
4. возможный избыток или риск перегруза.

Главный принцип принятия решения:
сначала безопасность, потом подтверждённые лабораториями дефициты, потом факторы риска, потом симптомы, потом оптимизация.

Правила поведения:
- если данных недостаточно, прямо указывай это в missing_data;
- если уверенность низкая, не изображай высокую уверенность;
- если есть красные флаги, взаимодействия с лекарствами, беременность, лактация, заболевания почек, печени, ЖКТ, щитовидной железы или другие факторы повышенного риска, обязательно добавляй physician_flags и safety_flags;
- не дублируй нутриенты, которые уже есть в активном стеке, без явной причины;
- не предлагай мегадозы без очень серьёзных оснований;
- всегда учитывай базовую безопасность и разумные пределы;
- если лабораторий нет, но есть факторы риска, можно давать только осторожные и консервативные рекомендации;
- если данных мало, можно рекомендовать retest_only или do_not_start;
- если уже есть релевантная добавка в активном стеке, сначала оцени continue, stop, do_not_start, retest_only или осторожную корректировку, а не автоматически start;
- если есть основания не рекомендовать нутриент, ты всё равно можешь вернуть его в nutrient_plan со статусом likely_ok или unclear и с recommend_action do_not_start или retest_only, чтобы ответ был честным и понятным;
- каждый входной анализ из массива labs обязан присутствовать в labs_review;
- нельзя молча пропускать анализы, даже если по ним нет практического назначения в nutrient_plan;
- если во входном анализе есть interpreted_status_pre_llm и reference_known=true, используй это как базу, а не пиши автоматически "нет референсов";
- если reference_source=registry или reference_source=user, считай что опора на референс есть;
- формулировку про нехватку референсов используй только когда reference_known=false и interpreted_status_pre_llm=unclear;
- если данных по анализу недостаточно, в labs_review ставь actionability=insufficient_data и дай короткое объяснение;
- не используй markdown;
- не добавляй пояснения вне JSON;
- возвращай только валидный JSON.

Требование к стилю:
- пиши по-русски;
- объясняй просто, но профессионально;
- не нагнетай;
- не пугай;
- не используй медицинскую истерику;
- не делай обещаний лечения;
- формулируй рекомендации как информационно-аналитический план.

Верни только JSON, строго по контракту, без любых дополнительных слов."""

NUTRIENT_ASSISTANT_JSON_CONTRACT = """{
  "summary_ru": "string",
  "confidence": "low|medium|high",
  "safety_flags": ["string"],
  "physician_flags": ["string"],
  "labs_used": [
    {
      "analyte_code": "string",
      "value": "string",
      "unit": "string",
      "date": "string"
    }
  ],
  "missing_data": ["string"],
  "nutrient_plan": [
    {
      "nutrient_code": "string",
      "nutrient_name_ru": "string",
      "status": "likely_low_or_risk|likely_ok|unclear|possible_excess",
      "recommend_action": "start|continue|stop|do_not_start|discuss_with_physician|retest_only",
      "dose_text_ru": "string",
      "frequency_text_ru": "string",
      "duration_text_ru": "string",
      "timing_text_ru": "string",
      "with_food": true,
      "reason_ru": "string",
      "monitor_ru": "string",
      "retest_after_text_ru": "string",
      "stop_or_pause_rule_ru": "string",
      "cautions_ru": ["string"],
      "interactions_ru": ["string"]
    }
  ],
  "labs_review": [
    {
      "analyte_code": "string",
      "analyte_name_ru": "string",
      "value": "string",
      "unit": "string",
      "ref_low": "string|number|null",
      "ref_high": "string|number|null",
      "interpretation_status": "low|normal|high|unclear",
      "mapped_nutrient_code": "string",
      "actionability": "actionable|watch|retest|insufficient_data",
      "short_reason_ru": "string"
    }
  ],
  "next_steps_ru": ["string"],
  "disclaimer_ru": "string"
}"""

NUTRIENT_ASSISTANT_JSON_SCHEMA = {
    "name": "nutrient_assistant_plan",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary_ru": {"type": "string"},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "safety_flags": {"type": "array", "items": {"type": "string"}},
            "physician_flags": {"type": "array", "items": {"type": "string"}},
            "labs_used": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "analyte_code": {"type": "string"},
                        "value": {"type": "string"},
                        "unit": {"type": "string"},
                        "date": {"type": "string"},
                    },
                    "required": ["analyte_code", "value", "unit", "date"],
                },
            },
            "missing_data": {"type": "array", "items": {"type": "string"}},
            "nutrient_plan": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "nutrient_code": {"type": "string"},
                        "nutrient_name_ru": {"type": "string"},
                        "status": {"type": "string", "enum": ["likely_low_or_risk", "likely_ok", "unclear", "possible_excess"]},
                        "recommend_action": {"type": "string", "enum": ["start", "continue", "stop", "do_not_start", "discuss_with_physician", "retest_only"]},
                        "dose_text_ru": {"type": "string"},
                        "frequency_text_ru": {"type": "string"},
                        "duration_text_ru": {"type": "string"},
                        "timing_text_ru": {"type": "string"},
                        "with_food": {"type": "boolean"},
                        "reason_ru": {"type": "string"},
                        "monitor_ru": {"type": "string"},
                        "retest_after_text_ru": {"type": "string"},
                        "stop_or_pause_rule_ru": {"type": "string"},
                        "cautions_ru": {"type": "array", "items": {"type": "string"}},
                        "interactions_ru": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": [
                        "nutrient_code", "nutrient_name_ru", "status", "recommend_action", "dose_text_ru",
                        "frequency_text_ru", "duration_text_ru", "timing_text_ru", "with_food", "reason_ru",
                        "monitor_ru", "retest_after_text_ru", "stop_or_pause_rule_ru", "cautions_ru", "interactions_ru",
                    ],
                },
            },
            "labs_review": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "analyte_code": {"type": "string"},
                        "analyte_name_ru": {"type": "string"},
                        "value": {"type": "string"},
                        "unit": {"type": "string"},
                        "ref_low": {"type": ["string", "number", "null"]},
                        "ref_high": {"type": ["string", "number", "null"]},
                        "interpretation_status": {"type": "string", "enum": ["low", "normal", "high", "unclear"]},
                        "mapped_nutrient_code": {"type": "string"},
                        "actionability": {"type": "string", "enum": ["actionable", "watch", "retest", "insufficient_data"]},
                        "short_reason_ru": {"type": "string"},
                    },
                    "required": [
                        "analyte_code", "analyte_name_ru", "value", "unit", "ref_low", "ref_high",
                        "interpretation_status", "mapped_nutrient_code", "actionability", "short_reason_ru",
                    ],
                },
            },
            "next_steps_ru": {"type": "array", "items": {"type": "string"}},
            "disclaimer_ru": {"type": "string"},
        },
        "required": ["summary_ru", "confidence", "safety_flags", "physician_flags", "labs_used", "missing_data", "nutrient_plan", "labs_review", "next_steps_ru", "disclaimer_ru"],
    },
}


def _validate_strict_schema_consistency(schema: dict, path: str = "schema") -> None:
    if not isinstance(schema, dict):
        return
    node_type = schema.get("type")
    if node_type == "object":
        if schema.get("additionalProperties") is not False:
            raise RuntimeError(f"Invalid strict schema at {path}: object node must set additionalProperties=false")
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            raise RuntimeError(f"Invalid strict schema at {path}: object node must define properties object")
        required = schema.get("required")
        if not isinstance(required, list):
            raise RuntimeError(f"Invalid strict schema at {path}: object node must define required array")
        prop_keys = set(properties.keys())
        req_keys = {str(x) for x in required}
        if req_keys != prop_keys:
            missing_in_required = sorted(prop_keys - req_keys)
            unknown_required = sorted(req_keys - prop_keys)
            raise RuntimeError(
                f"Invalid strict schema at {path}: required must match properties exactly; "
                f"missing_in_required={missing_in_required}; unknown_required={unknown_required}"
            )
        for key, child in properties.items():
            _validate_strict_schema_consistency(child, path=f"{path}.properties.{key}")
    elif node_type == "array":
        _validate_strict_schema_consistency(schema.get("items"), path=f"{path}.items")
    else:
        for key in ("anyOf", "oneOf", "allOf"):
            variants = schema.get(key)
            if isinstance(variants, list):
                for idx, child in enumerate(variants):
                    _validate_strict_schema_consistency(child, path=f"{path}.{key}[{idx}]")


_validate_strict_schema_consistency(NUTRIENT_ASSISTANT_JSON_SCHEMA["schema"], path="NUTRIENT_ASSISTANT_JSON_SCHEMA.schema")

SUGGEST_STACK_JSON_SCHEMA = {
    "name": "supplement_stack_suggestion",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "goals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "goal_ru": {"type": "string"},
                        "candidates": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "name": {"type": "string"},
                                    "name_ru": {"type": "string"},
                                    "why_ru": {"type": "string"},
                                    "dose_ru": {"type": "string"},
                                    "timing_ru": {"type": "string"},
                                    "confidence": {"type": "number", "minimum": 0, "maximum": 100},
                                },
                                "required": ["name", "name_ru", "why_ru", "dose_ru", "timing_ru", "confidence"],
                            },
                        },
                    },
                    "required": ["goal_ru", "candidates"],
                },
            },
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string"},
                        "name_ru": {"type": "string"},
                        "why_ru": {"type": "string"},
                        "dose_ru": {"type": "string"},
                        "timing_ru": {"type": "string"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 100},
                    },
                    "required": ["name", "name_ru", "why_ru", "dose_ru", "timing_ru", "confidence"],
                },
            },
            "warnings_ru": {"type": "array", "items": {"type": "string"}},
            "questions_ru": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["goals", "candidates", "warnings_ru", "questions_ru"],
    },
}


@dataclass(frozen=True)
class OpenAIConfig:
    api_key: str
    model: str

@dataclass(frozen=True)
class OpenAITranscribeConfig:
    api_key: str
    model: str

def _build_openai_client(
    cfg: OpenAIConfig | OpenAITranscribeConfig,
    timeout_s: float | None = None,
    max_retries: int | None = None,
):
    from openai import OpenAI
    params: dict[str, object] = {"api_key": cfg.api_key}
    if timeout_s is not None:
        params["timeout"] = timeout_s
    if max_retries is not None:
        params["max_retries"] = max_retries
    return OpenAI(**params)


def _sync_call(
    cfg: OpenAIConfig,
    *,
    instructions: str,
    input_text: str,
    max_output_tokens: int | None = None,
    timeout_s: float | None = None,
    max_retries: int | None = None,
    text_format: dict | None = None,
) -> str:
    client = _build_openai_client(cfg, timeout_s=timeout_s, max_retries=max_retries)
    params = {
        "model": cfg.model,
        "instructions": instructions,
        "input": input_text,
    }
    if max_output_tokens is not None:
        params["max_output_tokens"] = max_output_tokens
    if text_format is not None:
        params["text"] = {"format": text_format}
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
    client = _build_openai_client(cfg, timeout_s=timeout_s)
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
    client = _build_openai_client(cfg, timeout_s=timeout_s)
    params = {
        "model": cfg.model,
        "input": messages,
    }
    if max_output_tokens is not None:
        params["max_output_tokens"] = max_output_tokens
    resp = client.responses.create(**params)
    return (resp.output_text or "").strip()


def _sync_call_nutrient_assistant(
    cfg: OpenAIConfig,
    *,
    payload_json: str,
    max_output_tokens: int | None = None,
    timeout_s: float | None = None,
    max_retries: int | None = None,
) -> str:
    client = _build_openai_client(cfg, timeout_s=timeout_s, max_retries=max_retries)
    instructions = f"{NUTRIENT_ASSISTANT_SYSTEM_PROMPT}\nJSON-контракт:\n{NUTRIENT_ASSISTANT_JSON_CONTRACT}"
    params = {
        "model": cfg.model,
        "instructions": instructions,
        "input": payload_json,
        "text": {
            "format": {
                "type": "json_schema",
                "name": NUTRIENT_ASSISTANT_JSON_SCHEMA["name"],
                "strict": NUTRIENT_ASSISTANT_JSON_SCHEMA["strict"],
                "schema": NUTRIENT_ASSISTANT_JSON_SCHEMA["schema"],
            }
        },
    }
    model_name = str(cfg.model or "").lower()
    # GPT-5 family does not accept temperature for Responses API in this setup.
    # Keep temperature only for older families where it is valid.
    if not model_name.startswith("gpt-5"):
        params["temperature"] = 0.1
    if max_output_tokens is not None:
        params["max_output_tokens"] = max_output_tokens
    try:
        resp = client.responses.create(**params)
    except BadRequestError as exc:
        message = str(exc)
        if "Unsupported parameter: 'temperature'" in message and "temperature" in params:
            params.pop("temperature", None)
            log.warning(
                "nutrient assistant responses.create fallback: retrying without temperature for model=%s",
                cfg.model,
            )
            resp = client.responses.create(**params)
        else:
            raise
    return (resp.output_text or "").strip()


async def nutrient_assistant_single_call(
    cfg: Optional[OpenAIConfig],
    *,
    payload: dict,
) -> str:
    if not cfg:
        raise RuntimeError("OPENAI_DISABLED")
    payload_json = json.dumps(payload, ensure_ascii=False)
    labs_count = len(list(payload.get("labs") or []))
    # Keep answer compact for large lab payloads so model has less to generate
    # and can respond more predictably under network jitter.
    max_output_tokens = 2200 if labs_count <= 20 else (1800 if labs_count <= 40 else 1400)
    # Production-friendly compact ladder: one main call + one compact retry.
    timeouts = (23, 17)
    last_exc: Exception | None = None
    for attempt, timeout_s in enumerate(timeouts, start=1):
        try:
            return await asyncio.to_thread(
                _sync_call_nutrient_assistant,
                cfg,
                payload_json=payload_json,
                max_output_tokens=max_output_tokens,
                timeout_s=timeout_s,
                max_retries=0,
            )
        except (APITimeoutError, APIConnectionError) as exc:
            last_exc = exc
            log.warning(
                "nutrient_assistant transient OpenAI error on attempt %s/%s: %s",
                attempt,
                len(timeouts),
                exc,
            )
            if attempt < len(timeouts):
                await asyncio.sleep(0.8 * attempt)
                continue
            raise
    if last_exc is not None:
        raise last_exc
    return ""

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

    raw_goal = str(goal_text or "").strip()
    compact_goal = " ".join(raw_goal.split())
    goal_parts = [part.strip(" ,;:-") for part in compact_goal.replace(" и ", ",").split(",")]
    normalized_goals: list[str] = []
    for part in goal_parts:
        lowered = part.lower()
        if len(lowered) < 3:
            continue
        if lowered in {"хочу", "нужно", "надо", "чтобы", "в целом", "вообще"}:
            continue
        if lowered in normalized_goals:
            continue
        normalized_goals.append(lowered)
    if not normalized_goals and compact_goal:
        normalized_goals = [compact_goal.lower()]
    limited_goals = normalized_goals[:6]
    goals_hint = "; ".join(limited_goals) if limited_goals else compact_goal

    instructions = (
        "Ты помощник по первичному подбору БАДов в Telegram-боте. "
        "Никаких диагнозов и лечения; только консервативные информационные рекомендации. "
        "Пользователь уже получил юридические предупреждения. "
        "Возвращай только структурированный JSON по заданной схеме. "
        "Покрывай цели широко и практично, без чрезмерной осторожной недодачи вариантов. "
        "Если целей очень много, обрабатывай только первые 6 самых явных, чтобы гарантировать полный валидный JSON-ответ."
    )

    prompt = f"""ВХОДНЫЕ ДАННЫЕ
- Цель пользователя (оригинал): {goal_text}
- Нормализованные цели (лимит 6): {goals_hint}
- Текущие добавки: {user_supplements_text}

ТРЕБОВАНИЯ
1) Выдели явные цели пользователя и покрой каждую отдельным блоком в goals[].
2) Для каждой цели предложи 2-4 релевантных кандидатов (минимум 1, если данных мало), без дублирования внутри цели.
3) На каждую добавку дай очень коротко why_ru, dose_ru, timing_ru (до ~12 слов в каждом поле) и confidence 0-100.
4) Используй распространённые названия для каталога: name (англ/латиница), name_ru (русский).
5) Если данных мало — уменьши confidence и добавь предупреждения в warnings_ru.
6) questions_ru всегда пустой массив [].
7) На верхнем уровне candidates[] верни агрегированный список 6-14 кандидатов (уникальных по name), собранный из целей.
8) Если в цели есть "тестостерон", "сила", "восстановление", "печень" или близкие формулировки — обязательно дай отдельные блоки под них.
9) Внутри каждой цели старайся давать разные по механике варианты, а не однотипные дубли.
10) Верни только JSON-объект по схеме без markdown и без пояснений вне JSON.
"""

    timeouts = (35, 65, 95)
    last_exc: Exception | None = None
    text_format = {
        "type": "json_schema",
        "name": SUGGEST_STACK_JSON_SCHEMA["name"],
        "strict": True,
        "schema": SUGGEST_STACK_JSON_SCHEMA["schema"],
    }

    for attempt, timeout_s in enumerate(timeouts, start=1):
        try:
            return await asyncio.to_thread(
                _sync_call,
                cfg,
                instructions=instructions,
                input_text=prompt,
                max_output_tokens=2200,
                timeout_s=timeout_s,
                max_retries=0,
                text_format=text_format,
            )
        except (APITimeoutError, APIConnectionError) as exc:
            last_exc = exc
            log.warning("suggest_stack_v1 transient OpenAI error on attempt %s/%s: %s", attempt, len(timeouts), exc)
            if attempt < len(timeouts):
                await asyncio.sleep(0.6 * attempt)
                continue
            raise

    if last_exc is not None:
        raise last_exc
    return ""

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


async def explain_plan_json(
    cfg: Optional[OpenAIConfig],
    *,
    instructions: str,
    payload_text: str,
    max_output_tokens: int = 900,
    timeout_s: float = 25,
) -> tuple[dict | None, str]:
    if not cfg:
        return None, ""
    try:
        raw = await asyncio.to_thread(
            _sync_call,
            cfg,
            instructions=instructions,
            input_text=payload_text,
            max_output_tokens=max_output_tokens,
            timeout_s=timeout_s,
        )
    except Exception:
        log.exception("OpenAI plan explanation failed")
        return None, cfg.model
    return _parse_json_dict(raw), cfg.model

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
        "Формат ответа: строго JSON object (top-level { ... }), без Markdown и без лишнего текста. "
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
