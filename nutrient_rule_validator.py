from __future__ import annotations

from typing import Any

from nutrient_rule_schema import (
    ENGINE_MODES,
    EXPLAIN_MODES,
    PLANNER_MODES,
    READINESS_LEVELS,
    STATUS_LEVELS,
)


def validate_registry_rules(rules: dict[str, dict[str, Any]], *, strict: bool = False) -> dict[str, list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    seen_alias: dict[str, str] = {}

    for code, rule in rules.items():
        if not code:
            errors.append("empty nutrient_code")
            continue
        if str(rule.get("nutrient_code") or "") != code:
            errors.append(f"{code}: nutrient_code mismatch")
        if not str(rule.get("title_ru") or "").strip():
            errors.append(f"{code}: missing title_ru")
        if str(rule.get("status_level") or "") not in STATUS_LEVELS:
            errors.append(f"{code}: invalid status_level")
        if str(rule.get("engine_mode") or "") not in ENGINE_MODES:
            errors.append(f"{code}: invalid engine_mode")
        if str(rule.get("planner_mode") or "") not in PLANNER_MODES:
            errors.append(f"{code}: invalid planner_mode")
        if str(rule.get("explanation_mode") or "") not in EXPLAIN_MODES:
            errors.append(f"{code}: invalid explanation_mode")
        if str(rule.get("readiness_level") or "") not in READINESS_LEVELS:
            errors.append(f"{code}: invalid readiness_level")

        if not str(rule.get("source_pack_id") or ""):
            warnings.append(f"{code}: missing source_pack_id")
        if not str(rule.get("source_pack_version") or ""):
            warnings.append(f"{code}: missing source_pack_version")

        aliases = rule.get("analyte_aliases") or {}
        if not isinstance(aliases, dict):
            errors.append(f"{code}: analyte_aliases must be dict")
            aliases = {}
        for canonical, vals in aliases.items():
            if not isinstance(vals, list):
                errors.append(f"{code}: analyte_aliases[{canonical}] must be list")
                continue
            for alias in vals + [canonical]:
                key = str(alias or "").strip().lower()
                if not key:
                    continue
                prev = seen_alias.get(key)
                if prev and prev != canonical:
                    warnings.append(f"{code}: analyte alias conflict '{key}' ({prev} vs {canonical})")
                seen_alias[key] = str(canonical)

        conversions = rule.get("unit_conversion_rules") or {}
        if not isinstance(conversions, dict):
            errors.append(f"{code}: unit_conversion_rules must be dict")
        else:
            for pair, factor in conversions.items():
                if not isinstance(pair, str) or "->" not in pair:
                    errors.append(f"{code}: invalid conversion key '{pair}'")
                try:
                    float(factor)
                except Exception:
                    errors.append(f"{code}: invalid conversion factor '{pair}'")

        if rule.get("engine_mode") == "deterministic_v1":
            if not rule.get("primary_biomarkers"):
                errors.append(f"{code}: deterministic_v1 requires primary_biomarkers")
            if not rule.get("interpretation_thresholds") and code in {"vitamin_d", "vitamin_b12", "iron", "folate"}:
                errors.append(f"{code}: deterministic_v1 requires thresholds")

        if rule.get("engine_mode") == "metadata_only" and rule.get("planner_mode") == "deterministic":
            errors.append(f"{code}: metadata_only cannot use deterministic planner")

    if strict and errors:
        raise RuntimeError("Registry validation failed: " + "; ".join(errors[:8]))
    return {"errors": errors, "warnings": warnings}
