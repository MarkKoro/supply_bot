from __future__ import annotations
import json
import aiosqlite
import db
from rules import DEFAULT_CLASS_RULES

COMPAT_MIN_DB = 2

REL_LABELS = {
    "synergy": "✅ Хорошо вместе",
    "avoid": "⛔ Лучше не сочетать",
    "caution": "⚠️ Осторожно вместе",
    "neutral": "ℹ️ Нейтрально/данных мало",
}
REL_ORDER = {
    "avoid": 0,
    "caution": 1,
    "synergy": 2,
    "neutral": 3,
}
EVIDENCE_ORDER = {
    "A": 0,
    "B": 1,
    "C": 2,
    "D": 3,
    "": 4,
}

async def describe_interactions(conn: aiosqlite.Connection, supp_row, *, limit_each: int = 8) -> dict[str, dict[str, list[dict]]]:
    supp_id = int(supp_row["id"])
    explicit_groups: dict[str, list[dict]] = {}
    for rel in ("avoid", "caution", "synergy", "neutral"):
        rows = await db.list_interactions_for_supplement(conn, supp_id, relation=rel, limit=limit_each)
        explicit_groups[rel] = await _hydrate(conn, supp_id, rows, origin="explicit")

    rule_groups = await _class_based_for_supplement(conn, supp_row, limit_each=limit_each)
    return {"explicit": explicit_groups, "rules": rule_groups}

async def _hydrate(conn: aiosqlite.Connection, anchor_id: int, rows, origin: str) -> list[dict]:
    out = []
    for r in rows:
        a_id = int(r["a_id"]); b_id = int(r["b_id"])
        other_id = b_id if a_id == anchor_id else a_id
        other = await db.fetchone(conn, "SELECT slug,name,evidence_grade FROM supplements WHERE id=?", (other_id,))
        out.append({
            "other_slug": other["slug"],
            "other_name": other["name"],
            "relation": r["relation"],
            "severity": int(r["severity"]),
            "evidence": r["evidence_grade"] or "",
            "note": r["note"] or "",
            "source": r["source_url"] or "",
            "origin": origin,
        })
    return out

async def get_classes(conn: aiosqlite.Connection, slug: str) -> set[str]:
    row = await db.get_supplement_by_slug(conn, slug)
    if not row:
        return set()
    return _parse_classes(_classes_raw(row))

def class_based_interactions(slug_a: str, slug_b: str, classes_a: set[str], classes_b: set[str]) -> list[dict]:
    hits: list[dict] = []
    for rule in DEFAULT_CLASS_RULES:
        a = rule["a"]
        b = rule["b"]
        match = (a in classes_a and b in classes_b) or (b in classes_a and a in classes_b)
        if not match:
            continue
        hits.append({
            "relation": rule["relation"],
            "severity": int(rule.get("severity", 0)),
            "evidence": rule.get("evidence", "") or "",
            "note": rule.get("note_ru") or rule.get("note") or "",
            "origin": "rule",
            "rule": f"{a}:{b}",
            "slug_a": slug_a,
            "slug_b": slug_b,
        })
    return hits

def _relation_to_kind(relation: str | None) -> str | None:
    rel = (relation or "").strip().lower()
    if rel == "synergy":
        return "synergy"
    if rel in {"avoid", "antagonism"}:
        return "antagonism"
    if rel == "caution":
        return "caution"
    return None

def _strength_from_severity(severity: int | None) -> int | None:
    if severity is None:
        return None
    try:
        value = int(severity)
    except (TypeError, ValueError):
        return None
    if 0 <= value <= 4:
        return min(100, value * 25)
    if 0 <= value <= 100:
        return value
    return None

def _supp_title(row) -> str:
    name_ru = row["name_ru"] or ""
    name_en = row["name_en"] or ""
    if name_ru and name_en:
        return f"{name_ru} ({name_en})"
    return name_ru or name_en or row["name"]

