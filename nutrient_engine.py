from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from typing import Any

from nutrient_registry import get_all_nutrient_rules, get_analyte_alias_map
from nutrient_rules import NUTRIENT_DEF_BY_CODE




def _rules() -> dict[str, dict]:
    return get_all_nutrient_rules()


@lru_cache(maxsize=1)
def _unit_aliases_from_rules() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for rule in _rules().values():
        for canonical, vals in (rule.get("supported_units") or {}).items():
            c = str(canonical or "").lower()
            out.setdefault(c, [])
            for v in vals or []:
                low = str(v or "").lower()
                if low and low not in out[c]:
                    out[c].append(low)
    return out


@lru_cache(maxsize=1)
def _unit_conversions_from_rules() -> dict[tuple[str, str], float]:
    out: dict[tuple[str, str], float] = {}
    for rule in _rules().values():
        conv = rule.get("unit_conversion_rules") or {}
        for key, factor in conv.items():
            try:
                src, dst = str(key).split("->", 1)
                out[(src.strip().lower(), dst.strip().lower())] = float(factor)
            except Exception:
                continue
    return out
def _norm_text(value: str | None) -> str:
    if not value:
        return ""
    out = str(value).strip().lower().replace("ё", "е")
    for ch in ("-", "_", "(", ")", ","):
        out = out.replace(ch, " ")
    out = " ".join(out.split())
    return out


def normalize_analyte_code(code: str | None, name: str | None = None) -> str:
    blob = f"{_norm_text(code)} {_norm_text(name)}".strip()
    if not blob:
        return ""
    for canonical, aliases in get_analyte_alias_map().items():
        options = {_norm_text(canonical), *(_norm_text(x) for x in aliases)}
        if blob in options:
            return canonical
        if any(option and option in blob for option in options):
            return canonical
    return _norm_text(code).replace(" ", "_")


def normalize_unit(unit: str | None) -> str:
    src = _norm_text(unit).replace(" ", "")
    if not src:
        return ""
    for canonical, aliases in _unit_aliases_from_rules().items():
        options = {x.lower().replace(" ", "") for x in aliases}
        if src == canonical or src in options:
            return canonical
    return src


def convert_value_to_supported_unit(value: float | None, from_unit: str, to_unit: str) -> tuple[float | None, str | None]:
    if value is None:
        return None, "missing_value"
    if from_unit == to_unit:
        return float(value), None
    factor = _unit_conversions_from_rules().get((from_unit, to_unit))
    if factor is None:
        return None, "unusable_unit"
    return float(value) * factor, None


def _iso_to_dt(value: str | None) -> datetime:
    raw = str(value or "").strip().replace("Z", "+00:00")
    if not raw:
        return datetime.min
    try:
        return datetime.fromisoformat(raw)
    except Exception:
        return datetime.min


def select_latest_relevant_labs(lab_rows: list[dict], analyte_codes: set[str], limit: int = 3) -> list[dict]:
    filtered = [row for row in lab_rows if normalize_analyte_code(row.get("analyte_code"), row.get("analyte_name")) in analyte_codes]
    filtered.sort(key=lambda x: _iso_to_dt(x.get("measured_at") or x.get("created_at_utc")), reverse=True)
    return filtered[:limit]


def select_recent_trend_points(lab_rows: list[dict], analyte_code: str, limit: int = 3) -> list[dict]:
    points = [row for row in lab_rows if normalize_analyte_code(row.get("analyte_code"), row.get("analyte_name")) == analyte_code]
    points.sort(key=lambda x: _iso_to_dt(x.get("measured_at") or x.get("created_at_utc")), reverse=True)
    return points[:limit]


def build_profile_context(profile: dict | None) -> dict:
    p = profile or {}
    birth_year = p.get("birth_year")
    age = None
    if isinstance(birth_year, int) and birth_year > 1900:
        age = max(0, datetime.utcnow().year - birth_year)
    conditions = str(p.get("conditions_json") or "").lower()
    diet = str(p.get("diet_type") or "").lower()
    return {
        "age": age,
        "sex_at_birth": str(p.get("sex_at_birth") or "").lower(),
        "pregnancy_status": str(p.get("pregnancy_status") or "").lower(),
        "lactation_status": str(p.get("lactation_status") or "").lower(),
        "sun_exposure_level": str(p.get("sun_exposure_level") or "").lower(),
        "diet_type": diet,
        "conditions_json": conditions,
        "has_kidney_context": any(x in conditions for x in ("kidney", "почка", "renal", "ckd")),
        "has_gi_context": any(x in conditions for x in ("malabs", "celiac", "crohn", "жкт", "киш")),
        "is_vegan_or_vegetarian": any(x in diet for x in ("vegan", "vegetarian", "веган", "вегет")),
    }


