from __future__ import annotations
import json
import logging
import re
from datetime import datetime, timedelta, timezone
import time

import db
from openai import APIConnectionError, APITimeoutError
from openai_client import OpenAIConfig, nutrient_assistant_single_call
from settings import get_settings

s = get_settings()
log = logging.getLogger(__name__)

ASSISTANT_PROMPT_VERSION = "v4"
NUTRIENT_ASSISTANT_MONTHLY_LIMIT = 5


def _norm_unit(value: str) -> str:
    unit = str(value or "").strip().lower()
    unit = unit.replace("μ", "µ")
    unit = unit.replace("мкг", "ug").replace("мкмоль", "umol").replace("ммоль", "mmol").replace("мг", "mg").replace("пг", "pg").replace("нг", "ng")
    unit = unit.replace("л", "l").replace("дл", "dl")
    unit = unit.replace("ме", "iu")
    return unit


ASSISTANT_ANALYTE_REGISTRY: dict[str, dict] = {
    "vitamin_d_25oh": {"name_ru": "Витамин D (25-OH)", "nutrient_code": "vitamin_d", "units": ["ng/ml", "nmol/l"], "aliases": ["25ohd", "25_ohd"], "default_unit": "ng/mL", "ref_ranges": {"any": {"ng/ml": (30.0, 100.0), "nmol/l": (75.0, 250.0)}}},
    "vitamin_b12": {"name_ru": "Витамин B12", "nutrient_code": "vitamin_b12", "units": ["pg/ml", "pmol/l"], "aliases": ["b12"], "default_unit": "pg/mL", "ref_ranges": {"any": {"pg/ml": (200.0, 900.0), "pmol/l": (148.0, 665.0)}}},
    "folate": {"name_ru": "Фолат", "nutrient_code": "folate", "units": ["ng/ml", "nmol/l"], "aliases": ["vitamin_b9"], "default_unit": "ng/mL", "ref_ranges": {"any": {"ng/ml": (3.0, 17.0), "nmol/l": (7.0, 39.0)}}},
    "ferritin": {"name_ru": "Ферритин", "nutrient_code": "iron", "units": ["ng/ml", "ug/l"], "default_unit": "ng/mL", "ref_ranges": {"female": {"ng/ml": (15.0, 150.0), "ug/l": (15.0, 150.0)}, "male": {"ng/ml": (30.0, 400.0), "ug/l": (30.0, 400.0)}, "any": {"ng/ml": (15.0, 400.0), "ug/l": (15.0, 400.0)}}},
    "serum_iron": {"name_ru": "Железо сывороточное", "nutrient_code": "iron", "units": ["umol/l", "ug/dl"], "default_unit": "µmol/L", "ref_ranges": {"any": {"umol/l": (9.0, 30.0), "ug/dl": (50.0, 170.0)}}},
    "hemoglobin": {"name_ru": "Гемоглобин", "nutrient_code": "iron", "units": ["g/l", "g/dl"], "default_unit": "g/L", "ref_ranges": {"female": {"g/l": (120.0, 150.0), "g/dl": (12.0, 15.0)}, "male": {"g/l": (130.0, 170.0), "g/dl": (13.0, 17.0)}, "any": {"g/l": (120.0, 170.0), "g/dl": (12.0, 17.0)}}},
    "magnesium_serum": {"name_ru": "Магний (сыворотка)", "nutrient_code": "magnesium", "units": ["mmol/l", "mg/dl"], "aliases": ["magnesium"], "default_unit": "mmol/L", "ref_ranges": {"any": {"mmol/l": (0.75, 1.05), "mg/dl": (1.8, 2.6)}}},
    "zinc": {"name_ru": "Цинк", "nutrient_code": "zinc", "units": ["umol/l", "ug/dl"], "default_unit": "µmol/L", "ref_ranges": {"any": {"umol/l": (10.7, 18.4), "ug/dl": (70.0, 120.0)}}},
    "selenium": {"name_ru": "Селен", "nutrient_code": "selenium", "units": ["ug/l", "umol/l"], "default_unit": "µg/L", "ref_ranges": {"any": {"ug/l": (70.0, 150.0), "umol/l": (0.89, 1.90)}}},
    "vitamin_a": {"name_ru": "Витамин A (ретинол)", "nutrient_code": "vitamin_a", "units": ["ug/l", "umol/l"], "default_unit": "µg/L", "ref_ranges": {"any": {"ug/l": (300.0, 900.0), "umol/l": (1.05, 3.14)}}},
    "vitamin_b1": {"name_ru": "Витамин B1 (тиамин)", "nutrient_code": "vitamin_b1", "units": ["nmol/l", "ug/l"], "default_unit": "nmol/L", "ref_ranges": {"any": {"nmol/l": (70.0, 180.0), "ug/l": (21.0, 54.0)}}},
    "calcium": {"name_ru": "Кальций", "nutrient_code": "calcium", "units": ["mmol/l", "mg/dl"], "default_unit": "mmol/L", "ref_ranges": {"any": {"mmol/l": (2.15, 2.55), "mg/dl": (8.6, 10.2)}}},
    "vitamin_e": {"name_ru": "Витамин E", "nutrient_code": "vitamin_e", "units": ["mg/l", "umol/l"], "default_unit": "mg/L", "ref_ranges": {"any": {"mg/l": (5.0, 20.0), "umol/l": (12.0, 46.0)}}},
    "vitamin_c": {"name_ru": "Витамин C", "nutrient_code": "vitamin_c", "units": ["mg/l", "umol/l"], "default_unit": "mg/L", "ref_ranges": {"any": {"mg/l": (4.0, 20.0), "umol/l": (23.0, 114.0)}}},
    "vitamin_b2": {"name_ru": "Витамин B2 (рибофлавин)", "nutrient_code": "vitamin_b2", "units": ["nmol/l", "ug/l"], "default_unit": "nmol/L", "ref_ranges": {"any": {"nmol/l": (200.0, 500.0), "ug/l": (52.0, 130.0)}}},
    "phosphorus": {"name_ru": "Фосфор", "nutrient_code": "phosphorus", "units": ["mmol/l", "mg/dl"], "default_unit": "mmol/L", "ref_ranges": {"any": {"mmol/l": (0.81, 1.45), "mg/dl": (2.5, 4.5)}}},
    "potassium": {"name_ru": "Калий", "nutrient_code": "potassium", "units": ["mmol/l", "meq/l"], "default_unit": "mmol/L", "ref_ranges": {"any": {"mmol/l": (3.5, 5.1), "meq/l": (3.5, 5.1)}}},
    "sodium": {"name_ru": "Натрий", "nutrient_code": "sodium", "units": ["mmol/l", "meq/l"], "default_unit": "mmol/L", "ref_ranges": {"any": {"mmol/l": (135.0, 145.0), "meq/l": (135.0, 145.0)}}},
    "copper": {"name_ru": "Медь", "nutrient_code": "copper", "units": ["ug/dl", "umol/l"], "default_unit": "µg/dL", "ref_ranges": {"any": {"ug/dl": (70.0, 140.0), "umol/l": (11.0, 22.0)}}},
    "iodine": {"name_ru": "Йод", "nutrient_code": "iodine", "units": ["ug/l", "umol/l"], "default_unit": "µg/L", "ref_ranges": {"any": {"ug/l": (40.0, 92.0), "umol/l": (0.32, 0.72)}}},
    "choline": {"name_ru": "Холин", "nutrient_code": "choline", "units": ["umol/l", "mg/l"], "default_unit": "µmol/L", "ref_ranges": {"any": {"umol/l": (7.0, 20.0), "mg/l": (0.73, 2.1)}}},
    "vitamin_b3": {"name_ru": "Витамин B3 (ниацин)", "nutrient_code": "vitamin_b3", "units": ["umol/l", "mg/l"], "default_unit": "µmol/L", "ref_ranges": {"any": {"umol/l": (10.0, 50.0)}}},
    "vitamin_b5": {"name_ru": "Витамин B5 (пантотеновая кислота)", "nutrient_code": "vitamin_b5", "units": ["umol/l", "mg/l"], "default_unit": "µmol/L", "ref_ranges": {"any": {"umol/l": (1.0, 8.0)}}},
    "vitamin_b6": {"name_ru": "Витамин B6", "nutrient_code": "vitamin_b6", "units": ["nmol/l", "ug/l"], "default_unit": "nmol/L", "ref_ranges": {"any": {"nmol/l": (20.0, 125.0)}}},
    "vitamin_b7": {"name_ru": "Витамин B7 (биотин)", "nutrient_code": "vitamin_b7", "units": ["ng/l", "nmol/l"], "default_unit": "ng/L", "ref_ranges": {"any": {"ng/l": (200.0, 1200.0)}}},
    "tsh": {"name_ru": "ТТГ", "nutrient_code": "", "units": ["miu/l", "uiu/ml"], "default_unit": "mIU/L", "ref_ranges": {"any": {"miu/l": (0.4, 4.0), "uiu/ml": (0.4, 4.0)}}},
    "free_t4": {"name_ru": "Свободный T4", "nutrient_code": "", "units": ["pmol/l", "ng/dl"], "default_unit": "pmol/L", "ref_ranges": {"any": {"pmol/l": (10.0, 23.0), "ng/dl": (0.8, 1.8)}}},
    "free_t3": {"name_ru": "Свободный T3", "nutrient_code": "", "units": ["pmol/l", "pg/ml"], "default_unit": "pmol/L", "ref_ranges": {"any": {"pmol/l": (3.1, 6.8), "pg/ml": (2.0, 4.4)}}},
    "crp": {"name_ru": "С-реактивный белок", "nutrient_code": "", "units": ["mg/l", "mg/dl"], "default_unit": "mg/L", "ref_ranges": {"any": {"mg/l": (0.0, 5.0), "mg/dl": (0.0, 0.5)}}},
    "hba1c": {"name_ru": "HbA1c", "nutrient_code": "", "units": ["%"], "default_unit": "%", "ref_ranges": {"any": {"%": (4.0, 5.6)}}},
    "glucose_fasting": {"name_ru": "Глюкоза натощак", "nutrient_code": "", "units": ["mmol/l", "mg/dl"], "default_unit": "mmol/L", "ref_ranges": {"any": {"mmol/l": (3.9, 5.5), "mg/dl": (70.0, 99.0)}}},
    "omega3_index": {"name_ru": "Омега-3 индекс", "nutrient_code": "omega_3", "units": ["%"], "default_unit": "%", "ref_ranges": {"any": {"%": (8.0, 12.0)}}},
}