async def build_compatibility(conn: aiosqlite.Connection, sid: int) -> dict:
    db_items = await db.get_interactions_for_supplement(conn, sid)
    grouped: dict[str, list[dict]] = {"synergy": [], "antagonism": [], "caution": []}
    seen: set[tuple[int, str, str]] = set()
    for item in db_items:
        kind = item.get("kind")
        if kind not in grouped:
            continue
        other_id = int(item["other_supplement_id"])
        reason = (item.get("reason_ru") or "").strip()
        strength = item.get("strength")
        key = (other_id, kind, reason)
        if key in seen:
            continue
        seen.add(key)
        grouped[kind].append(
            {
                "other_id": other_id,
                "reason_ru": reason,
                "strength": strength,
            }
        )

    db_total = sum(len(items) for items in grouped.values())
    source = "db"
    if db_total == 0 or db_total < COMPAT_MIN_DB:
        rules_grouped = await _build_rule_compatibility(conn, sid)
        if db_total == 0:
            grouped = rules_grouped
            source = "rules"
        else:
            source = "mixed"
            for kind, items in rules_grouped.items():
                for item in items:
                    key = (item["other_id"], kind, item.get("reason_ru") or "")
                    if key in seen:
                        continue
                    seen.add(key)
                    grouped[kind].append(item)

    for kind in grouped:
        grouped[kind].sort(key=_compat_sort_key)
    grouped = await _attach_titles(conn, grouped)
    return {**grouped, "source": source}

async def _build_rule_compatibility(conn: aiosqlite.Connection, sid: int) -> dict[str, list[dict]]:
    supp = await db.get_supplement_by_id(conn, sid)
    if not supp:
        return {"synergy": [], "antagonism": [], "caution": []}
    classes_a = _parse_classes(_classes_raw(supp))
    if not classes_a:
        return {"synergy": [], "antagonism": [], "caution": []}

    rows = await db.fetchall(
        conn,
        "SELECT id,slug,name,name_ru,name_en,interaction_classes,interaction_classes_json FROM supplements",
    )
    grouped: dict[str, list[dict]] = {"synergy": [], "antagonism": [], "caution": []}
    best: dict[tuple[int, str], dict] = {}

    for other in rows:
        if int(other["id"]) == sid:
            continue
        classes_b = _parse_classes(_classes_raw(other))
        if not classes_b:
            continue
        hits = class_based_interactions(supp["slug"], other["slug"], classes_a, classes_b)
        for hit in hits:
            kind = _relation_to_kind(hit.get("relation"))
            if kind not in grouped:
                continue
            other_id = int(other["id"])
            reason = (hit.get("note") or "").strip()
            strength = _strength_from_severity(hit.get("severity"))
            key = (other_id, kind)
            existing = best.get(key)
            if existing and (existing.get("strength") or 0) >= (strength or 0):
                continue
            best[key] = {
                "other_id": other_id,
                "reason_ru": reason,
                "strength": strength,
            }

    for (other_id, kind), payload in best.items():
        grouped[kind].append(payload)
    for kind in grouped:
        grouped[kind].sort(key=_compat_sort_key)
    return grouped

def _compat_sort_key(item: dict) -> tuple:
    strength = item.get("strength")
    strength_value = strength if isinstance(strength, int) else 0
    return (-strength_value, str(item.get("title") or ""), int(item.get("other_id") or 0))

async def _attach_titles(conn: aiosqlite.Connection, grouped: dict[str, list[dict]]) -> dict[str, list[dict]]:
    ids: list[int] = []
    for items in grouped.values():
        for item in items:
            ids.append(int(item["other_id"]))
    rows = await db.list_supplements_by_ids(conn, ids)
    titles = {int(r["id"]): _supp_title(r) for r in rows}
    for kind, items in grouped.items():
        for item in items:
            item["title"] = titles.get(int(item["other_id"]), "Неизвестно")
    return grouped

def format_interactions(title_supp: str, groups: dict[str, dict[str, list[dict]]]) -> str:
    lines = [f"🧩 <b>Совместимость для:</b> <b>{title_supp}</b>"]

    explicit_groups = groups.get("explicit") or {}
    rule_groups = groups.get("rules") or {}

    explicit_count = _append_interaction_section(lines, "Явные взаимодействия", explicit_groups)
    rule_count = _append_interaction_section(lines, "По классам", rule_groups)

    if explicit_count + rule_count == 0:
        lines.append("\nПока нет данных по связям. (Мы добавим доказательную базу.)")
    return "\n".join(lines)

