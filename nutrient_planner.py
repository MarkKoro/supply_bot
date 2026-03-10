from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from nutrient_registry import get_nutrient_rule
from nutrient_rule_schema import RULES_VERSION

SUPPORTED_V1 = {"vitamin_d", "vitamin_b12", "iron", "folate", "zinc"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _due_iso(days: int | None) -> str:
    if not days:
        return ""
    return (datetime.now(timezone.utc) + timedelta(days=int(days))).replace(microsecond=0).isoformat()


def _confidence_bucket(level: str) -> str:
    v = str(level or "").lower()
    if v in {"high", "medium", "low"}:
        return v
    if v in {"unsupported", "unknown"}:
        return "low"
    return "low"


def derive_plan_phase(preview: dict[str, Any]) -> str:
    status = str(preview.get("interpreted_status") or "")
    mode = str(preview.get("engine_mode") or "limited")
    if mode == "limited":
        return "limited_interpretation"
    if status in {"insufficient_data", "unknown_or_limited"}:
        return "insufficient_data"
    if status in {"very_high_urgent_flag", "discordant_review"}:
        return "physician_review"
    if status in {"high_caution"}:
        return "hold_and_recheck"
    if status in {
        "low",
        "borderline_low",
        "likely_low_confirmed",
        "very_low_iron_stores",
        "low_iron_stores",
        "borderline_or_low_signal",
    }:
        return "correction_phase"
    if status in {"adequate_for_most", "adequate_signal", "adequate_long_term_signal", "stores_not_low_by_ferritin"}:
        return "maintenance_phase"
    return "initial_review"


def derive_action_strategy(preview: dict[str, Any], plan_mode: str) -> str:
    if plan_mode in {"no_plan", "limited_mode_plan"}:
        return "collect_more_data"
    if plan_mode == "physician_review_plan":
        return "physician_review_recommended"
    if plan_mode == "hold_plan":
        return "hold_current_nutrient_related_stack"
    if plan_mode == "correction_plan":
        return "consider_correction_course"
    if plan_mode == "maintenance_plan":
        if preview.get("current_stack_matches"):
            return "continue_current_stack_and_monitor"
        return "consider_maintenance_support"
    if plan_mode == "supportive_plan":
        if preview.get("current_stack_matches"):
            return "review_current_stack"
        return "collect_more_data"
    return "collect_more_data"


def derive_retest_window(preview: dict[str, Any], nutrient_code: str, plan_mode: str) -> tuple[int | None, int | None]:
    status = str(preview.get("interpreted_status") or "")
    if nutrient_code == "vitamin_d":
        if status in {"low", "borderline_low"}:
            return 56, 84
        if status == "high_caution":
            return 21, 42
        if status == "very_high_urgent_flag":
            return 7, 21
        return 84, 140
    if nutrient_code == "vitamin_b12":
        if status in {"low", "likely_low_confirmed"}:
            return 42, 70
        if status == "borderline_needs_confirmation":
            return 28, 56
        return 70, 120
    if nutrient_code == "iron":
        if status == "very_low_iron_stores":
            return 42, 56
        if status == "low_iron_stores":
            return 42, 70
        if status == "high_caution":
            return 21, 42
        return 70, 120
    if nutrient_code == "folate":
        if status == "borderline_or_low_signal":
            return 42, 84
        return 70, 120
    if nutrient_code == "zinc":
        return 70, 120
    if plan_mode == "limited_mode_plan":
        return None, None
    return 56, 84


def derive_pause_window(plan_mode: str) -> tuple[int | None, int | None]:
    if plan_mode in {"correction_plan", "maintenance_plan", "supportive_plan"}:
        return 14, 30
    if plan_mode in {"hold_plan", "physician_review_plan"}:
        return 7, 21
    return None, None


def build_timing_hints(nutrient_code: str) -> list[dict[str, Any]]:
    if nutrient_code == "vitamin_d":
        return [{"with_food": True, "meal_context": "fat_containing_meal_preferred"}]
    if nutrient_code == "iron":
        return [{"separate_from": ["calcium_like_items", "tea_coffee"], "empty_stomach_preferred_if_tolerated": True}]
    if nutrient_code == "vitamin_b12":
        return [{"time_of_day": "flexible", "consistency": "daily_or_as_directed"}]
    if nutrient_code == "zinc":
        return [{"avoid_overlap_with": ["iron", "calcium_like_items"], "take_with_food_if_sensitive": True}]
    return [{"time_of_day": "flexible"}]


def build_format_hints(nutrient_code: str) -> list[dict[str, Any]]:
    if nutrient_code == "vitamin_b12":
        return [{"possible_formats": ["oral", "sublingual", "injectable_review_with_physician"], "note": "без выбора персональной дозы"}]
    if nutrient_code == "vitamin_d":
        return [{"possible_formats": ["oil_capsule", "drops"], "note": "подбор дозы вне planner v1"}]
    if nutrient_code == "iron":
        return [{"possible_formats": ["gentle_iron", "standard_oral_iron"], "note": "учитывать переносимость ЖКТ"}]
    return [{"possible_formats": ["standard_oral"], "note": "консервативный формат"}]


def derive_followup_items(plan: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    mn, mx = plan.get("retest_window_days_min"), plan.get("retest_window_days_max")
    if mn and mx:
        out.append({
            "item_type": "retest_lab",
            "title_ru": f"Пересдать ключевой анализ через {mn}–{mx} дней",
            "details": {"window_days_min": mn, "window_days_max": mx},
            "recommended_window_days": {"min": mn, "max": mx},
            "due_date_utc": _due_iso(mx),
        })
    if plan.get("current_stack_matches"):
        out.append({
            "item_type": "review_stack",
            "title_ru": "Проверить текущий стек и актуальность приёма",
            "details": {"matches": plan.get("current_stack_matches")},
            "recommended_window_days": {"min": 7, "max": 21},
            "due_date_utc": _due_iso(14),
        })
    missing = list(plan.get("missing_inputs") or [])
    if missing:
        out.append({
            "item_type": "collect_missing_input",
            "title_ru": "Добавить недостающие анализы/контекст",
            "details": {"missing_inputs": missing},
            "recommended_window_days": {"min": 1, "max": 21},
            "due_date_utc": _due_iso(14),
        })
    if plan.get("plan_mode") == "physician_review_plan":
        out.append({
            "item_type": "physician_review",
            "title_ru": "Обсудить результат с врачом",
            "details": {"physician_flags": plan.get("physician_flags") or []},
            "recommended_window_days": {"min": 1, "max": 7},
            "due_date_utc": _due_iso(7),
        })
    if plan.get("nutrient_code") == "vitamin_d" and any(
        x in (plan.get("missing_inputs") or []) for x in ["consider_followup_due_to_low_sun_exposure"]
    ):
        out.append({
            "item_type": "seasonal_followup",
            "title_ru": "Сезонный follow-up по витамину D",
            "details": {"reason": "низкая инсоляция"},
            "recommended_window_days": {"min": 90, "max": 180},
            "due_date_utc": _due_iso(120),
        })
    if plan.get("pause_window_days_max"):
        out.append({
            "item_type": "pause_review",
            "title_ru": "Оценить паузу/пересмотр после фазы",
            "details": {"pause_days_max": plan.get("pause_window_days_max")},
            "recommended_window_days": {"min": plan.get("pause_window_days_min"), "max": plan.get("pause_window_days_max")},
            "due_date_utc": _due_iso(plan.get("pause_window_days_max")),
        })
    return out


def derive_reminder_candidates(plan: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if plan.get("retest_window_days_max"):
        out.append({"candidate_type": "retest_due", "trigger_date_utc": _due_iso(plan.get("retest_window_days_max")), "payload": {"nutrient_code": plan.get("nutrient_code")}})
    if plan.get("pause_window_days_max"):
        out.append({"candidate_type": "pause_due", "trigger_date_utc": _due_iso(plan.get("pause_window_days_max")), "payload": {"nutrient_code": plan.get("nutrient_code")}})
    if plan.get("nutrient_code") == "vitamin_d" and any(i.get("item_type") == "seasonal_followup" for i in plan.get("followup_items") or []):
        out.append({"candidate_type": "seasonal_check", "trigger_date_utc": _due_iso(120), "payload": {"reason": "low_sun_exposure"}})
    out.append({"candidate_type": "followup_review", "trigger_date_utc": _due_iso(14), "payload": {"plan_mode": plan.get("plan_mode")}})
    return out


def build_plan_summary(plan: dict[str, Any]) -> list[str]:
    lines = [
        f"Статус: {plan.get('interpreted_status')}",
        f"Режим плана: {plan.get('plan_mode')}",
        f"Фаза: {plan.get('plan_phase')}",
        f"Стратегия: {plan.get('action_strategy')}",
    ]
    if plan.get("retest_window_days_min") and plan.get("retest_window_days_max"):
        lines.append(f"Окно пересдачи: {plan.get('retest_window_days_min')}–{plan.get('retest_window_days_max')} дней")
    if plan.get("missing_inputs"):
        lines.append(f"Нужны доп. данные: {len(plan.get('missing_inputs') or [])}")
    return lines


def _derive_mode_for_v1(nutrient_code: str, preview: dict[str, Any]) -> str:
    status = str(preview.get("interpreted_status") or "")
    stack = bool(preview.get("current_stack_matches"))
    physician_flags = list(preview.get("physician_flags") or [])
    prof = preview.get("profile_snapshot") or {}

    if status in {"very_high_urgent_flag", "discordant_review"}:
        return "physician_review_plan"
    if status == "high_caution":
        return "physician_review_plan" if physician_flags else "hold_plan"

    if nutrient_code == "vitamin_d":
        if status in {"low", "borderline_low"}:
            return "correction_plan"
        if status == "adequate_for_most":
            if stack:
                return "maintenance_plan"
            if str(prof.get("sun_exposure_level") or "") in {"low", "minimal", "низкий"}:
                return "supportive_plan"
            return "observe_only"
    elif nutrient_code == "vitamin_b12":
        if status in {"low", "likely_low_confirmed"}:
            return "correction_plan"
        if status == "borderline_needs_confirmation":
            return "supportive_plan" if stack else "observe_only"
    elif nutrient_code == "iron":
        if status in {"very_low_iron_stores", "low_iron_stores"}:
            return "correction_plan"
        if status == "stores_not_low_by_ferritin":
            return "observe_only"
    elif nutrient_code == "folate":
        if status == "borderline_or_low_signal":
            b12_unknown = "check_b12_before_confident_folate_interpretation" in physician_flags
            return "supportive_plan" if b12_unknown else "correction_plan"
        if status in {"adequate_signal", "adequate_long_term_signal"}:
            return "observe_only"
    elif nutrient_code == "zinc":
        if status == "cautious_signal_only":
            return "supportive_plan"
        return "observe_only"

    if status in {"insufficient_data", "unknown_or_limited"}:
        return "observe_only"
    return "supportive_plan"


def build_plan_from_preview(preview: dict[str, Any]) -> dict[str, Any]:
    code = str(preview.get("nutrient_code") or "").strip().lower()
    rule = get_nutrient_rule(code) or {}
    supported = bool(preview.get("supported"))
    engine_mode = str(preview.get("engine_mode") or rule.get("engine_mode") or "limited")
    confidence = _confidence_bucket(str(preview.get("confidence_level") or "low"))

    if (not supported) or engine_mode == "limited" or code not in SUPPORTED_V1:
        plan_mode = "limited_mode_plan"
    else:
        plan_mode = _derive_mode_for_v1(code, preview)

    plan_phase = derive_plan_phase(preview)
    if plan_mode == "limited_mode_plan":
        plan_phase = "limited_interpretation"
    if plan_mode == "physician_review_plan":
        plan_phase = "physician_review"

    action_strategy = derive_action_strategy(preview, plan_mode)
    retest_map = rule.get("retest_windows") or {}
    pause_map = rule.get("pause_windows") or {}
    if plan_mode in retest_map and isinstance(retest_map.get(plan_mode), (list, tuple)) and len(retest_map.get(plan_mode)) == 2:
        retest_min, retest_max = int(retest_map[plan_mode][0]), int(retest_map[plan_mode][1])
    else:
        retest_min, retest_max = derive_retest_window(preview, code, plan_mode)
    if plan_mode in pause_map and isinstance(pause_map.get(plan_mode), (list, tuple)) and len(pause_map.get(plan_mode)) == 2:
        pause_min, pause_max = int(pause_map[plan_mode][0]), int(pause_map[plan_mode][1])
    else:
        pause_min, pause_max = derive_pause_window(plan_mode)

    plan: dict[str, Any] = {
        "nutrient_code": code,
        "title_ru": preview.get("title_ru") or code,
        "supported": supported,
        "engine_mode": engine_mode,
        "plan_mode": plan_mode,
        "confidence_level": confidence,
        "interpreted_status": preview.get("interpreted_status") or "unknown_or_limited",
        "plan_phase": plan_phase,
        "action_strategy": action_strategy,
        "goal_type": "stabilize_or_confirm_status",
        "current_context": preview.get("profile_snapshot") or {},
        "current_stack_matches": preview.get("current_stack_matches") or [],
        "current_stack_flags": preview.get("current_stack_flags") or [],
        "recommended_monitoring": ["retest_lab", "stack_review"],
        "retest_window_days_min": retest_min,
        "retest_window_days_max": retest_max,
        "pause_window_days_min": pause_min,
        "pause_window_days_max": pause_max,
        "timing_hints": (rule.get("timing_hints") or build_timing_hints(code)),
        "format_hints": (rule.get("format_hints") or build_format_hints(code)),
        "intake_notes": (rule.get("intake_notes") or ["Planner v1 не назначает персональную дозировку."]),
        "data_quality_notes": [preview.get("ui_notes_ru") or ""] if preview.get("ui_notes_ru") else [],
        "missing_inputs": preview.get("missing_inputs") or [],
        "physician_flags": preview.get("physician_flags") or [],
        "safety_flags": preview.get("safety_flags") or [],
        "followup_items": [],
        "reminder_candidates": [],
        "ui_summary_lines": [],
        "created_at_utc": _utc_now_iso(),
        "rules_version": str(preview.get("rules_version") or rule.get("rules_version") or RULES_VERSION),
        "readiness_level": str(preview.get("readiness_level") or rule.get("readiness_level") or "limited_interpretation"),
    }

    # nutrient-specific conservative adjustments
    if code == "zinc" and plan["confidence_level"] == "high":
        plan["confidence_level"] = "medium"
    if code == "folate" and "check_b12_before_confident_folate_interpretation" in plan["physician_flags"]:
        plan["confidence_level"] = "low"

    plan["followup_items"] = derive_followup_items(plan)
    plan["reminder_candidates"] = derive_reminder_candidates(plan)
    plan["ui_summary_lines"] = build_plan_summary(plan)
    return plan
