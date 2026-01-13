from __future__ import annotations
import aiosqlite
import db

REL_LABELS = {
    "synergy": "✅ Хорошо вместе",
    "avoid": "⛔ Лучше не сочетать",
    "caution": "⚠️ Осторожно вместе",
    "neutral": "ℹ️ Нейтрально/данных мало",
}

async def describe_interactions(conn: aiosqlite.Connection, supp_row, *, limit_each: int = 8) -> dict[str, list[dict]]:
    supp_id = int(supp_row["id"])
    groups: dict[str, list[dict]] = {}
    for rel in ("synergy", "avoid", "caution"):
        rows = await db.list_interactions_for_supplement(conn, supp_id, relation=rel, limit=limit_each)
        groups[rel] = await _hydrate(conn, supp_id, rows)
    return groups

async def _hydrate(conn: aiosqlite.Connection, anchor_id: int, rows) -> list[dict]:
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
        })
    return out

def format_interactions(title_supp: str, groups: dict[str, list[dict]]) -> str:
    lines = [f"🧩 <b>Совместимость для:</b> <b>{title_supp}</b>"]
    shown = 0
    for rel in ("synergy", "avoid", "caution"):
        items = groups.get(rel) or []
        if not items:
            continue
        lines.append("")
        lines.append(f"<b>{REL_LABELS.get(rel, rel)}</b>")
        for it in items:
            ev = f" ({it['evidence']})" if it["evidence"] else ""
            note = f" — {it['note']}" if it["note"] else ""
            lines.append(f"• <b>{it['other_name']}</b>{ev}{note}")
            shown += 1
    if shown == 0:
        lines.append("\nПока нет данных по связям. (Мы добавим доказательную базу.)")
    return "\n".join(lines)
