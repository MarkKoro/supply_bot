from __future__ import annotations
import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from settings import get_settings
import db

def _iter_slug_chunks(path: str):
    # Compatibility iterator; records may span lines and need not start with slug.
    text = Path(path).read_text(encoding="utf-8-sig")
    decoder = json.JSONDecoder()
    position = 0
    while position < len(text):
        while position < len(text) and text[position].isspace():
            position += 1
        if position == len(text):
            break
        start = position
        try:
            _, position = decoder.raw_decode(text, position)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in {path} at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
        yield text[start:position], text.count("\n", 0, start) + 1, text.count("\n", 0, position) + 1


def _read_jsonl(path: str, required_fields: tuple[str, ...] = ("slug",)):
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    seen: set[str] = set()
    for chunk, start_line, end_line in _iter_slug_chunks(path):
        try:
            item = json.loads(chunk, object_pairs_hook=unique_keys)
        except ValueError as exc:
            raise ValueError(f"Invalid JSON in {path} (lines {start_line}-{end_line}): {exc}") from exc
        if not isinstance(item, dict):
            raise ValueError(f"Invalid record in {path} at line {start_line}: expected object")
        missing = [field for field in required_fields if not isinstance(item.get(field), str) or not item[field].strip()]
        if missing:
            raise ValueError(f"Invalid record in {path} at line {start_line}: missing {', '.join(missing)}")
        if "slug" in required_fields:
            slug = item["slug"].strip()
            if slug in seen:
                raise ValueError(f"Duplicate slug '{slug}' in {path} at line {start_line}")
            seen.add(slug)
        yield item


def _normalize_jsonl(path: str) -> None:
    objects = list(_read_jsonl(path, ()))
    normalized = "\n".join(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) for obj in objects)
    Path(path).write_text(normalized + "\n", encoding="utf-8")

def _build_aliases(item: dict) -> list[str]:
    aliases = []
    for key in ("aliases_ru", "aliases_en", "aliases_misc", "aliases"):
        aliases.extend(item.get(key) or [])
    return aliases

def _ensure_omega_aliases(name_en: str | None, aliases: list[str]) -> list[str]:
    if not name_en:
        return aliases
    lower = str(name_en).lower()
    if re.search(r"omega\s*-?\s*3", lower):
        aliases.extend(["omega3", "omega-3", "omega 3"])
    return aliases

def _extract_evidence_fields(item: dict) -> dict[str, str]:
    evidence = item.get("evidence") or {}
    if not isinstance(evidence, dict):
        evidence = {}
    def _string(value: object) -> str:
        return str(value).strip() if value is not None else ""
    return {
        "human_trials_count_est": _string(evidence.get("human_trials_count_est")),
        "participants_total_est": _string(evidence.get("participants_total_est")),
        "avg_effect_percent_est": _string(evidence.get("avg_effect_percent_est")),
        "confidence": _string(evidence.get("confidence")),
    }

