from __future__ import annotations

from typing import Any

RULES_VERSION = "nutrient_rules_v2"
REGISTRY_VERSION = "nutrient_registry_v2"

READINESS_LEVELS = {"deterministic_v1", "limited_interpretation", "metadata_only", "planned"}
ENGINE_MODES = {"deterministic_v1", "limited", "metadata_only"}
PLANNER_MODES = {"deterministic", "limited", "metadata_only"}
EXPLAIN_MODES = {"full", "limited", "metadata_only"}
STATUS_LEVELS = {"strong", "medium", "weak"}


def rule_defaults(code: str, title_ru: str) -> dict[str, Any]:
    return {
        "nutrient_code": code,
        "title_ru": title_ru,
        "category": "other",
        "enabled": True,
        "status_level": "weak",
        "engine_mode": "limited",
        "planner_mode": "limited",
        "explanation_mode": "limited",
        "readiness_level": "limited_interpretation",
        "rules_version": RULES_VERSION,
        "registry_version": REGISTRY_VERSION,
        "source_pack_id": "",
        "source_pack_version": "",
        "curation_status": "draft",
        "last_reviewed_at_utc": "",
        "notes_internal": "",
        "primary_biomarkers": [],
        "secondary_biomarkers": [],
        "supported_analytes": [],
        "analyte_aliases": {},
        "supported_units": {},
        "canonical_units": {},
        "unit_conversion_rules": {},
        "confidence_ceiling": "medium",
        "confidence_logic": "limited_mode",
        "interpretation_thresholds": {},
        "missing_input_rules": ["limited_evidence"],
        "unusable_input_rules": [],
        "physician_flag_rules": [],
        "safety_flag_rules": [],
        "profile_modifiers": [],
        "context_modifiers": [],
        "seasonal_relevance": "neutral",
        "geo_relevance": "neutral",
        "stack_match_slugs": [],
        "stack_aliases": [],
        "stack_flags_logic": "simple_match",
        "plan_templates": {},
        "retest_windows": {},
        "pause_windows": {},
        "followup_templates": [],
        "reminder_candidate_templates": [],
        "timing_hints": [],
        "format_hints": [],
        "intake_notes": ["Planner не назначает персональную дозировку."],
        "ui_notes_ru": "",
        "limitation_note_ru": "",
        "trust_copy_ru": "Это информационный инструмент и не заменяет консультацию врача.",
        "dashboard_copy_ru": "",
        "history_label_ru": title_ru,
        "profile_trigger_fields": [],
        "lab_trigger_analytes": [],
        "stack_trigger_slugs": [],
    }


def apply_rule_schema(raw_rule: dict[str, Any]) -> dict[str, Any]:
    code = str(raw_rule.get("nutrient_code") or "").strip().lower()
    title = str(raw_rule.get("title_ru") or code)
    base = rule_defaults(code, title)
    out = {**base, **(raw_rule or {})}
    out["nutrient_code"] = code
    out["title_ru"] = title

    for key in (
        "primary_biomarkers",
        "secondary_biomarkers",
        "supported_analytes",
        "profile_modifiers",
        "context_modifiers",
        "stack_match_slugs",
        "stack_aliases",
        "missing_input_rules",
        "unusable_input_rules",
        "physician_flag_rules",
        "safety_flag_rules",
        "followup_templates",
        "reminder_candidate_templates",
        "timing_hints",
        "format_hints",
        "intake_notes",
        "profile_trigger_fields",
        "lab_trigger_analytes",
        "stack_trigger_slugs",
    ):
        val = out.get(key)
        out[key] = list(val) if isinstance(val, list) else []

    for key in (
        "analyte_aliases",
        "supported_units",
        "canonical_units",
        "unit_conversion_rules",
        "interpretation_thresholds",
        "plan_templates",
        "retest_windows",
        "pause_windows",
    ):
        val = out.get(key)
        out[key] = dict(val) if isinstance(val, dict) else {}

    if out["engine_mode"] == "deterministic_v1":
        out["readiness_level"] = "deterministic_v1"
    elif out["engine_mode"] == "metadata_only":
        out["readiness_level"] = "metadata_only"

    if not out["supported_analytes"]:
        out["supported_analytes"] = sorted(set(out["primary_biomarkers"] + out["secondary_biomarkers"]))

    return out
