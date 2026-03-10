from __future__ import annotations

from typing import Any


def _rule_caps(rule: dict[str, Any]) -> dict[str, bool]:
    caps = dict(rule.get("capabilities") or {})
    if not caps:
        planner_mode = str(rule.get("planner_mode") or "")
        explain_mode = str(rule.get("explanation_mode") or "")
        caps = {
            "can_preview": str(rule.get("engine_mode") or "") in {"deterministic_v1", "limited"},
            "can_plan": planner_mode in {"deterministic", "limited"},
            "can_explain": explain_mode in {"full", "limited", "metadata_only"},
            "can_followup": planner_mode in {"deterministic", "limited"},
        }
    return caps


def build_registry_coverage_report(rules: dict[str, dict[str, Any]], validation: dict[str, list[str]] | None = None) -> dict[str, Any]:
    validation = validation or {"errors": [], "warnings": []}
    items = []
    counters = {"deterministic_v1": 0, "limited_interpretation": 0, "metadata_only": 0, "planned": 0}

    for code, rule in sorted(rules.items()):
        readiness = str(rule.get("readiness_level") or "metadata_only")
        counters[readiness] = counters.get(readiness, 0) + 1
        caps = _rule_caps(rule)
        warnings: list[str] = []
        errors: list[str] = []
        if not (rule.get("supported_analytes") or []):
            warnings.append("no_supported_analytes")
        if not (rule.get("primary_biomarkers") or []):
            warnings.append("no_primary_biomarkers")
        if not (rule.get("unit_conversion_rules") or {}):
            warnings.append("no_unit_conversion_rules")
        if not (rule.get("profile_trigger_fields") or []):
            warnings.append("no_profile_triggers")
        if not (rule.get("stack_match_slugs") or []):
            warnings.append("no_stack_mapping")
        if not (rule.get("timing_hints") or []):
            warnings.append("no_planner_timing_hints")
        if not str(rule.get("ui_notes_ru") or "").strip():
            warnings.append("no_ui_notes")
        if readiness == "deterministic_v1" and not rule.get("interpretation_thresholds"):
            errors.append("deterministic_without_thresholds")

        items.append(
            {
                "nutrient_code": code,
                "category": rule.get("category") or "other",
                "readiness_level": readiness,
                "has_preview_support": bool(caps.get("can_preview")),
                "has_plan_support": bool(caps.get("can_plan")),
                "has_explain_support": bool(caps.get("can_explain")),
                "has_followup_support": bool(caps.get("can_followup")),
                "has_supported_analytes": bool(rule.get("supported_analytes")),
                "has_primary_biomarkers": bool(rule.get("primary_biomarkers")),
                "has_unit_rules": bool(rule.get("unit_conversion_rules")),
                "has_profile_triggers": bool(rule.get("profile_trigger_fields")),
                "has_stack_mappings": bool(rule.get("stack_match_slugs")),
                "has_planner_templates": bool(rule.get("plan_templates") or rule.get("timing_hints")),
                "has_ui_notes": bool(str(rule.get("ui_notes_ru") or "").strip()),
                "warnings": warnings,
                "errors": errors,
            }
        )

    alias_conflicts = [x for x in (validation.get("warnings") or []) if "alias conflict" in str(x)]
    return {
        "summary": {
            "total_nutrients": len(items),
            "full": counters.get("deterministic_v1", 0),
            "limited": counters.get("limited_interpretation", 0),
            "metadata_only": counters.get("metadata_only", 0),
            "planned": counters.get("planned", 0),
            "validation_errors": len(validation.get("errors") or []),
            "validation_warnings": len(validation.get("warnings") or []),
            "alias_conflicts": len(alias_conflicts),
            "missing_unit_rules": sum(1 for i in items if not i["has_unit_rules"]),
            "missing_impact_mapping": sum(1 for i in items if not i["has_profile_triggers"] and not i["has_stack_mappings"]),
            "missing_planner_metadata": sum(1 for i in items if not i["has_planner_templates"]),
            "disabled_due_to_validation": 0,
        },
        "items": items,
        "validation": validation,
    }


def build_pack_qc_report(packs: list[dict[str, Any]], merged_rules: dict[str, dict[str, Any]], validation: dict[str, list[str]] | None = None) -> dict[str, Any]:
    coverage = build_registry_coverage_report(merged_rules, validation=validation)
    pack_items = []
    for p in packs:
        pack_items.append(
            {
                "pack_id": str(p.get("pack_id") or ""),
                "pack_version": str(p.get("pack_version") or ""),
                "pack_category": str(p.get("pack_category") or ""),
                "items_count": len(p.get("items") or []),
                "curation_status": str(p.get("curation_status") or "draft"),
                "source_label": str(p.get("source_label") or ""),
                "loaded": True,
            }
        )
    coverage["packs"] = pack_items
    coverage["summary"]["packs_count"] = len(pack_items)
    return coverage