def build_stack_context(active_supplements: list[dict], stack_match_slugs: list[str]) -> dict:
    slugs = {str(x.get("slug") or "").lower() for x in active_supplements}
    matched = []
    for item in active_supplements:
        slug = str(item.get("slug") or "").lower()
        if any(token in slug for token in stack_match_slugs):
            matched.append({"slug": slug, "name_ru": item.get("name_ru") or item.get("name") or slug})
    flags = []
    if matched:
        flags.append("already_in_stack")
    return {"current_stack_matches": matched, "current_stack_flags": flags, "all_slugs": sorted(slugs)}


def _extract_numeric(row: dict, canonical_unit: str | None = None) -> dict:
    analyte = normalize_analyte_code(row.get("analyte_code"), row.get("analyte_name"))
    unit = normalize_unit(row.get("unit"))
    value_num = row.get("value_num")
    if value_num is None:
        try:
            value_num = float(str(row.get("value_text") or "").replace(",", "."))
        except Exception:
            value_num = None
    converted = value_num
    err = None
    if canonical_unit:
        converted, err = convert_value_to_supported_unit(value_num, unit, canonical_unit)
    return {
        "id": row.get("id"),
        "report_id": row.get("report_id"),
        "analyte_code": analyte,
        "analyte_name": row.get("analyte_name"),
        "measured_at": row.get("measured_at") or row.get("created_at_utc") or "",
        "raw_value_num": value_num,
        "raw_unit": unit,
        "value_num": converted,
        "unit": canonical_unit or unit,
        "convert_error": err,
    }


def _trend_summary(points: list[dict]) -> dict:
    if len(points) < 2:
        return {"direction": "insufficient_data"}
    newest = points[0].get("value_num")
    older = points[-1].get("value_num")
    if newest is None or older is None:
        return {"direction": "insufficient_data"}
    delta = newest - older
    if abs(delta) < 1e-9:
        return {"direction": "stable", "delta": 0}
    return {"direction": "up" if delta > 0 else "down", "delta": round(delta, 4)}