ASSISTANT_ALLOWED_NUTRIENT_CODES: list[str] = sorted(
    {
        str(meta.get("nutrient_code") or "").strip().lower()
        for meta in ASSISTANT_ANALYTE_REGISTRY.values()
        if str(meta.get("nutrient_code") or "").strip()
    }
)
ASSISTANT_ALLOWED_NUTRIENT_CODE_SET: set[str] = set(ASSISTANT_ALLOWED_NUTRIENT_CODES)
ASSISTANT_NUTRIENT_CODE_ALIASES: dict[str, str] = {
    "omega3": "omega_3",
    "omega-3": "omega_3",
    "omega_3": "omega_3",
    "vitamin d": "vitamin_d",
    "vitamin-d": "vitamin_d",
    "vitamin b12": "vitamin_b12",
    "vitamin-b12": "vitamin_b12",
    "vitamin b9": "folate",
    "vitamin-b9": "folate",
    "b9": "folate",
}


def _normalize_nutrient_code(raw_code: str) -> str:
    code = str(raw_code or "").strip().lower()
    if not code:
        return ""
    compact = re.sub(r"[^a-z0-9]+", "_", code).strip("_")
    candidate = ASSISTANT_NUTRIENT_CODE_ALIASES.get(code) or ASSISTANT_NUTRIENT_CODE_ALIASES.get(compact) or compact
    return candidate if candidate in ASSISTANT_ALLOWED_NUTRIENT_CODE_SET else ""


def _normalize_analyte_code(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower())
    return text.strip("_")


_ANALYTE_ALIAS_MAP: dict[str, str] = {}
for _code, _meta in ASSISTANT_ANALYTE_REGISTRY.items():
    _ANALYTE_ALIAS_MAP[_code] = _code
    for _alias in list(_meta.get("aliases") or []):
        _ANALYTE_ALIAS_MAP[_normalize_analyte_code(str(_alias))] = _code


def _canonical_analyte_code(raw_code: str) -> str:
    normalized = _normalize_analyte_code(raw_code)
    return _ANALYTE_ALIAS_MAP.get(normalized, normalized)


def _safe_float(value: str | float | int | None) -> float | None:
    if value is None:
        return None
    txt = str(value).strip().replace(",", ".")
    if not txt:
        return None
    try:
        return float(txt)
    except ValueError:
        return None


def _pick_ref_range(registry_item: dict, sex_key: str, unit: str) -> tuple[float | None, float | None]:
    ref_ranges = dict(registry_item.get("ref_ranges") or {})
    unit_key = _norm_unit(unit)
    preferred = [sex_key, "any"]
    for key in preferred:
        per_sex = dict(ref_ranges.get(key) or {})
        if unit_key in per_sex:
            low, high = per_sex[unit_key]
            return _safe_float(low), _safe_float(high)
    return None, None


def _safe_json_loads(raw: str) -> dict | None:
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _decode_json_or_default(raw, default):
    try:
        data = json.loads(str(raw or ""))
        return data if isinstance(data, type(default)) else default
    except Exception:
        return default


def _extract_json_object(raw: str) -> dict | None:
    direct = _safe_json_loads(str(raw or "").strip())
    if direct is not None:
        return direct
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
        direct = _safe_json_loads(text)
        if direct is not None:
            return direct
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return _safe_json_loads(text[start : end + 1])
    return None