async def stack_compatibility(conn: aiosqlite.Connection, supp_rows, *, limit: int = 30) -> list[dict]:
    pairs: list[dict] = []
    rows = list(supp_rows)
    if len(rows) < 2:
        return []

    classes_by_id = {int(r["id"]): _parse_classes(_classes_raw(r)) for r in rows}

    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            a_id = int(a["id"])
            b_id = int(b["id"])
            explicit = await db.get_interaction_between(conn, a_id, b_id)
            if explicit:
                pairs.append({
                    "a_name": a["name"],
                    "b_name": b["name"],
                    "relation": explicit["relation"],
                    "severity": int(explicit["severity"]),
                    "evidence": explicit["evidence_grade"] or "",
                    "note": explicit["note"] or "",
                    "origin": "explicit",
                })

            class_hits = class_based_interactions(
                a["slug"],
                b["slug"],
                classes_by_id[a_id],
                classes_by_id[b_id],
            )
            for hit in class_hits:
                pairs.append({
                    "a_name": a["name"],
                    "b_name": b["name"],
                    "relation": hit["relation"],
                    "severity": hit["severity"],
                    "evidence": hit["evidence"],
                    "note": hit["note"],
                    "origin": "rule",
                })

    pairs.sort(key=_interaction_sort_key)
    return pairs[:limit]

def format_stack_compatibility(items: list[dict]) -> str:
    lines = ["🧩 <b>Проверка стека</b>"]
    if not items:
        lines.append("\nКонфликтов не найдено или недостаточно данных.")
        return "\n".join(lines)
    lines.append("")
    for it in items:
        ev = f"{it['evidence']}" if it["evidence"] else ""
        origin = it["origin"]
        label_bits = [b for b in (ev, origin) if b]
        label = f" ({', '.join(label_bits)})" if label_bits else ""
        note = f" — {it['note']}" if it["note"] else ""
        rel_label = REL_LABELS.get(it["relation"], it["relation"])
        lines.append(f"• {rel_label}: <b>{it['a_name']}</b> + <b>{it['b_name']}</b>{label}{note}")
    return "\n".join(lines)

async def _class_based_for_supplement(conn: aiosqlite.Connection, supp_row, *, limit_each: int = 8) -> dict[str, list[dict]]:
    classes_a = _parse_classes(_classes_raw(supp_row))
    if not classes_a:
        return {rel: [] for rel in REL_ORDER}

    rows = await db.fetchall(conn, "SELECT id,slug,name,interaction_classes,interaction_classes_json FROM supplements")
    rule_groups: dict[str, list[dict]] = {rel: [] for rel in REL_ORDER}

    for other in rows:
        if int(other["id"]) == int(supp_row["id"]):
            continue
        classes_b = _parse_classes(_classes_raw(other))
        if not classes_b:
            continue
        hits = class_based_interactions(supp_row["slug"], other["slug"], classes_a, classes_b)
        for hit in hits:
            rule_groups.setdefault(hit["relation"], []).append({
                "other_slug": other["slug"],
                "other_name": other["name"],
                "relation": hit["relation"],
                "severity": hit["severity"],
                "evidence": hit["evidence"],
                "note": hit["note"],
                "source": "",
                "origin": "rule",
            })

    for rel in rule_groups:
        rule_groups[rel].sort(key=_interaction_sort_key)
        rule_groups[rel] = rule_groups[rel][:limit_each]

    return rule_groups

def _interaction_sort_key(item: dict) -> tuple:
    rel_rank = REL_ORDER.get(item.get("relation"), 99)
    severity = int(item.get("severity", 0))
    evidence = str(item.get("evidence", "")).upper()
    evidence_rank = EVIDENCE_ORDER.get(evidence, len(EVIDENCE_ORDER))
    origin = item.get("origin")
    origin_rank = 0 if origin == "explicit" else 1
    return (rel_rank, -severity, evidence_rank, origin_rank, item.get("other_name") or "", item.get("a_name") or "")

def _append_interaction_section(lines: list[str], title: str, groups: dict[str, list[dict]]) -> int:
    lines.append("")
    lines.append(f"<b>{title}</b>")
    shown = 0
    for rel in ("avoid", "caution", "synergy", "neutral"):
        items = groups.get(rel) or []
        if not items:
            continue
        items.sort(key=_interaction_sort_key)
        lines.append("")
        lines.append(f"<b>{REL_LABELS.get(rel, rel)}</b>")
        for it in items:
            ev = f"{it['evidence']}" if it.get("evidence") else ""
            origin = it.get("origin", "")
            label_bits = [b for b in (ev, origin) if b]
            label = f" ({', '.join(label_bits)})" if label_bits else ""
            note = f" — {it['note']}" if it.get("note") else ""
            lines.append(f"• <b>{it['other_name']}</b>{label}{note}")
            shown += 1
    if shown == 0:
        lines.append("Нет данных.")
    return shown