def evaluate_nutrient(
    nutrient_code: str,
    profile: dict | None,
    active_supplements: list[dict],
    lab_rows: list[dict],
) -> dict[str, Any]:
    code = str(nutrient_code or "").strip().lower()
    definition = NUTRIENT_DEF_BY_CODE.get(code)
    rule = _rules().get(code)
    if not definition or not rule:
        return {
            "nutrient_code": code,
            "title_ru": code,
            "engine_mode": "limited",
            "supported": False,
            "confidence_level": "unsupported",
            "profile_snapshot": build_profile_context(profile),
            "latest_relevant_labs": [],
            "trend_points": [],
            "trend_summary": {"direction": "insufficient_data"},
            "interpreted_status": "unknown_nutrient",
            "current_stack_matches": [],
            "current_stack_flags": [],
            "missing_inputs": ["unknown_nutrient_code"],
            "physician_flags": [],
            "safety_flags": [],
            "retest_hint": "Проверьте код нутриента.",
            "evidence_basis": "nutrient_not_in_catalog",
            "ui_notes_ru": "Этот нутриент пока не поддерживается в preview-движке.",
            "readiness_level": "metadata_only",
            "rules_version": "",
            "capabilities": {"can_preview": False, "can_plan": False, "can_explain": True, "can_followup": False},
        }

    profile_ctx = build_profile_context(profile)
    stack_ctx = build_stack_context(active_supplements, [str(x).lower() for x in rule.get("stack_match_slugs", [])])
    canonical_units = rule.get("canonical_units") or {}
    analyte_set = set(rule.get("primary_biomarkers", []) + rule.get("secondary_biomarkers", []))

    normalized_rows = []
    for row in list(lab_rows or []):
        analyte = normalize_analyte_code(row.get("analyte_code"), row.get("analyte_name"))
        if analyte not in analyte_set:
            continue
        normalized_rows.append(_extract_numeric(row, canonical_units.get(analyte)))

    normalized_rows.sort(key=lambda x: _iso_to_dt(x.get("measured_at")), reverse=True)
    latest = normalized_rows[:3]

    missing_inputs: list[str] = []
    physician_flags: list[str] = []
    safety_flags: list[str] = []

    if not normalized_rows:
        missing_inputs.append("no_relevant_labs")

    if any(x.get("convert_error") == "unusable_unit" for x in normalized_rows):
        safety_flags.append("unusable_unit")
        missing_inputs.append("unit_normalization_required")

    confidence = "low"
    interpreted_status = "unknown_or_limited"
    evidence_basis = "limited_preview"
    trend_points = []

    if rule.get("engine_mode") == "limited":
        confidence = "low"
        interpreted_status = "unknown_or_limited"
    elif code == "vitamin_d":
        d_rows = [x for x in normalized_rows if x["analyte_code"] == "25ohd" and x.get("value_num") is not None]
        trend_points = d_rows[:3]
        if not d_rows:
            missing_inputs.append("need_25ohd")
            interpreted_status = "insufficient_data"
            confidence = "low"
        else:
            v = float(d_rows[0]["value_num"])
            th = rule["interpretation_thresholds"]
            if v >= th["urgent_high"]:
                interpreted_status = "very_high_urgent_flag"
                physician_flags.append("physician_review_recommended")
                confidence = "high"
            elif v > th["adequate_high"]:
                interpreted_status = "high_caution"
                confidence = "high"
            elif v >= th["borderline_low"]:
                interpreted_status = "adequate_for_most"
                confidence = "high"
            elif v >= th["very_low"]:
                interpreted_status = "borderline_low"
                confidence = "high"
            else:
                interpreted_status = "low"
                confidence = "high"
            evidence_basis = "25ohd_primary"
            if profile_ctx["sun_exposure_level"] in {"low", "низкий", "minimal"}:
                missing_inputs.append("consider_followup_due_to_low_sun_exposure")
    elif code == "vitamin_b12":
        b12 = next((x for x in normalized_rows if x["analyte_code"] == "serum_b12" and x.get("value_num") is not None), None)
        mma = next((x for x in normalized_rows if x["analyte_code"] == "methylmalonic_acid" and x.get("value_num") is not None), None)
        hcy = next((x for x in normalized_rows if x["analyte_code"] == "homocysteine" and x.get("value_num") is not None), None)
        trend_points = select_recent_trend_points(normalized_rows, "serum_b12")
        if not b12:
            interpreted_status = "insufficient_data"
            missing_inputs.append("need_serum_b12")
        else:
            b = float(b12["value_num"])
            th = rule["interpretation_thresholds"]
            if b < th["low_b12"]:
                interpreted_status = "low"
                confidence = "high"
            elif b < th["borderline_high"]:
                if mma and float(mma["value_num"]) > th["mma_high"]:
                    interpreted_status = "likely_low_confirmed"
                    confidence = "high"
                elif mma:
                    interpreted_status = "borderline_needs_confirmation"
                    confidence = "medium"
                else:
                    interpreted_status = "borderline_needs_confirmation"
                    confidence = "medium"
                    missing_inputs.append("mma_for_confirmation")
            else:
                if mma and float(mma["value_num"]) > th["mma_high"]:
                    interpreted_status = "discordant_review"
                    confidence = "medium"
                    physician_flags.append("discordant_b12_mma")
                else:
                    interpreted_status = "adequate_signal"
                    confidence = "medium"
            if hcy:
                missing_inputs.append("homocysteine_is_secondary_hint")
            evidence_basis = "serum_b12_primary_mma_secondary"
            if profile_ctx["has_kidney_context"]:
                confidence = "low" if confidence == "medium" else "medium"
                physician_flags.append("kidney_context_reduces_mma_specificity")
            if profile_ctx["is_vegan_or_vegetarian"]:
                missing_inputs.append("dietary_pattern_increases_b12_relevance")
    elif code == "iron":
        ferritin = next((x for x in normalized_rows if x["analyte_code"] == "ferritin" and x.get("value_num") is not None), None)
        tsat = next((x for x in normalized_rows if x["analyte_code"] == "transferrin_saturation"), None)
        hb = next((x for x in normalized_rows if x["analyte_code"] == "hemoglobin"), None)
        crp = next((x for x in normalized_rows if x["analyte_code"] == "crp"), None)
        trend_points = select_recent_trend_points(normalized_rows, "ferritin")
        if not ferritin:
            interpreted_status = "insufficient_data"
            missing_inputs.append("need_ferritin")
        else:
            f = float(ferritin["value_num"])
            th = rule["interpretation_thresholds"]
            if f < th["very_low"]:
                interpreted_status = "very_low_iron_stores"
                confidence = "high"
                physician_flags.append("very_low_ferritin")
            elif f < th["low"]:
                interpreted_status = "low_iron_stores"
                confidence = "high"
            else:
                interpreted_status = "stores_not_low_by_ferritin"
                confidence = "medium"
                menstruating = profile_ctx["sex_at_birth"] == "female" and profile_ctx["pregnancy_status"] not in {"pregnant", "yes"}
                cutoff = th["high_menstruating"] if menstruating else th["high_other"]
                if f > cutoff:
                    interpreted_status = "high_caution"
                    physician_flags.append("high_ferritin_context_needed")
                if not crp:
                    missing_inputs.append("crp_for_inflammation_context")
            if not tsat:
                missing_inputs.append("tsat_missing_for_full_picture")
            if not hb:
                missing_inputs.append("cbc_hb_missing_for_full_picture")
            evidence_basis = "ferritin_primary"
    elif code == "folate":
        serum = next((x for x in normalized_rows if x["analyte_code"] == "serum_folate" and x.get("value_num") is not None), None)
        rbc = next((x for x in normalized_rows if x["analyte_code"] == "rbc_folate" and x.get("value_num") is not None), None)
        hcy = next((x for x in normalized_rows if x["analyte_code"] == "homocysteine" and x.get("value_num") is not None), None)
        b12 = next((x for x in normalized_rows if x["analyte_code"] == "serum_b12" and x.get("value_num") is not None), None)
        trend_points = select_recent_trend_points(normalized_rows, "serum_folate")
        th = rule["interpretation_thresholds"]
        if rbc and float(rbc["value_num"]) > th["rbc_adequate"]:
            interpreted_status = "adequate_long_term_signal"
            confidence = "high"
        elif serum and float(serum["value_num"]) > th["serum_adequate"]:
            interpreted_status = "adequate_signal"
            confidence = "medium"
        elif serum or rbc:
            interpreted_status = "borderline_or_low_signal"
            confidence = "medium"
        elif hcy:
            interpreted_status = "insufficient_data"
            confidence = "low"
            missing_inputs.append("homocysteine_only_not_specific")
            missing_inputs.append("need_serum_or_rbc_folate")
        else:
            interpreted_status = "insufficient_data"
            missing_inputs.append("need_folate_markers")
        if (serum or rbc) and (not b12 or float(b12["value_num"]) < 200):
            physician_flags.append("check_b12_before_confident_folate_interpretation")
            confidence = "low" if confidence == "medium" else confidence
        evidence_basis = "rbc_folate_stronger_serum_folate_secondary"
    elif code == "zinc":
        zinc = next((x for x in normalized_rows if x["analyte_code"] == "serum_zinc" and x.get("value_num") is not None), None)
        trend_points = select_recent_trend_points(normalized_rows, "serum_zinc")
        if zinc:
            interpreted_status = "cautious_signal_only"
            confidence = "medium"
            missing_inputs.append("single_serum_zinc_limited_specificity")
        else:
            interpreted_status = "insufficient_data"
            confidence = "low"
            missing_inputs.append("need_serum_or_plasma_zinc")
        evidence_basis = "serum_zinc_limited_specificity"
    else:
        interpreted_status = "unknown_or_limited"
        confidence = "low"

    if profile_ctx["pregnancy_status"] in {"pregnant", "yes"} or profile_ctx["lactation_status"] in {"yes", "lactating"}:
        physician_flags.append("pregnancy_lactation_context")

    trend_summary = _trend_summary(trend_points)
    return {
        "nutrient_code": code,
        "title_ru": rule.get("title_ru") or definition.get("title_ru") or code,
        "engine_mode": rule.get("engine_mode", "limited"),
        "supported": True,
        "confidence_level": confidence,
        "profile_snapshot": profile_ctx,
        "latest_relevant_labs": latest,
        "trend_points": trend_points,
        "trend_summary": trend_summary,
        "interpreted_status": interpreted_status,
        "current_stack_matches": stack_ctx["current_stack_matches"],
        "current_stack_flags": stack_ctx["current_stack_flags"],
        "missing_inputs": sorted(set(x for x in missing_inputs if x)),
        "physician_flags": sorted(set(x for x in physician_flags if x)),
        "safety_flags": sorted(set(x for x in safety_flags if x)),
        "retest_hint": "При недостатке данных можно повторить анализ с корректной единицей и датой.",
        "evidence_basis": evidence_basis,
        "ui_notes_ru": rule.get("ui_notes_ru") or ((definition.get("metadata") or {}).get("limitation_note_ru") or ""),
        "trust_copy_ru": rule.get("trust_copy_ru") or "",
        "dashboard_copy_ru": rule.get("dashboard_copy_ru") or "",
        "rules_version": rule.get("rules_version") or "",
        "readiness_level": rule.get("readiness_level") or ("deterministic_v1" if rule.get("engine_mode")=="deterministic_v1" else "limited_interpretation"),
        "capabilities": {
            "can_preview": str(rule.get("engine_mode") or "") in {"deterministic_v1", "limited"},
            "can_plan": str(rule.get("planner_mode") or "") in {"deterministic", "limited"},
            "can_explain": str(rule.get("explanation_mode") or "") in {"full", "limited"},
            "can_followup": str(rule.get("planner_mode") or "") in {"deterministic", "limited"},
        },
    }