def _validate_assistant_response(data: dict, *, allowed_nutrient_codes: set[str] | None = None) -> tuple[bool, str]:
    required = [
        "summary_ru", "confidence", "safety_flags", "physician_flags", "labs_used",
        "missing_data", "nutrient_plan", "labs_review", "next_steps_ru", "disclaimer_ru",
    ]
    for key in required:
        if key not in data:
            return False, f"missing_field:{key}"
    if str(data.get("confidence") or "") not in {"low", "medium", "high"}:
        return False, "invalid_confidence"
    if not isinstance(data.get("nutrient_plan"), list):
        return False, "invalid_nutrient_plan"
    if not isinstance(data.get("labs_review"), list):
        return False, "invalid_labs_review"
    for idx, lab in enumerate(list(data.get("labs_review") or [])):
        if not isinstance(lab, dict):
            return False, f"invalid_labs_review_item:{idx}"
        required_lab = [
            "analyte_code", "analyte_name_ru", "value", "unit", "ref_low", "ref_high",
            "interpretation_status", "mapped_nutrient_code", "actionability", "short_reason_ru",
        ]
        for field in required_lab:
            if field not in lab:
                return False, f"missing_labs_review_field:{idx}:{field}"
        if str(lab.get("interpretation_status") or "") not in {"low", "normal", "high", "unclear"}:
            return False, f"invalid_interpretation_status:{idx}"
        if str(lab.get("actionability") or "") not in {"actionable", "watch", "retest", "insufficient_data"}:
            return False, f"invalid_actionability:{idx}"
        mapped_code = _normalize_nutrient_code(str(lab.get("mapped_nutrient_code") or ""))
        if str(lab.get("mapped_nutrient_code") or "").strip() and not mapped_code:
            return False, f"invalid_mapped_nutrient_code:{idx}"
        if allowed_nutrient_codes is not None and mapped_code and mapped_code not in allowed_nutrient_codes:
            return False, f"mapped_nutrient_code_out_of_catalog:{idx}"
    for idx, item in enumerate(list(data.get("nutrient_plan") or [])):
        if not isinstance(item, dict):
            return False, f"invalid_nutrient_item:{idx}"
        required_item = [
            "nutrient_code", "nutrient_name_ru", "status", "recommend_action", "pause_after_course_days", "cycle_mode", "dose_text_ru",
            "frequency_text_ru", "duration_text_ru", "timing_text_ru", "with_food", "reason_ru",
            "monitor_ru", "retest_after_text_ru", "stop_or_pause_rule_ru", "cautions_ru", "interactions_ru",
        ]
        for field in required_item:
            if field not in item:
                return False, f"missing_nutrient_field:{idx}:{field}"
        action = str(item.get("recommend_action") or "")
        if action not in {"start", "continue", "stop", "do_not_start", "discuss_with_physician", "retest_only"}:
            return False, f"invalid_action:{idx}"
        code = _normalize_nutrient_code(str(item.get("nutrient_code") or ""))
        if not code:
            return False, f"invalid_nutrient_code:{idx}"
        if allowed_nutrient_codes is not None and code not in allowed_nutrient_codes:
            return False, f"nutrient_code_out_of_catalog:{idx}"
        item["nutrient_code"] = code
    return True, ""


async def _compute_assistant_with_retry(
    cfg: OpenAIConfig,
    llm_payload: dict,
    retries: int = 2,
    *,
    allowed_nutrient_codes: set[str] | None = None,
) -> tuple[str, dict | None, str]:
    last_raw = ""
    reason = "invalid_json"
    for _ in range(max(0, int(retries)) + 1):
        last_raw = await nutrient_assistant_single_call(cfg, payload=llm_payload)
        parsed = _extract_json_object(last_raw)
        if not parsed:
            reason = "invalid_json"
            continue
        valid, reason = _validate_assistant_response(parsed, allowed_nutrient_codes=allowed_nutrient_codes)
        if valid:
            return last_raw, parsed, ""
    return last_raw, None, reason


def _timeout_failsafe_payload(llm_payload: dict) -> dict:
    payload = dict(llm_payload or {})
    compact = dict(payload.get("assistant_context") or {})
    compact["timeout_failsafe"] = True
    compact["payload_compaction"] = "labs<=24,stack<=12,trim_text"
    payload["assistant_context"] = compact
    payload["labs"] = _compact_labs_for_llm(list(payload.get("labs") or []), max_items=24, max_per_analyte=1)
    payload["active_stack"] = list(payload.get("active_stack") or [])[:12]

    profile = dict(payload.get("user_profile") or {})
    for key in ("symptoms_other_free_text", "primary_goal"):
        if str(profile.get(key) or "") and len(str(profile.get(key) or "")) > 300:
            profile[key] = str(profile.get(key) or "")[:300]
    payload["user_profile"] = profile
    return payload


def _build_fallback_nutrient_plan_from_labs(
    labs_for_llm: list[dict],
    *,
    active_stack_codes: set[str] | None = None,
    allowed_nutrient_codes: set[str] | None = None,
    max_items: int = 20,
) -> list[dict]:
    plan: list[dict] = []
    seen_codes: set[str] = set()
    active_codes = {str(x or "").strip().lower() for x in (active_stack_codes or set()) if str(x or "").strip()}
    for item in list(labs_for_llm or []):
        nutrient_code = _normalize_nutrient_code(str(item.get("canonical_nutrient_code") or ""))
        if not nutrient_code or nutrient_code in seen_codes:
            continue
        if allowed_nutrient_codes is not None and nutrient_code not in allowed_nutrient_codes:
            continue
        status = str(item.get("interpreted_status_pre_llm") or "unclear")
        in_active_stack = nutrient_code in active_codes
        if status == "low":
            action = "start"
            reason = "Показатель ниже референса; есть основания для коррекции."
        elif status == "high":
            action = "stop" if in_active_stack else "do_not_start"
            reason = (
                "Показатель выше референса на фоне текущего приёма; стоит остановить и пересмотреть."
                if in_active_stack
                else "Показатель выше референса, начинать добавку не нужно."
            )
        elif status == "normal":
            action = "continue" if in_active_stack else "do_not_start"
            reason = (
                "Показатель в референсе; сохранить текущую поддержку можно только если она уже идёт и есть контекст."
                if in_active_stack
                else "Показатель в референсе, начинать добавку не требуется."
            )
        else:
            action = "retest_only"
            reason = "Недостаточно данных для точной коррекции, сначала повторите/уточните анализ."
        plan.append(
            {
                "nutrient_code": nutrient_code,
                "nutrient_name_ru": str(item.get("canonical_analyte_name_ru") or nutrient_code),
                "status": "attention" if status in {"low", "high"} else "monitor",
                "recommend_action": action,
                "pause_after_course_days": None,
                "cycle_mode": "one_shot",
                "dose_text_ru": "По стандартной схеме из справочника, индивидуализировать с врачом.",
                "frequency_text_ru": "1 раз в день",
                "duration_text_ru": "8–12 недель",
                "timing_text_ru": "Во время еды",
                "with_food": True,
                "reason_ru": reason,
                "monitor_ru": "Отслеживать самочувствие и переносимость.",
                "retest_after_text_ru": "Через 8–12 недель",
                "stop_or_pause_rule_ru": "Остановить при выраженной непереносимости и обсудить с врачом.",
                "cautions_ru": "Требуется персональная проверка взаимодействий и противопоказаний.",
                "interactions_ru": "Проверяйте совместимость с текущими препаратами и добавками.",
            }
        )
        seen_codes.add(nutrient_code)
        if len(plan) >= max(1, int(max_items)):
            break
    return plan


