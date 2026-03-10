from __future__ import annotations

from typing import Any

from nutrient_pack_loader import (
    build_registry_from_packs,
    load_all_nutrient_packs,
)
from nutrient_pack_qc import build_pack_qc_report, build_registry_coverage_report
from nutrient_rule_schema import REGISTRY_VERSION, RULES_VERSION, apply_rule_schema
from nutrient_rule_validator import validate_registry_rules
from nutrient_rules import (
    ANALYTE_ALIASES,
    LAB_ANALYTE_IMPACT_MAP,
    NUTRIENT_PROTOCOL_DEFS,
    NUTRIENT_RULE_PROFILES,
    PROFILE_IMPACT_MAP,
    UNIT_CONVERSIONS,
)

_REGISTRY_CACHE: dict[str, dict[str, Any]] | None = None
_REGISTRY_VALIDATION: dict[str, list[str]] = {"errors": [], "warnings": []}
_REGISTRY_META: dict[str, Any] = {}
_PACKS_CACHE: list[dict[str, Any]] = []


def _compose_registry_fallback() -> dict[str, dict[str, Any]]:
    rules: dict[str, dict[str, Any]] = {}
    by_code = {str(x.get("nutrient_code") or "").lower(): x for x in NUTRIENT_PROTOCOL_DEFS}

    for code, rule in (NUTRIENT_RULE_PROFILES or {}).items():
        c = str(code or "").lower()
        proto = by_code.get(c) or {}
        md = proto.get("metadata") or {}
        merged = dict(rule or {})
        merged.setdefault("nutrient_code", c)
        merged.setdefault("title_ru", proto.get("title_ru") or c)
        merged.setdefault("category", proto.get("category") or "other")
        merged.setdefault("enabled", True)
        merged.setdefault("status_level", proto.get("status_level") or "weak")
        merged.setdefault("planner_mode", "deterministic" if merged.get("engine_mode") == "deterministic_v1" else "limited")
        merged.setdefault("explanation_mode", "full" if merged.get("engine_mode") == "deterministic_v1" else "limited")
        merged.setdefault("supported_analytes", list(md.get("supported_analytes") or merged.get("lab_requirements") or []))
        merged.setdefault("analyte_aliases", {k: list(v) for k, v in ANALYTE_ALIASES.items()})
        merged.setdefault("unit_conversion_rules", {f"{a}->{b}": v for (a, b), v in UNIT_CONVERSIONS.items()})
        merged.setdefault("confidence_ceiling", "high" if merged.get("engine_mode") == "deterministic_v1" else "medium")
        merged.setdefault("limitation_note_ru", str(md.get("limitation_note_ru") or ""))
        merged.setdefault("trust_copy_ru", "Помощник не ставит диагнозы и не заменяет очную консультацию врача.")
        merged.setdefault("dashboard_copy_ru", str((md.get("ui_confidence_note_ru") or "").strip()))
        merged.setdefault("history_label_ru", merged.get("title_ru") or c)
        merged.setdefault("profile_trigger_fields", list(merged.get("profile_modifiers") or []))
        merged.setdefault("lab_trigger_analytes", list(merged.get("supported_analytes") or []))
        merged.setdefault("stack_trigger_slugs", list(merged.get("stack_match_slugs") or []))
        merged.setdefault("readiness_level", "deterministic_v1" if merged.get("engine_mode") == "deterministic_v1" else "limited_interpretation")
        merged.setdefault("rules_version", RULES_VERSION)
        merged.setdefault("registry_version", REGISTRY_VERSION)
        merged.setdefault("source_pack_id", "legacy_fallback")
        merged.setdefault("source_pack_version", "legacy")
        merged.setdefault("curation_status", "draft")
        rules[c] = apply_rule_schema(merged)

    for item in NUTRIENT_PROTOCOL_DEFS:
        code = str(item.get("nutrient_code") or "").lower()
        if code in rules:
            continue
        md = item.get("metadata") or {}
        rules[code] = apply_rule_schema(
            {
                "nutrient_code": code,
                "title_ru": str(item.get("title_ru") or code),
                "category": str(item.get("category") or "other"),
                "enabled": True,
                "status_level": str(item.get("status_level") or "weak"),
                "engine_mode": "metadata_only",
                "planner_mode": "metadata_only",
                "explanation_mode": "metadata_only",
                "readiness_level": "metadata_only",
                "primary_biomarkers": list(md.get("supported_analytes") or []),
                "supported_analytes": list(md.get("supported_analytes") or []),
                "analyte_aliases": {k: list(v) for k, v in ANALYTE_ALIASES.items()},
                "unit_conversion_rules": {f"{a}->{b}": v for (a, b), v in UNIT_CONVERSIONS.items()},
                "ui_notes_ru": str(md.get("limitation_note_ru") or ""),
                "limitation_note_ru": str(md.get("limitation_note_ru") or ""),
                "dashboard_copy_ru": "Доступен справочный режим без полноценной интерпретации.",
                "lab_trigger_analytes": list(md.get("supported_analytes") or []),
                "rules_version": RULES_VERSION,
                "registry_version": REGISTRY_VERSION,
                "source_pack_id": "legacy_fallback",
                "source_pack_version": "legacy",
                "curation_status": "draft",
            }
        )
    return rules


