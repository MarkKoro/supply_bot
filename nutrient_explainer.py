from __future__ import annotations

from typing import Any

from openai_client import OpenAIConfig, explain_plan_json
from nutrient_registry import get_nutrient_rule

PROMPT_VERSION = "nutrient_explainer_v1"


def build_explainer_payload(*, nutrient_code: str, preview: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    latest_labs = list(preview.get("latest_relevant_labs") or [])[:5]
    compact_labs = [
        {
            "analyte": row.get("analyte_code") or row.get("analyte_name") or "",
            "value": row.get("value_num") if row.get("value_num") is not None else row.get("value_text"),
            "unit": row.get("unit") or "",
            "measured_at": row.get("measured_at") or row.get("created_at_utc") or "",
        }
        for row in latest_labs
    ]
    return {
        "nutrient_code": nutrient_code,
        "title_ru": plan.get("title_ru") or preview.get("title_ru") or nutrient_code,
        "profile_snapshot": preview.get("profile_snapshot") or plan.get("current_context") or {},
        "latest_labs": compact_labs,
        "trend_summary": preview.get("trend_summary") or {},
        "interpreted_status": plan.get("interpreted_status") or preview.get("interpreted_status") or "unknown_or_limited",
        "confidence_level": plan.get("confidence_level") or preview.get("confidence_level") or "low",
        "plan_mode": plan.get("plan_mode") or "no_plan",
        "plan_phase": plan.get("plan_phase") or "initial_review",
        "action_strategy": plan.get("action_strategy") or "collect_more_data",
        "current_stack_matches": plan.get("current_stack_matches") or preview.get("current_stack_matches") or [],
        "current_stack_flags": plan.get("current_stack_flags") or preview.get("current_stack_flags") or [],
        "missing_inputs": plan.get("missing_inputs") or preview.get("missing_inputs") or [],
        "physician_flags": plan.get("physician_flags") or preview.get("physician_flags") or [],
        "safety_flags": plan.get("safety_flags") or preview.get("safety_flags") or [],
        "followup_items": plan.get("followup_items") or [],
        "retest_window_days_min": plan.get("retest_window_days_min"),
        "retest_window_days_max": plan.get("retest_window_days_max"),
        "pause_window_days_min": plan.get("pause_window_days_min"),
        "pause_window_days_max": plan.get("pause_window_days_max"),
        "timing_hints": plan.get("timing_hints") or [],
        "format_hints": plan.get("format_hints") or [],
        "intake_notes": plan.get("intake_notes") or [],
        "evidence_basis": preview.get("evidence_basis") or "",
        "ui_notes_ru": preview.get("ui_notes_ru") or rule.get("ui_notes_ru") or "",
        "limitation_note_ru": rule.get("limitation_note_ru") or "",
        "trust_copy_ru": rule.get("trust_copy_ru") or "",
        "dashboard_copy_ru": rule.get("dashboard_copy_ru") or "",
        "rules_version": str(plan.get("rules_version") or preview.get("rules_version") or rule.get("rules_version") or ""),
    }


def build_explainer_prompt(payload: dict[str, Any]) -> tuple[str, str]:
    instructions = (
        "Ты объясняешь пользователю уже готовый deterministic нутриентный план. "
        "Ты не врач, не ставишь диагнозы, не назначаешь дозировки и не придумываешь новые данные. "
        "Разрешено объяснять только поля из входного JSON. "
        "Нельзя противоречить plan_mode/plan_phase/action_strategy. "
        "Нельзя писать markdown и code fences. Верни только JSON object. "
        "Все поля и текст — на русском языке."
    )

    schema = {
        "title_ru": "",
        "summary_ru": "",
        "main_points_ru": [],
        "why_this_status_ru": "",
        "why_this_plan_ru": "",
        "what_to_do_now_ru": "",
        "what_to_monitor_ru": "",
        "what_to_retest_ru": "",
        "confidence_explained_ru": "",
        "cautions_ru": [],
        "missing_data_ru": [],
        "followup_ru": [],
        "tone_flags": {"calm": True, "non_alarmist": True},
        "disclaimer_short_ru": "",
    }

    prompt = (
        "ВХОДНЫЕ СТРУКТУРИРОВАННЫЕ ДАННЫЕ:\n"
        f"{payload}\n\n"
        "ОГРАНИЧЕНИЯ:\n"
        "- Не добавляй новых анализов/фактов.\n"
        "- Не предлагай дозировки.\n"
        "- Не ставь диагнозы.\n"
        "- Не советуй отменять назначения врача.\n"
        "- Если уверенность низкая, скажи это спокойно и ясно.\n\n"
        "Верни JSON строго по схеме:\n"
        f"{schema}"
    )
    return instructions, prompt


def _normalize_text_array(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def validate_explainer_response(raw: dict[str, Any] | None, fallback: dict[str, Any]) -> dict[str, Any]:
    data = dict(raw or {})
    out = {
        "title_ru": str(data.get("title_ru") or fallback["title_ru"]),
        "summary_ru": str(data.get("summary_ru") or fallback["summary_ru"]),
        "main_points_ru": _normalize_text_array(data.get("main_points_ru") or fallback["main_points_ru"]),
        "why_this_status_ru": str(data.get("why_this_status_ru") or fallback["why_this_status_ru"]),
        "why_this_plan_ru": str(data.get("why_this_plan_ru") or fallback["why_this_plan_ru"]),
        "what_to_do_now_ru": str(data.get("what_to_do_now_ru") or fallback["what_to_do_now_ru"]),
        "what_to_monitor_ru": str(data.get("what_to_monitor_ru") or fallback["what_to_monitor_ru"]),
        "what_to_retest_ru": str(data.get("what_to_retest_ru") or fallback["what_to_retest_ru"]),
        "confidence_explained_ru": str(data.get("confidence_explained_ru") or fallback["confidence_explained_ru"]),
        "cautions_ru": _normalize_text_array(data.get("cautions_ru") or fallback["cautions_ru"]),
        "missing_data_ru": _normalize_text_array(data.get("missing_data_ru") or fallback["missing_data_ru"]),
        "followup_ru": _normalize_text_array(data.get("followup_ru") or fallback["followup_ru"]),
        "tone_flags": data.get("tone_flags") if isinstance(data.get("tone_flags"), dict) else {"calm": True, "non_alarmist": True},
        "disclaimer_short_ru": str(data.get("disclaimer_short_ru") or fallback["disclaimer_short_ru"]),
    }
    if not out["main_points_ru"]:
        out["main_points_ru"] = fallback["main_points_ru"]
    return out


def build_fallback_explanation(payload: dict[str, Any]) -> dict[str, Any]:
    title = f"Объяснение по нутриенту: {payload.get('title_ru') or payload.get('nutrient_code')}"
    status = str(payload.get("interpreted_status") or "unknown")
    mode = str(payload.get("plan_mode") or "no_plan")
    phase = str(payload.get("plan_phase") or "initial_review")
    strategy = str(payload.get("action_strategy") or "collect_more_data")
    retest = ""
    if payload.get("retest_window_days_min") and payload.get("retest_window_days_max"):
        retest = f"Рекомендуемое окно пересдачи: {payload.get('retest_window_days_min')}–{payload.get('retest_window_days_max')} дней."

    missing_data = [f"Нужно уточнить: {x}" for x in (payload.get("missing_inputs") or [])]
    followup = [str(x.get("title_ru") or x.get("item_type") or "") for x in (payload.get("followup_items") or []) if x]
    cautions = [f"Флаг врача: {x}" for x in (payload.get("physician_flags") or [])] + [f"Ограничение безопасности: {x}" for x in (payload.get("safety_flags") or [])]

    return {
        "title_ru": title,
        "summary_ru": "Это объяснение построено по текущему плану и доступным данным без дополнительных предположений.",
        "main_points_ru": [
            f"Текущий статус по данным: {status}.",
            f"Режим и фаза плана: {mode} / {phase}.",
            f"Текущая стратегия: {strategy}.",
        ],
        "why_this_status_ru": "Статус определён из последних релевантных анализов и контекста профиля, которые уже учтены в preview.",
        "why_this_plan_ru": "План выбран детерминированным planner-слоем и отражает уровень уверенности и риски в данных.",
        "what_to_do_now_ru": "Следуйте текущей стратегии плана и не вносите резких изменений без подтверждающих данных.",
        "what_to_monitor_ru": "Отслеживайте самочувствие и актуальность текущего стека, а также обновляйте профиль при изменениях.",
        "what_to_retest_ru": retest or "Пересдача определяется после уточнения данных.",
        "confidence_explained_ru": f"Текущий уровень уверенности: {payload.get('confidence_level') or 'low'}.",
        "cautions_ru": cautions or ["План не заменяет консультацию врача и не является диагнозом."],
        "missing_data_ru": missing_data,
        "followup_ru": followup,
        "tone_flags": {"calm": True, "non_alarmist": True},
        "disclaimer_short_ru": str(payload.get("trust_copy_ru") or "Это информационное объяснение, а не медицинский диагноз или назначение."),
    }


async def call_plan_explainer_llm(*, cfg: OpenAIConfig | None, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    if not cfg:
        return None, None
    instructions, prompt = build_explainer_prompt(payload)
    parsed, model_name = await explain_plan_json(
        cfg,
        instructions=instructions,
        payload_text=prompt,
        max_output_tokens=900,
        timeout_s=25,
    )
    return parsed, model_name


async def build_explanation_from_plan(
    *,
    cfg: OpenAIConfig | None,
    nutrient_code: str,
    preview_snapshot: dict[str, Any],
    plan_snapshot: dict[str, Any],
) -> dict[str, Any]:
    payload = build_explainer_payload(nutrient_code=nutrient_code, preview=preview_snapshot, plan=plan_snapshot)
    fallback = build_fallback_explanation(payload)
    parsed, model_name = await call_plan_explainer_llm(cfg=cfg, payload=payload)
    if parsed is None:
        explanation = fallback
        source_type = "fallback"
    else:
        explanation = validate_explainer_response(parsed, fallback)
        source_type = "llm"

    return {
        "explanation": explanation,
        "source_type": source_type,
        "model_name": model_name,
        "prompt_version": PROMPT_VERSION,
        "payload": payload,
    }