def _build_timeout_fallback_assistant_response(
    labs_for_llm: list[dict],
    *,
    active_stack_codes: set[str] | None = None,
    allowed_nutrient_codes: set[str] | None = None,
) -> dict:
    def _lab_row(item: dict) -> dict:
        status = str(item.get("interpreted_status_pre_llm") or "unclear")
        if status not in {"low", "normal", "high", "unclear"}:
            status = "unclear"
        reason = str(item.get("interpretation_hint_ru") or "").strip() or "Автоматический резервный разбор из-за таймаута AI."
        if status in {"low", "high"}:
            actionability = "actionable"
        elif status == "normal":
            actionability = "watch"
        else:
            actionability = "insufficient_data"
        return {
            "analyte_code": str(item.get("analyte_code") or ""),
            "analyte_name_ru": str(item.get("analyte_name_ru") or ""),
            "value": str(item.get("value") or ""),
            "unit": str(item.get("unit") or ""),
            "ref_low": item.get("ref_low"),
            "ref_high": item.get("ref_high"),
            "interpretation_status": status,
            "mapped_nutrient_code": str(item.get("canonical_nutrient_code") or ""),
            "actionability": actionability,
            "short_reason_ru": reason[:300],
        }

    labs_review = [_lab_row(x) for x in list(labs_for_llm or [])]
    nutrient_plan = _build_fallback_nutrient_plan_from_labs(
        labs_for_llm,
        active_stack_codes=active_stack_codes,
        allowed_nutrient_codes=allowed_nutrient_codes,
        max_items=20,
    )
    return {
        "summary_ru": "AI-ответ временно недоступен по таймауту. Показан резервный безопасный разбор лабораторных показателей с базовым планом по ключевым нутриентам.",
        "confidence": "low",
        "safety_flags": ["timeout_fallback_mode"],
        "physician_flags": ["Перепроверьте рекомендации с врачом перед началом/изменением приёма."],
        "labs_used": sorted({str(x.get("analyte_code") or "") for x in labs_for_llm if str(x.get("analyte_code") or "")}),
        "missing_data": [],
        "nutrient_plan": nutrient_plan,
        "labs_review": labs_review,
        "next_steps_ru": [
            "Попробуйте пересчитать план через 30–60 секунд.",
            "Если таймаут повторяется, оставьте только самые свежие анализы и повторите расчёт.",
        ],
        "disclaimer_ru": "Резервный ответ сформирован автоматически при сетевом таймауте и не заменяет консультацию врача.",
}


def _merge_missing_labs_review(response_json: dict, input_labs: list[dict]) -> dict:
    data = dict(response_json or {})
    review = [dict(x) for x in list(data.get("labs_review") or []) if isinstance(x, dict)]
    reviewed_codes = {str(x.get("analyte_code") or "").strip().lower() for x in review if str(x.get("analyte_code") or "").strip()}
    for item in list(input_labs or []):
        code = str(item.get("analyte_code") or "").strip().lower()
        if not code or code in reviewed_codes:
            continue
        review.append(
            {
                "analyte_code": str(item.get("analyte_code") or ""),
                "analyte_name_ru": str(item.get("analyte_name_ru") or ""),
                "value": str(item.get("value") or ""),
                "unit": str(item.get("unit") or ""),
                "ref_low": item.get("ref_low"),
                "ref_high": item.get("ref_high"),
                "interpretation_status": str(item.get("interpreted_status_pre_llm") or "unclear"),
                "mapped_nutrient_code": str(item.get("canonical_nutrient_code") or ""),
                "actionability": "insufficient_data",
                "short_reason_ru": "Добавлено автоматически для полного покрытия входных анализов.",
            }
        )
    data["labs_review"] = review
    return data


def _fallback_profile_value(assistant_profile: dict, profile: dict, key: str, default: str = "unknown") -> str:
    assistant_value = str((assistant_profile or {}).get(key) or "").strip()
    if assistant_value and assistant_value.lower() != "unknown":
        return assistant_value
    profile_value = str((profile or {}).get(key) or "").strip()
    if profile_value and profile_value.lower() != "na":
        return profile_value
    return default


def _parse_json_list_field(raw: str | None, *, max_items: int = 8, max_len: int = 80) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    out: list[str] = []
    for item in parsed:
        value = str(item).strip()
        if not value:
            continue
        out.append(value[:max_len])
        if len(out) >= max_items:
            break
    return out