def _with_capabilities(rule: dict[str, Any]) -> dict[str, Any]:
    out = dict(rule)
    planner_mode = str(out.get("planner_mode") or "")
    explain_mode = str(out.get("explanation_mode") or "")
    out["capabilities"] = {
        "can_preview": str(out.get("engine_mode") or "") in {"deterministic_v1", "limited"},
        "can_plan": planner_mode in {"deterministic", "limited"},
        "can_explain": explain_mode in {"full", "limited", "metadata_only"},
        "can_followup": planner_mode in {"deterministic", "limited"},
    }
    return out


def load_registry(*, strict: bool = False, force_reload: bool = False) -> dict[str, dict[str, Any]]:
    global _REGISTRY_CACHE, _REGISTRY_VALIDATION, _REGISTRY_META, _PACKS_CACHE
    if _REGISTRY_CACHE is None or force_reload:
        packs = load_all_nutrient_packs()
        _PACKS_CACHE = packs
        try:
            built, meta = build_registry_from_packs(fail_on_conflict=True)
            source = "packs"
        except Exception:
            built = _compose_registry_fallback()
            meta = {"packs_count": len(packs), "pack_ids": [str(x.get("pack_id") or "") for x in packs], "conflicts": ["fallback_used"]}
            source = "fallback"
        built = {k: _with_capabilities(v) for k, v in built.items()}
        _REGISTRY_VALIDATION = validate_registry_rules(built, strict=strict)
        _REGISTRY_META = {**meta, "source": source}
        _REGISTRY_CACHE = built
    return _REGISTRY_CACHE


def get_registry_validation() -> dict[str, list[str]]:
    load_registry(strict=False)
    return _REGISTRY_VALIDATION


def get_all_nutrient_rules() -> dict[str, dict[str, Any]]:
    return load_registry(strict=False)


def get_nutrient_rule(nutrient_code: str) -> dict[str, Any] | None:
    return get_all_nutrient_rules().get(str(nutrient_code or "").strip().lower())


def get_enabled_nutrients() -> list[dict[str, Any]]:
    return [x for x in get_all_nutrient_rules().values() if bool(x.get("enabled", True))]


def get_registry_meta() -> dict[str, Any]:
    load_registry(strict=False)
    return dict(_REGISTRY_META)


def list_loaded_packs() -> list[dict[str, Any]]:
    load_registry(strict=False)
    out = []
    for pack in _PACKS_CACHE:
        out.append(
            {
                "pack_id": str(pack.get("pack_id") or ""),
                "pack_version": str(pack.get("pack_version") or ""),
                "pack_category": str(pack.get("pack_category") or ""),
                "items_count": len(pack.get("items") or []),
                "curation_status": str(pack.get("curation_status") or "draft"),
                "source_label": str(pack.get("source_label") or ""),
                "loaded": True,
            }
        )
    return out


