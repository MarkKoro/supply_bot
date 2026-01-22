from __future__ import annotations
import argparse
import asyncio
import json
import sys
from pathlib import Path
from settings import get_settings
import db

def _read_jsonl(path: str):
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)

def _build_aliases(item: dict) -> list[str]:
    aliases = []
    for key in ("aliases_ru", "aliases_en", "aliases_misc", "aliases"):
        aliases.extend(item.get(key) or [])
    return aliases

async def _import_rich(conn, supp_path: str, slug_to_id: dict[str, int]) -> None:
    pending_interactions: list[tuple[str, dict]] = []
    for item in _read_jsonl(supp_path):
        name_ru = item.get("name_ru")
        name_en = item.get("name_en")
        interaction_classes = item.get("interaction_classes") or []
        payload = {
            "slug": item["slug"],
            "name": item.get("name") or name_ru or name_en or item["slug"],
            "name_ru": name_ru,
            "name_en": name_en,
            "summary_verified": item.get("summary_ru_verified") or "",
            "summary_theoretical": item.get("summary_ru_theoretical") or "",
            "molecular_mechanisms": item.get("molecular_mechanisms_ru") or "",
            "evidence_json": json.dumps(item.get("evidence") or {}, ensure_ascii=False),
            "dose_ru": item.get("dose_ru") or "",
            "timing_ru": item.get("timing_ru") or "",
            "safety_ru": item.get("safety_ru") or "",
            "purchase_url": item.get("purchase_url") or "",
            "tags_json": json.dumps(item.get("tags") or [], ensure_ascii=False),
            "interaction_classes": json.dumps(interaction_classes, ensure_ascii=False),
            "interaction_classes_json": json.dumps(interaction_classes, ensure_ascii=False),
            "payload_json": json.dumps(item, ensure_ascii=False),
        }
        sid = await db.upsert_supplement(conn, payload)
        slug_to_id[item["slug"]] = sid

        aliases = _build_aliases(item)
        for extra in (name_ru, name_en, item.get("slug")):
            if extra:
                aliases.append(extra)
        await db.upsert_aliases(conn, sid, aliases)

        for interaction in item.get("explicit_interactions") or []:
            other_slug = interaction.get("other_slug")
            if not other_slug:
                continue
            if other_slug not in slug_to_id:
                pending_interactions.append((item["slug"], interaction))
                continue
            await db.upsert_interaction(
                conn,
                slug_to_id[item["slug"]],
                slug_to_id[other_slug],
                {
                    "relation": interaction.get("relation", "neutral"),
                    "severity": int(interaction.get("severity", 0)),
                    "evidence_grade": interaction.get("evidence_grade") or "",
                    "note": interaction.get("note_ru") or "",
                    "source_url": interaction.get("source_url") or "",
                },
            )

    if pending_interactions:
        remaining: list[tuple[str, dict]] = []
        for slug, interaction in pending_interactions:
            other_slug = interaction.get("other_slug")
            if not other_slug or other_slug not in slug_to_id or slug not in slug_to_id:
                remaining.append((slug, interaction))
                continue
            await db.upsert_interaction(
                conn,
                slug_to_id[slug],
                slug_to_id[other_slug],
                {
                    "relation": interaction.get("relation", "neutral"),
                    "severity": int(interaction.get("severity", 0)),
                    "evidence_grade": interaction.get("evidence_grade") or "",
                    "note": interaction.get("note_ru") or "",
                    "source_url": interaction.get("source_url") or "",
                },
            )
        if remaining:
            print(f"Skipped {len(remaining)} pending interactions due to missing slugs.")

async def _import(
    supp_path: str | None,
    inter_path: str | None,
    rich_supp_path: str | None = None,
) -> None:
    if rich_supp_path is None and supp_path:
        if "rich" in Path(supp_path).name:
            rich_supp_path = supp_path
            supp_path = None
    if rich_supp_path:
        if not Path(rich_supp_path).exists():
            raise RuntimeError(f"Missing rich supplements seed file: {rich_supp_path}")
    elif supp_path:
        if not Path(supp_path).exists():
            raise RuntimeError(f"Missing supplements seed file: {supp_path}")
    if inter_path and not Path(inter_path).exists():
        print(f"Warning: interactions seed file missing, skipping: {inter_path}", file=sys.stderr)
        inter_path = None
    s = get_settings()
    conn = await db.connect(s.DB_PATH)
    try:
        slug_to_id: dict[str, int] = {}

        if rich_supp_path:
            await _import_rich(conn, rich_supp_path, slug_to_id)
        elif supp_path:
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
                await db.upsert_aliases(conn, sid, _build_aliases(item))

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
    ap.add_argument("--rich-supplements", default=None)
    args = ap.parse_args()
    asyncio.run(_import(args.supplements, args.interactions, args.rich_supplements))
    print("Import done.")

if __name__ == "__main__":
    main()