def _to_iso_date(raw: str | None) -> str:
    txt = str(raw or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", txt):
        return txt
    return ""


def _compact_labs_for_llm(
    labs: list[dict],
    *,
    max_items: int = 80,
    max_per_analyte: int = 2,
) -> list[dict]:
    # Preserve signal while keeping payload compact:
    # - include up to N latest values per analyte (for trend context),
    # - cap total size to avoid LLM/proxy timeouts.
    grouped: dict[str, list[dict]] = {}
    uncoded: list[dict] = []
    for lab in list(labs or []):
        item = dict(lab or {})
        code = str(item.get("analyte_code") or "").strip()
        if not code:
            uncoded.append(item)
            continue
        grouped.setdefault(code, []).append(item)

    selected: list[dict] = []
    for code, items in grouped.items():
        _ = code
        items.sort(key=lambda x: str(x.get("measured_at") or ""), reverse=True)
        selected.extend(items[: max(1, int(max_per_analyte))])

    selected.extend(uncoded)
    selected.sort(
        key=lambda x: (
            str(x.get("measured_at") or ""),
            str(x.get("analyte_code") or ""),
            str(x.get("analyte_name_ru") or ""),
        ),
        reverse=True,
    )
    return selected[: max(1, int(max_items))]


def _prune_for_llm(value):
    if isinstance(value, dict):
        out: dict = {}
        for k, v in value.items():
            pv = _prune_for_llm(v)
            if pv is None:
                continue
            out[k] = pv
        return out or None
    if isinstance(value, list):
        out_list = [x for x in (_prune_for_llm(v) for v in value) if x is not None]
        return out_list or None
    if value is None:
        return None
    if isinstance(value, str):
        txt = value.strip()
        if not txt:
            return None
        if txt.lower() in {"unknown", "na", "n/a"}:
            return None
        return txt
    return value


def _normalize_assistant_lab_item(item: dict, sex_key: str = "any") -> dict:
    analyte_code = _canonical_analyte_code(str(item.get("analyte_code") or item.get("code") or ""))
    registry_item = ASSISTANT_ANALYTE_REGISTRY.get(analyte_code) or {}
    analyte_name = str(item.get("analyte_name_ru") or item.get("analyte_name") or registry_item.get("name_ru") or analyte_code).strip()
    value_raw = item.get("value_num")
    if value_raw is None:
        value_raw = item.get("value")
    if value_raw is None:
        value_raw = item.get("value_text")
    value_text = str(value_raw if value_raw is not None else "").strip()
    value_num = _safe_float(str(value_text).replace(",", ".")) if value_text else None
    input_ref_low = _safe_float(item.get("ref_low"))
    input_ref_high = _safe_float(item.get("ref_high"))
    unit = str(item.get("unit") or "").strip()
    registry_ref_low, registry_ref_high = _pick_ref_range(registry_item, sex_key, unit)
    ref_source = "unknown"
    ref_low = None
    ref_high = None
    if input_ref_low is not None or input_ref_high is not None:
        ref_low = input_ref_low
        ref_high = input_ref_high
        ref_source = "user"
    elif registry_ref_low is not None or registry_ref_high is not None:
        ref_low = registry_ref_low
        ref_high = registry_ref_high
        ref_source = "registry"
    reference_known = ref_low is not None or ref_high is not None
    below_ref = bool(value_num is not None and ref_low is not None and value_num < ref_low)
    above_ref = bool(value_num is not None and ref_high is not None and value_num > ref_high)
    in_ref = bool(reference_known and value_num is not None and not below_ref and not above_ref)
    return {
        "analyte_code": analyte_code,
        "analyte_name_ru": analyte_name,
        "value": value_text,
        "unit": unit,
        "ref_low": ref_low,
        "ref_high": ref_high,
        "measured_at": _to_iso_date(str(item.get("measured_at") or "")),
        "reference_known": reference_known,
        "below_ref": below_ref,
        "above_ref": above_ref,
        "in_ref": in_ref,
        "reference_source": ref_source,
        "canonical_analyte_name_ru": str(registry_item.get("name_ru") or analyte_name),
        "canonical_nutrient_code": str(registry_item.get("nutrient_code") or ""),
        "interpreted_status_pre_llm": "low" if below_ref else ("high" if above_ref else ("normal" if in_ref else "unclear")),
        "interpretation_hint_ru": "Статус определён по пользовательскому референсу." if ref_source == "user" else ("Статус определён по каноническому референсу справочника." if ref_source == "registry" else "Надёжный референс для этого анализа/единицы не найден."),
    }


def _labs_review_coverage(response_json: dict, input_labs: list[dict]) -> tuple[bool, list[str]]:
    expected = {str(x.get("analyte_code") or "").strip().lower() for x in input_labs if str(x.get("analyte_code") or "").strip()}
    reviewed = {str(x.get("analyte_code") or "").strip().lower() for x in list(response_json.get("labs_review") or []) if isinstance(x, dict)}
    missing = sorted(expected - reviewed)
    return (len(missing) == 0), missing


def _build_nutrient_lab_status_map(labs_for_llm: list[dict]) -> dict[str, str]:
    priority = {"high": 4, "low": 3, "normal": 2, "unclear": 1}
    result: dict[str, str] = {}
    for item in list(labs_for_llm or []):
        nutrient_code = _normalize_nutrient_code(str(item.get("canonical_nutrient_code") or item.get("mapped_nutrient_code") or ""))
        if not nutrient_code:
            continue
        status = str(item.get("interpreted_status_pre_llm") or item.get("interpretation_status") or "unclear").strip().lower()
        if status not in priority:
            status = "unclear"
        prev = str(result.get(nutrient_code) or "unclear")
        if priority[status] > priority.get(prev, 0):
            result[nutrient_code] = status
    return result


def _clamped_action_for_status(nutrient_status: str, in_active_stack: bool) -> tuple[str, str]:
    if nutrient_status == "high":
        return ("stop", "possible_excess") if in_active_stack else ("do_not_start", "possible_excess")
    if nutrient_status == "normal":
        return ("continue", "likely_ok") if in_active_stack else ("do_not_start", "likely_ok")
    if nutrient_status == "low":
        return "start", "likely_low_or_risk"
    return "retest_only", "unclear"


def _apply_lab_action_safety_clamp(parsed: dict, labs_for_llm: list[dict], active_stack_codes: set[str]) -> dict:
    data = dict(parsed or {})
    lab_status_map = _build_nutrient_lab_status_map(labs_for_llm)
    items = list(data.get("nutrient_plan") or [])
    clamped_items: list[dict] = []
    for raw in items:
        item = dict(raw or {})
        nutrient_code = _normalize_nutrient_code(str(item.get("nutrient_code") or ""))
        if not nutrient_code:
            clamped_items.append(item)
            continue
        nutrient_status = lab_status_map.get(nutrient_code, "unclear")
        in_active_stack = nutrient_code in set(active_stack_codes or set())
        forced_action, forced_status = _clamped_action_for_status(nutrient_status, in_active_stack)
        prev_action = str(item.get("recommend_action") or "")
        clamped = prev_action != forced_action
        item["recommend_action"] = forced_action
        item["status"] = forced_status
        item["safety_clamped"] = bool(clamped)
        item["safety_clamp_reason"] = (
            f"lab_status={nutrient_status};in_active_stack={1 if in_active_stack else 0};action:{prev_action}->{forced_action}"
        )
        item["lab_status_final"] = nutrient_status
        if nutrient_status == "high":
            item["reason_ru"] = (
                "Показатель выше референса на фоне текущего приёма — рекомендована остановка."
                if in_active_stack
                else "Показатель выше референса — начинать нутриент сейчас не нужно."
            )
        elif nutrient_status == "normal":
            item["reason_ru"] = (
                "Показатель в референсе, текущий курс можно продолжать под наблюдением."
                if in_active_stack
                else "Показатель в референсе, старт нутриента не требуется."
            )
        elif nutrient_status == "low":
            item["reason_ru"] = "Показатель ниже референса — есть основания для начала курса."
        else:
            item["reason_ru"] = "Недостаточно надёжных данных по анализам, сначала нужен контроль/пересдача."
        clamped_items.append(item)
    data["nutrient_plan"] = clamped_items
    data["nutrient_lab_status_map"] = lab_status_map
    return data


def _extract_reminder_times(frequency_text: str) -> list[str]:
    text = str(frequency_text or "").lower()
    if ("2" in text and "д" in text) or "2x" in text or "дважды" in text:
        return ["09:00", "19:00"]
    if ("3" in text and "д" in text) or "3x" in text or "трижды" in text:
        return ["09:00", "14:00", "20:00"]
    return ["09:00"]


def _safe_int_or_none(value) -> int | None:
    try:
        iv = int(value)
        return iv
    except Exception:
        return None


def _extract_first_int(text: str, *, min_value: int = 1, max_value: int = 3650) -> int | None:
    m = re.search(r"\b(\d{1,4})\b", str(text or ""))
    if not m:
        return None
    value = _safe_int_or_none(m.group(1))
    if value is None:
        return None
    if value < min_value or value > max_value:
        return None
    return value


def _derive_frequency_per_day(frequency_text_ru: str) -> int | None:
    text = str(frequency_text_ru or "").lower()
    if "трижды" in text:
        return 3
    if "дважды" in text:
        return 2
    if "через день" in text:
        return 1
    m = re.search(r"(\d)\s*(?:x|раз[а]?)\s*(?:в\s*д[её]нь|/день)?", text)
    if m:
        value = _safe_int_or_none(m.group(1))
        if value and 1 <= value <= 6:
            return value
    if "ежеднев" in text or "каждый день" in text or "1 раз" in text:
        return 1
    return None


def _derive_timing_context(timing_text_ru: str) -> str | None:
    text = str(timing_text_ru or "").strip().lower()
    if not text:
        return None
    if "натощак" in text:
        return "fasting"
    if "после" in text and "ед" in text:
        return "after_food"
    if "во время" in text and "ед" in text:
        return "with_food"
    if "до" in text and "ед" in text:
        return "before_food"
    if "утр" in text:
        return "morning"
    if "вечер" in text or "ноч" in text:
        return "evening"
    return None


def _derive_course_days(duration_text_ru: str) -> int | None:
    text = str(duration_text_ru or "").lower()
    if not text:
        return None
    value = _extract_first_int(text, min_value=1, max_value=3650)
    if value is None:
        return None
    if any(token in text for token in ("нед", "week")):
        return value * 7
    if any(token in text for token in ("мес", "месяц", "month")):
        return value * 30
    return value


def _derive_followup_kind(action: str) -> str:
    return "followup_review" if action in {"start", "continue"} else "followup_manual"


def enrich_nutrient_plan_items_for_execution(parsed: dict, context: dict | None = None) -> dict:
    data = dict(parsed or {})
    items = list(data.get("nutrient_plan") or [])
    enriched: list[dict] = []
    for raw in items:
        item = dict(raw or {})
        action = str(item.get("recommend_action") or "")
        frequency_text = str(item.get("frequency_text_ru") or "")
        duration_text = str(item.get("duration_text_ru") or "")
        timing_text = str(item.get("timing_text_ru") or "")
        retest_text = str(item.get("retest_after_text_ru") or "")

        frequency_per_day = _derive_frequency_per_day(frequency_text)
        explicit_times = _extract_reminder_times(frequency_text) if frequency_per_day else []
        timing_context = _derive_timing_context(timing_text)
        course_days = _derive_course_days(duration_text)
        retest_after_days = _derive_course_days(retest_text)
        pause_after_course_days = _safe_int_or_none(item.get("pause_after_course_days"))
        pause_days = pause_after_course_days if pause_after_course_days is not None else (max(0, retest_after_days - course_days) if retest_after_days and course_days and retest_after_days > course_days else 0)
        reevaluate_after_days = course_days if action in {"start", "continue"} else None
        auto_stop_at_end = bool(course_days) and action in {"start", "continue"}
        auto_repeat_enabled = action in {"start", "continue"} and auto_stop_at_end and int(pause_days or 0) > 0
        followup_kind = _derive_followup_kind(action)
        followup_message = ""
        if action in {"start", "continue"} and course_days:
            followup_message = f"Пересмотреть план через {course_days} дн."
        elif retest_after_days:
            followup_message = f"Повторить анализы через {retest_after_days} дн."

        item.update(
            {
                "frequency_per_day": frequency_per_day,
                "explicit_times": explicit_times,
                "timing_context": timing_context,
                "course_days": course_days,
                "pause_days": pause_days,
                "auto_stop_at_end": auto_stop_at_end,
                "cycle_mode": "auto_repeat_after_pause" if auto_repeat_enabled else "one_shot",
                "reevaluate_after_days": reevaluate_after_days,
                "retest_after_days": retest_after_days,
                "followup_kind": followup_kind,
                "followup_message": followup_message,
            }
        )
        enriched.append(item)
    data["nutrient_plan"] = enriched
    return data


def _days_since_iso(raw: str | None) -> int | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
        return max(0, int(delta.days))
    except Exception:
        return None


class NutrientAssistantPipelineError(Exception):
    def __init__(self, code: str, stage: str, message: str = "") -> None:
        self.code = str(code or "ASSISTANT_PIPELINE_ERROR")
        self.stage = str(stage or "unknown")
        self.message = str(message or "")
        super().__init__(self.message or self.code)


async def run_nutrient_assistant_compute_pipeline(
    conn,
    *,
    tg_user_id: int,
    assistant_profile: dict | None,
    labs: list[dict] | None,
    assistant_context: dict | None,
    debug_id: str,
) -> dict:
    started_ts = time.monotonic()
    stage_started = started_ts
    stage_times: dict[str, int] = {}
    def _mark(stage_name: str) -> None:
        nonlocal stage_started
        now = time.monotonic()
        stage_times[stage_name] = int((now - stage_started) * 1000)
        stage_started = now

    if not s.OPENAI_API_KEY:
        raise NutrientAssistantPipelineError("OPENAI_DISABLED", "validate_openai")
    stage = "load_profile"
    profile = await db.get_user_health_profile(conn, tg_user_id)
    stack = await db.get_user_supplements_with_enabled(conn, tg_user_id)
    _mark("load_profile")
    sex_key = {"m": "male", "f": "female"}.get(str(profile.get("sex_at_birth") or "").lower(), "any")
    stage = "normalize_labs"
    if labs:
        normalized_labs = [_normalize_assistant_lab_item(dict(x or {}), sex_key=sex_key) for x in list(labs)]
    else:
        latest = await db.list_latest_lab_results(conn, tg_user_id, limit=80)
        normalized_labs = [
            _normalize_assistant_lab_item({
                "analyte_code": str(x.get("analyte_code") or ""),
                "analyte_name_ru": str(x.get("analyte_name") or ""),
                "value": x.get("value_num") if x.get("value_num") is not None else str(x.get("value_text") or ""),
                "unit": str(x.get("unit") or ""),
                "ref_low": x.get("ref_low"),
                "ref_high": x.get("ref_high"),
                "measured_at": _to_iso_date(str(x.get("measured_at") or "")),
            }, sex_key=sex_key)
            for x in latest
        ]

    _mark("normalize_labs")
    labs_for_llm = _compact_labs_for_llm(normalized_labs, max_items=32, max_per_analyte=1)
    labs_count = len(labs_for_llm)
    current_year = int(db.get_user_local_date(None).split("-")[0])
    birth_year = int(profile.get("birth_year") or 0) if str(profile.get("birth_year") or "").isdigit() else None
    age = current_year - birth_year if birth_year and 1900 <= birth_year <= current_year else None
    profile_payload = dict(assistant_profile or {})

    user_profile_payload = {
        "sex_at_birth": {"m": "male", "f": "female", "na": "unknown"}.get(str(profile.get("sex_at_birth") or "").lower(), "unknown"),
        "age": age,
        "height_cm": profile.get("height_cm"),
        "weight_kg": profile.get("weight_kg"),
        "country_code": str(profile.get("country_code") or ""),
        "city": str(profile.get("city") or ""),
        "pregnancy_status": {"yes": "yes", "no": "no", "na": "not_applicable"}.get(str(profile.get("pregnancy_status") or "").lower(), "unknown"),
        "lactation_status": {"yes": "yes", "no": "no", "na": "not_applicable"}.get(str(profile.get("lactation_status") or "").lower(), "unknown"),
        "menstrual_status": str(profile_payload.get("menstrual_status") or "unknown"),
        "heavy_menstrual_bleeding": bool(profile_payload.get("heavy_menstrual_bleeding")),
        "diet_type": _fallback_profile_value(profile_payload, profile, "diet_type"),
        "fish_intake_frequency": str(profile_payload.get("fish_intake_frequency") or "unknown"),
        "meat_intake_frequency": str(profile_payload.get("meat_intake_frequency") or "unknown"),
        "dairy_intake_frequency": str(profile_payload.get("dairy_intake_frequency") or "unknown"),
        "egg_intake_frequency": str(profile_payload.get("egg_intake_frequency") or "unknown"),
        "sun_exposure_level": _fallback_profile_value(profile_payload, profile, "sun_exposure_level"),
        "sunscreen_use": str(profile_payload.get("sunscreen_use") or "unknown"),
        "training_sessions_per_week": profile_payload.get("training_sessions_per_week") or profile.get("training_sessions_per_week"),
        "training_minutes_per_week": profile_payload.get("training_minutes_per_week") or profile.get("training_minutes_per_week"),
        "smoking_status": _fallback_profile_value(profile_payload, profile, "smoking_status"),
        "alcohol_status": _fallback_profile_value(profile_payload, profile, "alcohol_status"),
        "sleep_quality": str(profile_payload.get("sleep_quality") or "unknown"),
        "stress_level": str(profile_payload.get("stress_level") or "unknown"),
        "medications": _parse_json_list_field(profile.get("meds_json")),
        "health_conditions": _parse_json_list_field(profile.get("conditions_json")),
        "gi_problems": list(profile_payload.get("gi_problems") or []),
        "malabsorption_risk": bool(profile_payload.get("malabsorption_risk")),
        "kidney_conditions": list(profile_payload.get("kidney_conditions") or []),
        "liver_conditions": list(profile_payload.get("liver_conditions") or []),
        "thyroid_conditions": list(profile_payload.get("thyroid_conditions") or []),
        "bariatric_or_gi_surgery_history": bool(profile_payload.get("bariatric_or_gi_surgery_history")),
        "allergies_or_restrictions": list(profile_payload.get("allergies_or_restrictions") or []),
        "known_past_deficiencies": list(profile_payload.get("known_past_deficiencies") or []),
        "primary_goal": str(profile_payload.get("primary_goal") or ""),
        "symptoms": list(profile_payload.get("symptoms") or []),
        "symptoms_other_free_text": str(profile_payload.get("symptoms_other_free_text") or ""),
    }
    active_stack_payload = []
    for item in stack:
        if int(item.get("is_active") or 0) != 1:
            continue
        if int(item.get("enabled") or 0) != 1 or int(item.get("user_frozen") or 0) == 1:
            continue
        active_stack_payload.append(
            {
                "supplement_name": str(item.get("name_ru") or item.get("name_en") or item.get("name") or ""),
                "nutrient_code": str(item.get("slug") or ""),
                "dose_text": str(item.get("dose_text") or "").strip(),
                "frequency_text": "daily",
                "started_days_ago": _days_since_iso(str(item.get("created_at") or "")),
            }
        )
    active_stack_codes = {
        _normalize_nutrient_code(str(item.get("nutrient_code") or ""))
        for item in active_stack_payload
        if _normalize_nutrient_code(str(item.get("nutrient_code") or ""))
    }
    stage = "assistant_catalog"
    assistant_nutrient_catalog: list[dict] = []
    missing_catalog_slugs: list[str] = []
    for code in ASSISTANT_ALLOWED_NUTRIENT_CODES:
        row = await db.fetchone(
            conn,
            """SELECT id, slug, COALESCE(NULLIF(name_ru,''), NULLIF(name_en,''), name, slug) AS name_ru
               FROM supplements
               WHERE slug=?
               LIMIT 1""",
            (code,),
        )
        if not row:
            missing_catalog_slugs.append(code)
            continue
        assistant_nutrient_catalog.append(
            {"supplement_id": int(row["id"]), "nutrient_code": str(row["slug"] or ""), "nutrient_name_ru": str(row["name_ru"] or row["slug"] or "")}
        )
    if missing_catalog_slugs:
        raise NutrientAssistantPipelineError("ASSISTANT_NUTRIENT_CATALOG_MISMATCH", stage, ",".join(sorted(missing_catalog_slugs)))
    _mark("assistant_catalog")
    assistant_catalog_by_code = {str(x.get("nutrient_code") or "").strip().lower(): x for x in assistant_nutrient_catalog}
    allowed_nutrient_codes = set(assistant_catalog_by_code.keys())
    llm_payload = _prune_for_llm(
        {
            "user_profile": user_profile_payload,
            "active_stack": active_stack_payload,
            "labs": labs_for_llm,
            "assistant_context": {
                "request_source": "webapp_worker",
                "language": "ru",
                "goal": "build_nutrient_recommendations",
                "prompt_version": ASSISTANT_PROMPT_VERSION,
                "nutrient_catalog": assistant_nutrient_catalog,
                "allowed_nutrient_codes_note": "nutrient_code в nutrient_plan должен быть строго одним из nutrient_catalog.nutrient_code.",
                **dict(assistant_context or {}),
            },
        }
    ) or {}
    assistant_model = (s.OPENAI_NUTRIENT_ASSISTANT_MODEL or "").strip() or s.OPENAI_MODEL
    cfg = OpenAIConfig(api_key=s.OPENAI_API_KEY, model=assistant_model)
    stage = "call_llm_attempt_1"
    used_timeout_fallback = False
    try:
        raw_response, parsed, _ = await _compute_assistant_with_retry(cfg, llm_payload, retries=0, allowed_nutrient_codes=allowed_nutrient_codes)
        _mark("call_llm_attempt_1")
    except (APITimeoutError, APIConnectionError, TimeoutError):
        _mark("call_llm_attempt_1")
        stage = "call_llm_attempt_2"
        try:
            raw_response, parsed, _ = await _compute_assistant_with_retry(
                cfg,
                _timeout_failsafe_payload(llm_payload),
                retries=0,
                allowed_nutrient_codes=allowed_nutrient_codes,
            )
        except (APITimeoutError, APIConnectionError, TimeoutError):
            parsed = None
            raw_response = ""
        _mark("call_llm_attempt_2")
    if not parsed:
        stage = "local_fallback"
        parsed = _build_timeout_fallback_assistant_response(
            labs_for_llm,
            active_stack_codes=active_stack_codes,
            allowed_nutrient_codes=allowed_nutrient_codes,
        )
        raw_response = raw_response if 'raw_response' in locals() else json.dumps(parsed, ensure_ascii=False)
        used_timeout_fallback = True
        _mark("local_fallback")
        log.warning("nutrient assistant fallback used debug_id=%s tg_user_id=%s stage=%s", debug_id, tg_user_id, stage)
    complete_labs, _ = _labs_review_coverage(parsed, labs_for_llm)
    if not complete_labs:
        stage = "merge_missing_labs_review"
        parsed = _merge_missing_labs_review(parsed, labs_for_llm)
        valid_merged, _ = _validate_assistant_response(parsed, allowed_nutrient_codes=allowed_nutrient_codes)
        if not valid_merged:
            parsed = _build_timeout_fallback_assistant_response(
                labs_for_llm,
                active_stack_codes=active_stack_codes,
                allowed_nutrient_codes=allowed_nutrient_codes,
            )
            used_timeout_fallback = True
        _mark("merge_missing_labs_review")
    stage = "safety_clamp"
    parsed = _apply_lab_action_safety_clamp(parsed, labs_for_llm, active_stack_codes)
    _mark("safety_clamp")
    stage = "enrich_execution_fields"
    parsed = enrich_nutrient_plan_items_for_execution(parsed, context={"labs": labs_for_llm, "assistant_profile": profile_payload})
    _mark("enrich_execution_fields")
    stage = "create_run"
    run_id = await db.create_llm_assistant_run(
        conn,
        tg_user_id,
        prompt_version=ASSISTANT_PROMPT_VERSION,
        model_name=assistant_model,
        input_snapshot=llm_payload,
        response_json=parsed,
        status="ok_timeout_fallback" if used_timeout_fallback else "ok",
    )
    _mark("create_run")
    stage = "materialize_actions"
    prepared_actions: list[dict] = []
    unresolved_nutrients: list[dict] = []
    suppressed_actions: list[dict] = []
    seen_action_keys: set[tuple[str, str]] = set()
    lab_status_map = _build_nutrient_lab_status_map(labs_for_llm)
    for item in list(parsed.get("nutrient_plan") or []):
        action = str(item.get("recommend_action") or "")
        if action not in {"start", "continue", "stop"}:
            continue
        nutrient_code = _normalize_nutrient_code(str(item.get("nutrient_code") or ""))
        if not nutrient_code or (nutrient_code, action) in seen_action_keys:
            continue
        nutrient_status = str(lab_status_map.get(nutrient_code) or "unclear")
        in_active_stack = nutrient_code in active_stack_codes
        if nutrient_status == "high" and (action == "start" or (action == "continue" and not in_active_stack)):
            log.warning("nutrient materialization suppressed: high_status action=%s nutrient_code=%s in_stack=%s", action, nutrient_code, in_active_stack)
            suppressed = {"nutrient_code": nutrient_code, "recommend_action": action, "reason": "high_status_guard", "lab_status": nutrient_status}
            suppressed_actions.append(suppressed)
            unresolved_nutrients.append(suppressed)
            continue
        if nutrient_status == "normal" and not in_active_stack and action in {"start", "continue"}:
            log.warning("nutrient materialization suppressed: normal_out_of_stack action=%s nutrient_code=%s", action, nutrient_code)
            suppressed = {"nutrient_code": nutrient_code, "recommend_action": action, "reason": "normal_out_of_stack_guard", "lab_status": nutrient_status}
            suppressed_actions.append(suppressed)
            unresolved_nutrients.append(suppressed)
            continue
        seen_action_keys.add((nutrient_code, action))
        resolved_supp = assistant_catalog_by_code.get(nutrient_code)
        if not resolved_supp:
            unresolved_nutrients.append({"nutrient_code": nutrient_code, "nutrient_name_ru": str(item.get("nutrient_name_ru") or ""), "recommend_action": action})
        prepared_actions.append(
            {
                "nutrient_code": nutrient_code,
                "nutrient_name_ru": str(item.get("nutrient_name_ru") or ""),
                "recommend_action": action,
                "supplement_name": str(item.get("nutrient_name_ru") or ""),
                "dose_text": str(item.get("dose_text_ru") or ""),
                "frequency_text": str(item.get("frequency_text_ru") or item.get("frequency_per_day") or ""),
                "duration_text": str(item.get("duration_text_ru") or item.get("course_days") or ""),
                "timing_text": str(item.get("timing_text_ru") or item.get("timing_context") or ""),
                "stop_or_pause_rule_ru": str(item.get("stop_or_pause_rule_ru") or ""),
                "with_food": bool(item.get("with_food")),
                "reminders": list(item.get("explicit_times") or _extract_reminder_times(str(item.get("frequency_text_ru") or ""))),
                "frequency_per_day": _safe_int_or_none(item.get("frequency_per_day")),
                "explicit_times": list(item.get("explicit_times") or []),
                "timing_context": str(item.get("timing_context") or ""),
                "course_days": _safe_int_or_none(item.get("course_days")),
                "pause_days": _safe_int_or_none(item.get("pause_days")),
                "auto_stop_at_end": bool(item.get("auto_stop_at_end")),
                "reevaluate_after_days": _safe_int_or_none(item.get("reevaluate_after_days")),
                "retest_after_days": _safe_int_or_none(item.get("retest_after_days")),
                "followup_kind": str(item.get("followup_kind") or "followup_review"),
                "followup_message": str(item.get("followup_message") or ""),
                "status": "prepared",
            }
        )
    schema_ok, schema_missing = await db.check_nutrient_assistant_materialization_schema(conn)
    if not schema_ok:
        raise NutrientAssistantPipelineError("ASSISTANT_SCHEMA_MISMATCH", "schema_guard_materialization", ",".join(schema_missing))
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    latest_lab_rows = await db.list_latest_lab_results(conn, tg_user_id, limit=1)
    latest_lab_at_utc = max([str(x.get("created_at_utc") or "") for x in latest_lab_rows] or [""])
    for action in prepared_actions:
        code = str(action.get("nutrient_code") or "")
        supp = await db.find_supplement_by_slug_or_name(conn, code or None, str(action.get("nutrient_name_ru") or "") or None)
        supplement_id = int(supp["id"]) if supp else None
        course_days = int(action.get("course_days") or 0) if str(action.get("course_days") or "").isdigit() else 0
        reevaluate_after_days = int(action.get("reevaluate_after_days") or 0) if str(action.get("reevaluate_after_days") or "").isdigit() else 0
        retest_after_days = int(action.get("retest_after_days") or 0) if str(action.get("retest_after_days") or "").isdigit() else 0
        lifecycle_status = "active" if str(action.get("recommend_action") or "") in {"start", "continue"} else "review_required"
        plan_snapshot = {
            "plan_mode": "assistant_generated",
            "plan_phase": "execution_ready",
            "confidence_level": str(parsed.get("confidence") or "low"),
            "lifecycle_status": lifecycle_status,
            "recommend_action": str(action.get("recommend_action") or ""),
            "supplement_id": supplement_id,
            "course_started_at_utc": now_utc.isoformat() if lifecycle_status == "active" else "",
            "course_ends_at_utc": (now_utc + timedelta(days=course_days)).isoformat() if course_days > 0 else "",
            "pause_days": action.get("pause_days"),
            "review_due_at_utc": (now_utc + timedelta(days=reevaluate_after_days)).isoformat() if reevaluate_after_days > 0 else "",
            "retest_due_at_utc": (now_utc + timedelta(days=retest_after_days)).isoformat() if retest_after_days > 0 else "",
            "auto_stop_at_end": bool(action.get("auto_stop_at_end")),
            "followup_kind": str(action.get("followup_kind") or ""),
            "followup_message": str(action.get("followup_message") or ""),
        }
        preview_snapshot = {"profile_snapshot": profile, "latest_lab_at_utc": latest_lab_at_utc, "stack_updated_at_utc": now_utc.isoformat()}
        plan_run_id = await db.create_nutrient_plan_run(conn, tg_user_id, code, None, preview_snapshot, plan_snapshot)
        action["nutrient_plan_run_id"] = plan_run_id
        followups = []
        if plan_snapshot["review_due_at_utc"]:
            followups.append({"item_type": "followup_review", "title_ru": "Пересмотр плана", "due_date_utc": plan_snapshot["review_due_at_utc"], "details": {"message_ru": plan_snapshot["followup_message"]}})
        if plan_snapshot["retest_due_at_utc"]:
            followups.append({"item_type": "retest", "title_ru": "Повторный анализ", "due_date_utc": plan_snapshot["retest_due_at_utc"], "details": {"message_ru": "Пора повторно сдать анализы."}})
        if followups:
            await db.save_nutrient_followup_items(conn, tg_user_id, plan_run_id, code, followups)
            candidates = [{"candidate_type": str(f["item_type"]), "trigger_date_utc": str(f["due_date_utc"]), "payload": dict(f.get("details") or {})} for f in followups]
            await db.save_nutrient_reminder_candidates(conn, tg_user_id, plan_run_id, code, candidates)
            await db.activate_nutrient_reminders_for_plan(conn, tg_user_id, plan_run_id)
    await db.save_llm_assistant_plan_actions(conn, tg_user_id, run_id, prepared_actions)
    _mark("materialize_actions")
    elapsed_total_ms = int((time.monotonic() - started_ts) * 1000)
    log.info(
        "nutrient pipeline success debug_id=%s tg_user_id=%s run_id=%s fallback_used=%s elapsed_ms_total=%s stage_ms=%s",
        debug_id,
        tg_user_id,
        run_id,
        used_timeout_fallback,
        elapsed_total_ms,
        stage_times,
    )
    return {
        "run_id": run_id,
        "result": parsed,
        "prepared_actions": prepared_actions,
        "missing_supplement_slugs": sorted({str(x.get("nutrient_code") or "") for x in unresolved_nutrients if str(x.get("nutrient_code") or "")}),
        "unresolved_nutrients": unresolved_nutrients,
        "suppressed_actions": suppressed_actions,
        "fallback_used": used_timeout_fallback,
    }
