from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nutrient_rule_schema import apply_rule_schema, REGISTRY_VERSION, RULES_VERSION

PACKS_DIR = Path("data/nutrient_packs")


class PackConflictError(RuntimeError):
    pass


def load_single_pack(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    required = ["pack_id", "pack_title_ru", "pack_version", "pack_category", "rules_version", "items"]
    missing = [k for k in required if k not in data]
    if missing:
        raise ValueError(f"{p}: missing pack fields {missing}")
    if not isinstance(data.get("items"), list):
        raise ValueError(f"{p}: items must be list")
    data["_path"] = str(p)
    return data


def load_all_nutrient_packs(packs_dir: str | Path = PACKS_DIR) -> list[dict[str, Any]]:
    root = Path(packs_dir)
    if not root.exists():
        return []
    packs = []
    for path in sorted(root.glob("*.json")):
        packs.append(load_single_pack(path))
    return packs


def _capabilities_from_rule(rule: dict[str, Any]) -> dict[str, bool]:
    planner_mode = str(rule.get("planner_mode") or "")
    explain_mode = str(rule.get("explanation_mode") or "")
    can_followup = planner_mode in {"deterministic", "limited"}
    return {
        "can_preview": str(rule.get("engine_mode") or "") in {"deterministic_v1", "limited"},
        "can_plan": planner_mode in {"deterministic", "limited"},
        "can_explain": explain_mode in {"full", "limited", "metadata_only"},
        "can_followup": can_followup,
    }


def detect_pack_conflicts(packs: list[dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    seen: dict[str, tuple[str, str]] = {}
    alias_seen: dict[str, str] = {}
    canonical_seen: dict[str, str] = {}
    for pack in packs:
        pack_id = str(pack.get("pack_id") or "")
        allow_override = bool(pack.get("allow_override", False))
        for item in pack.get("items") or []:
            code = str(item.get("nutrient_code") or "").strip().lower()
            if not code:
                continue
            if code in seen and not allow_override:
                prev_pack, _ = seen[code]
                issues.append(f"nutrient '{code}' duplicated in packs '{prev_pack}' and '{pack_id}' without allow_override")
            seen[code] = (pack_id, str(pack.get("pack_version") or ""))

            readiness = str(item.get("readiness_level") or "")
            if readiness and readiness not in {"deterministic_v1", "limited_interpretation", "metadata_only", "planned"}:
                issues.append(f"{pack_id}:{code} invalid readiness_level '{readiness}'")

            aliases = item.get("analyte_aliases") or {}
            if isinstance(aliases, dict):
                for canonical, vals in aliases.items():
                    c = str(canonical or "").lower()
                    for alias in list(vals or []) + [c]:
                        a = str(alias or "").lower()
                        if not a:
                            continue
                        prev = alias_seen.get(a)
                        if prev and prev != c:
                            issues.append(f"alias conflict '{a}' -> {prev} vs {c}")
                        alias_seen[a] = c

            can_units = item.get("canonical_units") or {}
            if isinstance(can_units, dict):
                for analyte, unit in can_units.items():
                    key = str(analyte or "").lower()
                    val = str(unit or "").lower()
                    prev = canonical_seen.get(key)
                    if prev and prev != val:
                        issues.append(f"canonical unit conflict for '{key}': {prev} vs {val}")
                    canonical_seen[key] = val
    return issues


def merge_pack_items(packs: list[dict[str, Any]], *, fail_on_conflict: bool = True) -> dict[str, dict[str, Any]]:
    conflicts = detect_pack_conflicts(packs)
    if conflicts and fail_on_conflict:
        raise PackConflictError("; ".join(conflicts))

    merged: dict[str, dict[str, Any]] = {}
    for pack in packs:
        pack_id = str(pack.get("pack_id") or "")
        pack_ver = str(pack.get("pack_version") or "")
        curation = str(pack.get("curation_status") or "draft")
        for raw in pack.get("items") or []:
            code = str(raw.get("nutrient_code") or "").strip().lower()
            if not code:
                continue
            item = dict(raw)
            item.setdefault("source_pack_id", pack_id)
            item.setdefault("source_pack_version", pack_ver)
            item.setdefault("curation_status", curation)
            item.setdefault("rules_version", pack.get("rules_version") or RULES_VERSION)
            item.setdefault("registry_version", REGISTRY_VERSION)
            normalized = apply_rule_schema(item)
            normalized["capabilities"] = _capabilities_from_rule(normalized)
            merged[code] = normalized
    return merged


def build_registry_from_packs(
    packs_dir: str | Path = PACKS_DIR,
    *,
    fail_on_conflict: bool = True,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    packs = load_all_nutrient_packs(packs_dir)
    merged = merge_pack_items(packs, fail_on_conflict=fail_on_conflict)
    meta = {
        "packs_count": len(packs),
        "pack_ids": [str(p.get("pack_id") or "") for p in packs],
        "conflicts": detect_pack_conflicts(packs),
    }
    return merged, meta