def _parse_classes(raw) -> set[str]:
    if not raw:
        return set()
    if isinstance(raw, (list, tuple, set)):
        return {str(item) for item in raw if item}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return set()
    if not isinstance(data, list):
        return set()
    return {str(item) for item in data if item}

def _classes_raw(row) -> str:
    if "interaction_classes_json" in row.keys() and row["interaction_classes_json"]:
        return row["interaction_classes_json"]
    return row["interaction_classes"] or "[]"

def format_supplement_details(row) -> str:
    title = _supp_title(row)
    summary_verified = row["summary_verified"] or row["summary_ru"] or row["mechanisms"] or ""
    summary_theoretical = row["summary_theoretical"] or ""
    evidence_grade = row["evidence_grade"] or ""
    evidence_line = _format_evidence_line(row["evidence_json"] or "", evidence_grade)
    dose = row["dose_ru"] or ""
    timing = row["timing_ru"] or ""

    parts = [f"💊 <b>{title}</b>", f"📊 {evidence_line}"]
    if summary_verified:
        parts.append(f"✅ <b>Проверено</b>: {summary_verified}")
    if summary_theoretical:
        parts.append(f"🔬 <b>Теоретически</b>: {summary_theoretical}")
    if dose:
        parts.append(f"💊 Дозировка: {dose}")
    if timing:
        parts.append(f"⏰ Время: {timing}")
    parts.append("<i>Не медицинский совет.</i>")
    return "\n\n".join(parts)

def format_supplement_hub(
    supp_row,
    user_row,
    schedule_time: str | None = None,
    today_status: str | None = None,
) -> str:
    name_ru = supp_row["name_ru"] or ""
    name_en = supp_row["name_en"] or ""
    if name_ru and name_en:
        title = f"{name_ru} ({name_en})"
    else:
        title = name_ru or name_en or supp_row["name"]

    summary = (
        supp_row["summary_verified"]
        or supp_row["summary_ru"]
        or supp_row["summary_theoretical"]
        or supp_row["molecular_mechanisms"]
        or supp_row["mechanisms"]
        or ""
    )

    is_active = bool(user_row["is_active"])
    dose_text = user_row["dose_text"] or "не указано"
    note_text = user_row["note_text"] or "нет"
    status = "Активен" if is_active else "Пауза"
    schedule_time = schedule_time or ""
    schedule_label = f"Напоминание: {schedule_time}" if schedule_time else "Напоминание: не задано"
    if today_status == "taken":
        today_label = "Сегодня: Выпил ✅"
    elif today_status == "skipped":
        today_label = "Сегодня: Не выпил ❌"
    else:
        today_label = "Сегодня: не отмечено"

    parts = [f"💊 <b>{title}</b>"]
    if summary:
        parts.append(summary)
    parts.append(f"Статус: <b>{status}</b>")
    parts.append(f"Доза: <b>{dose_text}</b>")
    parts.append(f"Заметка: <b>{note_text}</b>")
    parts.append("<b>Расписание</b>")
    parts.append(schedule_label)
    parts.append("<b>Сегодня</b>")
    parts.append(today_label)
    return "\n\n".join(parts)

def _format_evidence_line(raw: str, grade: str | None) -> str:
    grade_text = (grade or "").strip() or "неизвестно"
    humans = "неизвестно"
    participants = "неизвестно"
    effect = "неизвестно"
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {}
        if isinstance(data, dict):
            humans = str(data.get("human_trials_count_est") or "").strip() or humans
            participants = str(data.get("participants_total_est") or "").strip() or participants
            effect_raw = str(data.get("avg_effect_percent_est") or "").strip()
            if effect_raw:
                if "%" not in effect_raw:
                    effect_raw = f"{effect_raw}%"
                if not effect_raw.startswith("~"):
                    effect_raw = f"~{effect_raw}"
                effect = effect_raw
    return (
        f"Доказательность: {grade_text} | "
        f"Исследований на людях: {humans} | "
        f"Участников: {participants} | "
        f"Эффект: {effect}"
    )