async def _import_rich(conn, supp_path: str, slug_to_id: dict[str, int], *, only_missing: bool = False) -> None:
    existing_slugs = {str(row["slug"]) for row in await db.fetchall(conn, "SELECT slug FROM supplements")} if only_missing else set()
    pending_interactions: list[tuple[str, dict]] = []
    for item in _read_jsonl(supp_path):
        if item["slug"] in existing_slugs:
            continue
        name_ru = item.get("name_ru")
        name_en = item.get("name_en")
        interaction_classes = item.get("interaction_classes") or []
        functional_groups = item.get("functional_groups") or []
        if not isinstance(functional_groups, list):
            functional_groups = []
        body_effects = item.get("body_effects") or []
        if not isinstance(body_effects, list):
            body_effects = []

        targets: set[str] = set()
        burdens: set[str] = set()
        for effect in body_effects:
            if not isinstance(effect, dict):
                continue
            system = str(effect.get("system") or "").strip()
            if not system:
                continue
            direction = str(effect.get("direction") or "").strip().lower()
            try:
                strength = int(effect.get("strength", 0))
            except (TypeError, ValueError):
                strength = 0
            if direction == "benefit" and strength > 0:
                targets.add(system)
            elif direction == "burden" and strength > 0:
                burdens.add(system)

        targets_list = sorted(targets)
        burdens_list = sorted(burdens)
        functional_groups_str = " ".join([str(v) for v in functional_groups if v])
        functional_groups_json = json.dumps(functional_groups or [], ensure_ascii=False)
        body_effects_json = json.dumps(body_effects or [], ensure_ascii=False)
        body_targets_str = " ".join(targets_list)
        body_targets_json = json.dumps(targets_list, ensure_ascii=False)
        body_burdens_str = " ".join(burdens_list)
        body_burdens_json = json.dumps(burdens_list, ensure_ascii=False)

        aliases = _ensure_omega_aliases(name_en, _build_aliases(item))
        search_blob = db.build_search_blob(name_ru, name_en, aliases)
        evidence_fields = _extract_evidence_fields(item)
        payload = {
            "slug": item["slug"],
            "name": item.get("name") or name_ru or name_en or item["slug"],
            "name_ru": name_ru,
            "name_en": name_en,
            "aliases": aliases,
            "aliases_json": aliases,
            "search_blob": search_blob,
            "summary_verified": item.get("summary_ru_verified") or "",
            "summary_theoretical": item.get("summary_ru_theoretical") or "",
            "molecular_mechanisms": item.get("molecular_mechanisms_ru") or "",
            "evidence_json": json.dumps(item.get("evidence") or {}, ensure_ascii=False),
            **evidence_fields,
            "dose_ru": item.get("dose_ru") or "",
            "timing_ru": item.get("timing_ru") or "",
            "safety_ru": item.get("safety_ru") or "",
            "purchase_url": item.get("purchase_url") or "",
            "tags_json": json.dumps(item.get("tags") or [], ensure_ascii=False),
            "tags": item.get("tags") or [],
            "supplement_type": item.get("supplement_type") or "",
            "functional_groups": functional_groups_str,
            "functional_groups_json": functional_groups_json,
            "evidence_grade": item.get("evidence_grade") or "",
            "body_effects_json": body_effects_json,
            "body_targets": body_targets_str,
            "body_targets_json": body_targets_json,
            "body_burdens": body_burdens_str,
            "body_burdens_json": body_burdens_json,
            "interaction_classes": json.dumps(interaction_classes, ensure_ascii=False),
            "interaction_classes_json": json.dumps(interaction_classes, ensure_ascii=False),
            "payload_json": json.dumps(item, ensure_ascii=False),
        }
        sid = await db.upsert_supplement(conn, payload)
        slug_to_id[item["slug"]] = sid

        for extra in (name_ru, name_en, item.get("slug")):
            if extra:
                aliases.append(extra)
        await db.upsert_aliases(conn, sid, aliases)

        for interaction in item.get("explicit_interactions") or []:
            other_slug = interaction.get("other_slug")
            if not other_slug:
                continue
            other_id = slug_to_id.get(other_slug)
            if other_id is None:
                other_row = await db.get_supplement_by_slug(conn, other_slug)
                if other_row:
                    other_id = int(other_row["id"])
                    slug_to_id[other_slug] = other_id
            if other_id is None:
                pending_interactions.append((item["slug"], interaction))
                continue
            await db.upsert_interaction(
                conn,
                slug_to_id[item["slug"]],
                other_id,
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
            if not other_slug or slug not in slug_to_id:
                remaining.append((slug, interaction))
                continue
            other_id = slug_to_id.get(other_slug)
            if other_id is None:
                other_row = await db.get_supplement_by_slug(conn, other_slug)
                if other_row:
                    other_id = int(other_row["id"])
                    slug_to_id[other_slug] = other_id
            if other_id is None:
                remaining.append((slug, interaction))
                continue
            await db.upsert_interaction(
                conn,
                slug_to_id[slug],
                other_id,
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
    curated_stacks_path: str | None = None,
    curated_items_path: str | None = None,
    *,
    only_missing: bool = False,
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
    if curated_stacks_path and not Path(curated_stacks_path).exists():
        print(f"Warning: curated stacks seed file missing, skipping: {curated_stacks_path}", file=sys.stderr)
        curated_stacks_path = None
    if curated_items_path and not Path(curated_items_path).exists():
        print(f"Warning: curated stack items seed file missing, skipping: {curated_items_path}", file=sys.stderr)
        curated_items_path = None
    s = get_settings()
    conn = await db.connect(s.DB_PATH)
    try:
        async with db.transaction(conn) as tx:
            conn = tx
            slug_to_id: dict[str, int] = {}

            if rich_supp_path:
                await _import_rich(conn, rich_supp_path, slug_to_id, only_missing=only_missing)
            elif supp_path:
                for item in _read_jsonl(supp_path):
                    if only_missing and await db.get_supplement_by_slug(conn, item["slug"]):
                        continue
                    name_ru = item.get("name_ru")
                    name_en = item.get("name_en")
                    aliases = _ensure_omega_aliases(name_en, _build_aliases(item))
                    search_blob = db.build_search_blob(name_ru, name_en, aliases)
                    payload = {
                        "slug": item["slug"],
                        "name": item.get("name") or name_ru or name_en or item["slug"],
                        "name_ru": name_ru,
                        "name_en": name_en,
                        "aliases": aliases,
                        "aliases_json": aliases,
                        "search_blob": search_blob,
                        "summary_ru": item.get("summary_ru") or "",
                        "purchase_url": item.get("purchase_url") or "",
                        "tags_json": json.dumps(item.get("tags") or [], ensure_ascii=False),
                        "tags": item.get("tags") or [],
                        "mechanisms": item.get("mechanisms_ru") or item.get("mechanisms") or "",
                        "evidence_grade": item.get("evidence_grade") or item.get("evidence") or "",
                        "safety_notes": item.get("safety_ru") or item.get("safety_notes") or "",
                        "interaction_classes": json.dumps(item.get("interaction_classes") or [], ensure_ascii=False),
                    }
                    sid = await db.upsert_supplement(conn, payload)
                    slug_to_id[item["slug"]] = sid
                    await db.upsert_aliases(conn, sid, aliases)

            if inter_path:
                for item in _read_jsonl(inter_path, ("a", "b")):
                    a = item["a"]
                    b = item["b"]
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

            if curated_stacks_path:
                for item in _read_jsonl(curated_stacks_path):
                    tags_json = json.dumps(item.get("tags_json") or item.get("tags") or [], ensure_ascii=False)
                    await conn.execute(
                        """INSERT INTO curated_stacks(
                          slug,title_ru,author_ru,description_ru,source_url,tags_json,is_active,created_at,updated_at
                        )
                        VALUES(?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(slug) DO UPDATE SET
                          title_ru=excluded.title_ru,
                          author_ru=excluded.author_ru,
                          description_ru=excluded.description_ru,
                          source_url=excluded.source_url,
                          tags_json=excluded.tags_json,
                          is_active=excluded.is_active,
                          updated_at=excluded.updated_at""",
                        (
                            item["slug"],
                            item.get("title_ru") or item.get("title") or item["slug"],
                            item.get("author_ru") or "",
                            item.get("description_ru") or "",
                            item.get("source_url"),
                            tags_json,
                            int(item.get("is_active", 1)),
                            db.utc_now_iso(),
                            db.utc_now_iso(),
                        ),
                    )
                await conn.commit()

            if curated_items_path:
                items_by_stack: dict[str, list[dict]] = {}
                for item in _read_jsonl(curated_items_path, ("stack_slug",)):
                    stack_slug = item.get("stack_slug")
                    if not stack_slug:
                        continue
                    items_by_stack.setdefault(stack_slug, []).append(item)

                for stack_slug, items in items_by_stack.items():
                    stack_row = await db.fetchone(conn, "SELECT id FROM curated_stacks WHERE slug=?", (stack_slug,))
                    if not stack_row:
                        print(f"Warning: unknown curated stack slug for items: {stack_slug}", file=sys.stderr)
                        continue
                    stack_id = int(stack_row["id"])
                    await conn.execute("DELETE FROM curated_stack_items WHERE stack_id=?", (stack_id,))
                    for item in items:
                        await conn.execute(
                            """INSERT INTO curated_stack_items(
                              stack_id,supplement_slug,supplement_name,dose_ru,timing_ru,notes_ru,sort_order
                            )
                            VALUES(?,?,?,?,?,?,?)""",
                            (
                                stack_id,
                                item.get("supplement_slug"),
                                item.get("supplement_name"),
                                item.get("dose_ru"),
                                item.get("timing_ru"),
                                item.get("notes_ru"),
                                int(item.get("sort_order", 0)),
                            ),
                        )
                await conn.commit()
    finally:
        await conn.close()



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--supplements", default=None)
    ap.add_argument("--interactions", default=None)
    ap.add_argument("--rich-supplements", default=None)
    ap.add_argument("--curated-stacks", default=None)
    ap.add_argument("--curated-items", default=None)
    ap.add_argument("--normalize-jsonl", action="append", default=[])
    args = ap.parse_args()

    for path in args.normalize_jsonl:
        _normalize_jsonl(path)
        print(f"Normalized JSONL: {path}")

    if any((args.supplements, args.interactions, args.rich_supplements, args.curated_stacks, args.curated_items)):
        asyncio.run(
            _import(
                args.supplements,
                args.interactions,
                args.rich_supplements,
                args.curated_stacks,
                args.curated_items,
            )
        )
        print("Import done.")
    if not args.normalize_jsonl and not any((args.supplements, args.interactions, args.rich_supplements, args.curated_stacks, args.curated_items)):
        ap.error("Provide import paths and/or --normalize-jsonl")

if __name__ == "__main__":
    main()