def get_pack_detail(pack_id: str) -> dict[str, Any] | None:
    load_registry(strict=False)
    key = str(pack_id or "").strip().lower()
    for p in _PACKS_CACHE:
        if str(p.get("pack_id") or "").strip().lower() == key:
            return dict(p)
    return None


def get_analyte_alias_map() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for rule in get_enabled_nutrients():
        for canonical, aliases in (rule.get("analyte_aliases") or {}).items():
            bucket = out.setdefault(str(canonical).lower(), [])
            for item in aliases or []:
                low = str(item or "").lower()
                if low and low not in bucket:
                    bucket.append(low)
    return out


def get_nutrient_by_analyte(analyte_code: str) -> list[str]:
    key = str(analyte_code or "").strip().lower()
    out = []
    for code, rule in get_all_nutrient_rules().items():
        if key in {str(x).lower() for x in (rule.get("supported_analytes") or [])}:
            out.append(code)
    return sorted(set(out))


def get_stack_to_nutrient_map() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for code, rule in get_all_nutrient_rules().items():
        for token in (rule.get("stack_trigger_slugs") or rule.get("stack_match_slugs") or []):
            key = str(token or "").lower()
            if not key:
                continue
            out.setdefault(key, []).append(code)
    return {k: sorted(set(v)) for k, v in out.items()}


def get_profile_impact_map() -> dict[str, list[str]]:
    out: dict[str, set[str]] = {}
    for code, rule in get_all_nutrient_rules().items():
        for field in (rule.get("profile_trigger_fields") or []):
            out.setdefault(str(field), set()).add(code)
    if not out:
        for field, nutrients in PROFILE_IMPACT_MAP.items():
            out.setdefault(field, set()).update(nutrients)
    return {k: sorted(v) for k, v in out.items()}


def get_lab_impact_map() -> dict[str, list[str]]:
    out: dict[str, set[str]] = {}
    for code, rule in get_all_nutrient_rules().items():
        for analyte in (rule.get("lab_trigger_analytes") or []):
            out.setdefault(str(analyte).lower(), set()).add(code)
    if not out:
        for analyte, nutrients in LAB_ANALYTE_IMPACT_MAP.items():
            out.setdefault(analyte, set()).update(nutrients)
    return {k: sorted(v) for k, v in out.items()}


def registry_summary() -> dict[str, Any]:
    rules = get_all_nutrient_rules()
    items = []
    for code, rule in sorted(rules.items()):
        items.append(
            {
                "nutrient_code": code,
                "title_ru": rule.get("title_ru") or code,
                "enabled": bool(rule.get("enabled", True)),
                "readiness_level": rule.get("readiness_level"),
                "engine_mode": rule.get("engine_mode"),
                "planner_mode": rule.get("planner_mode"),
                "explanation_mode": rule.get("explanation_mode"),
                "primary_biomarkers": rule.get("primary_biomarkers") or [],
                "supported_units": rule.get("supported_units") or {},
                "source_pack_id": rule.get("source_pack_id") or "",
                "source_pack_version": rule.get("source_pack_version") or "",
                "curation_status": rule.get("curation_status") or "",
                "capabilities": dict(rule.get("capabilities") or {}),
            }
        )
    return {
        "registry_version": REGISTRY_VERSION,
        "rules_version": RULES_VERSION,
        "meta": get_registry_meta(),
        "items": items,
        "validation": get_registry_validation(),
    }


def coverage_report() -> dict[str, Any]:
    load_registry(strict=False)
    return build_pack_qc_report(_PACKS_CACHE, _REGISTRY_CACHE or {}, validation=_REGISTRY_VALIDATION)


load_registry(strict=False)
