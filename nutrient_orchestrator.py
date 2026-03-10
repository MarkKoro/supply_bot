from __future__ import annotations

import json
from typing import Any

import db
import nutrient_engine
import nutrient_explainer
import nutrient_planner
from nutrient_registry import get_lab_impact_map, get_profile_impact_map, get_all_nutrient_rules
from openai_client import OpenAIConfig


def _json_norm(value: Any) -> str:
    if value is None:
        return ""
    txt = str(value).strip()
    if not txt:
        return ""
    try:
        parsed = json.loads(txt)
        if isinstance(parsed, list):
            norm = sorted(str(x).strip() for x in parsed if str(x).strip())
            return json.dumps(norm, ensure_ascii=False)
        return json.dumps(parsed, ensure_ascii=False, sort_keys=True)
    except Exception:
        return txt


def collect_profile_change_impact(before: dict, after: dict) -> tuple[list[str], list[str]]:
    impacted: set[str] = set()
    reason_codes: list[str] = []
    for field, nutrients in get_profile_impact_map().items():
        b = before.get(field)
        a = after.get(field)
        if field in {"conditions_json", "meds_json"}:
            changed = _json_norm(b) != _json_norm(a)
        else:
            changed = str(b or "").strip() != str(a or "").strip()
        if not changed:
            continue
        impacted.update(nutrients)
        reason_codes.append(f"profile:{field}")
    return sorted(impacted), reason_codes


def collect_lab_change_impact(analyte_codes: list[str]) -> tuple[list[str], list[str]]:
    impacted: set[str] = set()
    reasons: list[str] = []
    for code in analyte_codes:
        key = str(code or "").strip().lower()
        nutrients = (get_lab_impact_map().get(key) or [])
        if nutrients:
            impacted.update(nutrients)
            reasons.append(f"lab:{key}")
    return sorted(impacted), sorted(set(reasons))


def collect_stack_change_impact(changed_slugs: list[str]) -> tuple[list[str], list[str]]:
    impacted: set[str] = set()
    reasons: list[str] = []
    slugs = [str(x or "").strip().lower() for x in changed_slugs if str(x or "").strip()]
    for nutrient_code, profile in get_all_nutrient_rules().items():
        match_tokens = [str(x).lower() for x in (profile.get("stack_match_slugs") or [])]
        if any(any(token and token in slug for token in match_tokens) for slug in slugs):
            impacted.add(nutrient_code)
            reasons.append(f"stack:{nutrient_code}")
    return sorted(impacted), sorted(set(reasons))


async def mark_plans_stale(conn, tg_user_id: int, nutrients: list[str], reason_codes: list[str]) -> int:
    if not nutrients:
        return 0
    return await db.mark_active_plans_stale(conn, tg_user_id, nutrients, reason_codes)


async def enqueue_plan_rebuilds(conn, tg_user_id: int, nutrients: list[str], trigger_type: str, trigger_payload: dict | None = None, priority: int = 50) -> int:
    cnt = 0
    for nutrient_code in nutrients:
        created = await db.enqueue_nutrient_rebuild_job(
            conn,
            tg_user_id,
            nutrient_code,
            trigger_type=trigger_type,
            trigger_payload=trigger_payload or {},
            priority=priority,
        )
        if created:
            cnt += 1
    return cnt


async def resolve_impacted_nutrients(conn, tg_user_id: int, *, profile_before: dict | None = None, profile_after: dict | None = None, analyte_codes: list[str] | None = None, stack_slugs: list[str] | None = None) -> tuple[list[str], list[str]]:
    impacted: set[str] = set()
    reasons: set[str] = set()
    if profile_before is not None and profile_after is not None:
        n, r = collect_profile_change_impact(profile_before, profile_after)
        impacted.update(n)
        reasons.update(r)
    if analyte_codes:
        n, r = collect_lab_change_impact(analyte_codes)
        impacted.update(n)
        reasons.update(r)
    if stack_slugs:
        n, r = collect_stack_change_impact(stack_slugs)
        impacted.update(n)
        reasons.update(r)
    return sorted(impacted), sorted(reasons)


async def rebuild_plan_for_nutrient(conn, tg_user_id: int, nutrient_code: str, *, source_report_id: int | None = None, openai_cfg: OpenAIConfig | None = None) -> dict:
    profile = await db.get_user_health_profile(conn, tg_user_id)
    supplements = await db.get_user_supplements_with_enabled(conn, tg_user_id)
    active = [x for x in supplements if int(x.get("enabled") or 0) == 1]
    lab_rows = await db.get_recent_lab_results_for_user(conn, tg_user_id, report_id=source_report_id, limit=180)

    preview = nutrient_engine.evaluate_nutrient(
        nutrient_code=nutrient_code,
        profile=profile,
        active_supplements=active,
        lab_rows=lab_rows,
    )
    plan = nutrient_planner.build_plan_from_preview(preview)

    new_plan_id = await db.create_nutrient_plan_run(conn, tg_user_id, nutrient_code, source_report_id, preview, plan)
    await db.save_nutrient_followup_items(conn, tg_user_id, new_plan_id, nutrient_code, plan.get("followup_items") or [])
    await db.save_nutrient_reminder_candidates(conn, tg_user_id, new_plan_id, nutrient_code, plan.get("reminder_candidates") or [])

    # if previous had active reminders, re-activate for new generation
    had_old_active = await db.has_active_nutrient_reminders_for_nutrient(conn, tg_user_id, nutrient_code, exclude_plan_run_id=new_plan_id)
    if had_old_active:
        await db.activate_nutrient_reminders_for_plan(conn, tg_user_id, new_plan_id)

    # eager explanation refresh only when LLM cfg exists; fallback handled inside builder
    built = await nutrient_explainer.build_explanation_from_plan(
        cfg=openai_cfg,
        nutrient_code=nutrient_code,
        preview_snapshot=preview,
        plan_snapshot=plan,
    )
    await db.save_plan_explanation(
        conn,
        tg_user_id,
        new_plan_id,
        nutrient_code,
        built.get("explanation") or {},
        built.get("model_name"),
        built.get("prompt_version") or nutrient_explainer.PROMPT_VERSION,
        built.get("source_type") or "fallback",
    )
    await conn.commit()
    return {"plan_id": new_plan_id, "preview": preview, "plan": plan}


async def process_pending_rebuild_jobs(conn, *, openai_cfg: OpenAIConfig | None = None, limit: int = 8) -> dict:
    jobs = await db.get_pending_nutrient_rebuild_jobs(conn, int(limit))
    done = 0
    failed = 0
    for job in jobs:
        jid = int(job["id"])
        tg_user_id = int(job["tg_user_id"])
        nutrient_code = str(job["nutrient_code"] or "").lower()
        try:
            await db.set_nutrient_rebuild_job_processing(conn, jid)
            await rebuild_plan_for_nutrient(conn, tg_user_id, nutrient_code, openai_cfg=openai_cfg)
            await db.set_nutrient_rebuild_job_done(conn, jid)
            done += 1
        except Exception as exc:
            await db.set_nutrient_rebuild_job_failed(conn, jid, str(exc)[:400])
            failed += 1
    return {"processed": len(jobs), "done": done, "failed": failed}
