from __future__ import annotations
import argparse
import asyncio
import json
from settings import get_settings
import db

def _read_jsonl(path: str):
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)

async def _import(supp_path: str | None, inter_path: str | None) -> None:
    s = get_settings()
    conn = await db.connect(s.DB_PATH)
    try:
        slug_to_id: dict[str, int] = {}

        if supp_path:
            for item in _read_jsonl(supp_path):
                name_ru = item.get("name_ru")
                name_en = item.get("name_en")
                payload = {
                    "slug": item["slug"],
                    "name": item.get("name") or name_ru or name_en or item["slug"],
                    "name_ru": name_ru,
                    "name_en": name_en,
                    "summary_ru": item.get("summary_ru") or "",
                    "purchase_url": item.get("purchase_url") or "",
                    "tags_json": json.dumps(item.get("tags") or [], ensure_ascii=False),
                    "mechanisms": item.get("mechanisms_ru") or item.get("mechanisms") or "",
                    "evidence_grade": item.get("evidence_grade") or item.get("evidence") or "",
                    "safety_notes": item.get("safety_ru") or item.get("safety_notes") or "",
                    "interaction_classes": json.dumps(item.get("interaction_classes") or [], ensure_ascii=False),
                }
                sid = await db.upsert_supplement(conn, payload)
                slug_to_id[item["slug"]] = sid
                alias_sets = [
                    (item.get("aliases_ru") or [], "ru"),
                    (item.get("aliases_en") or [], "en"),
                    (item.get("aliases_misc") or [], "misc"),
                    (item.get("aliases") or [], "misc"),
                ]
                for aliases, kind in alias_sets:
                    for alias in aliases:
                        alias = str(alias).strip()
                        if not alias:
                            continue
                        await conn.execute(
                            """INSERT OR IGNORE INTO supplement_aliases(supplement_id, alias, alias_norm, kind)
                               VALUES(?,?,?,?)""",
                            (sid, alias, db.normalize(alias), kind),
                        )
                await conn.commit()

        if inter_path:
            for item in _read_jsonl(inter_path):
                a = item["a"]; b = item["b"]
                if a not in slug_to_id:
                    ra = await db.get_supplement_by_slug(conn, a)
                    if not ra:
                        raise RuntimeError(f"Unknown supplement slug in interactions: {a}")
                    slug_to_id[a] = int(ra["id"])
                if b not in slug_to_id:
                    rb = await db.get_supplement_by_slug(conn, b)
                    if not rb:
                        raise RuntimeError(f"Unknown supplement slug in interactions: {b}")
                    slug_to_id[b] = int(rb["id"])

                await db.upsert_interaction(conn, slug_to_id[a], slug_to_id[b], {
                    "relation": item.get("relation", "neutral"),
                    "severity": int(item.get("severity", 0)),
                    "evidence_grade": item.get("evidence_grade") or item.get("evidence", ""),
                    "note": item.get("note_ru") or item.get("note", ""),
                    "source_url": item.get("source") or item.get("source_url", ""),
                })
    finally:
        await conn.close()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--supplements", default=None)
    ap.add_argument("--interactions", default=None)
    args = ap.parse_args()
    asyncio.run(_import(args.supplements, args.interactions))
    print("Import done.")

if __name__ == "__main__":
    main()
