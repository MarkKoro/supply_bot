import hashlib
import json
from datetime import timedelta
from pathlib import Path

import httpx
from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.exception_handlers import http_exception_handler as fastapi_http_exception_handler
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from telegram_webapp_auth import verify_init_data
import db
import engine
from settings import get_settings
from openai_client import OpenAIConfig, _sync_call
from achievements import THEMES, TAB_SKINS, BADGES

BASE_DIR = Path(__file__).resolve().parent
WEBAPP_DIR = BASE_DIR / "webapp"
ASSETS_DIR = WEBAPP_DIR / "assets"
ASSETS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="SupplyBot WebApp")
app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")
s = get_settings()
BOT_TOKEN = s.TG_TOKEN


async def _get_role_and_perms(tg_user_id: int) -> tuple[str, dict[str, bool]]:
    conn = await db.connect(s.DB_PATH)
    try:
        role = await db.get_role(conn, int(tg_user_id))
    finally:
        await conn.close()
    return role, db.role_perms(role)


async def _require_perm(tg_user_id: int, perm: str) -> tuple[bool, JSONResponse | None, str, dict[str, bool]]:
    role, perms = await _get_role_and_perms(tg_user_id)
    if not perms.get(perm, False):
        return False, JSONResponse({"ok": False, "code": "PERMISSION_DENIED"}, status_code=403), role, perms
    return True, None, role, perms


class VerifyBody(BaseModel):
    init_data: str


class TodayToggleBody(BaseModel):
    supplement_id: int
    status: str


class SlotTimeBody(BaseModel):
    slot_id: str
    time_local: str


class AddSlotBody(BaseModel):
    time_local: str
    title_ru: str | None = None


class ScheduleSetBody(BaseModel):
    supplement_id: int
    slot_id: str
    time_hhmm: str | None = None


class BillingInvoiceBody(BaseModel):
    plan_id: str
    method: str


class CatalogToggleBody(BaseModel):
    supplement_id: int
    action: str


class MySuppFreezeBody(BaseModel):
    supplement_id: int
    enabled: int


class MySuppUpdateBody(BaseModel):
    supplement_id: int
    dose_text: str = ""
    note_text: str = ""


class MySuppDeleteBody(BaseModel):
    supplement_id: int


class ReminderSetBody(BaseModel):
    supplement_id: int
    mode: str
    times: list[str] = []


class CheckinBody(BaseModel):
    answers: dict
    target: str | None = None
    target_day: str | None = None
    client_local_iso: str | None = None


class CheckinApplyBody(BaseModel):
    checkin_id: int
    selected_ids: list[int] = []
    mode: str

class TodayToggleComplexBody(BaseModel):
    complex_id: int
    status: str


class ComplexCreateBody(BaseModel):
    name: str
    note: str | None = ""


class ComplexRenameBody(BaseModel):
    complex_id: int
    new_name: str


class ComplexDeleteBody(BaseModel):
    complex_id: int


class ComplexItemBody(BaseModel):
    supplement_id: int
    dose_override: str | None = ""


class ComplexScheduleSetBody(BaseModel):
    complex_id: int
    slot_id: str
    time_local: str | None = None


class ComplexScheduleRemoveBody(BaseModel):
    complex_id: int
    slot_id: str


class DiagnosticsRunBody(BaseModel):
    apply: bool = False


class RecommendationsApplyBody(BaseModel):
    local_date: str | None = None
    recommendations: list[dict] = []


class CosmeticsSetBody(BaseModel):
    theme_id: str | None = None
    tab_skin_id: str | None = None
    badge_id: str | None = None


class UserTimezoneBody(BaseModel):
    tz_name: str


async def _auth_user_id(init_data: str) -> int:
    ok, parsed = verify_init_data(init_data, BOT_TOKEN)
    if not ok:
        raise HTTPException(status_code=401, detail="Invalid initData")
    user_raw = parsed.get("user")
    if not user_raw:
        raise HTTPException(status_code=401, detail="Missing user in initData")
    try:
        user_obj = json.loads(user_raw)
        tg_user_id = int(user_obj.get("id"))
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid user payload") from exc
    conn = await db.connect(s.DB_PATH)
    try:
        await db.touch_user(
            conn,
            tg_user_id,
            user_obj.get("username"),
            user_obj.get("first_name"),
            user_obj.get("last_name"),
        )
        status = await db.get_user_status(conn, tg_user_id)
    finally:
        await conn.close()
    if int(status.get("is_banned") or 0) or str(status.get("deleted_at_utc") or "").strip():
        raise HTTPException(status_code=403, detail={"ok": False, "code": "BANNED", "reason": str(status.get("banned_reason") or "")})
    return tg_user_id


async def _premium_guard(tg_user_id: int) -> bool:
    role, _ = await _get_role_and_perms(tg_user_id)
    if role in {db.ROLE_OWNER, db.ROLE_ADMIN}:
        return True
    conn = await db.connect(s.DB_PATH)
    try:
        return await db.is_premium_active(conn, tg_user_id)
    finally:
        await conn.close()


async def supply_http_exception_handler(request, exc: HTTPException):
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            {"ok": False, "error": str(exc.detail), "code": "HTTP_ERROR"},
            status_code=exc.status_code,
        )
    return await fastapi_http_exception_handler(request, exc)


app.add_exception_handler(HTTPException, supply_http_exception_handler)


def _plans_payload() -> list[dict]:
    rows = []
    for plan_id, months in s.PREMIUM_PLANS.items():
        rows.append(
            {
                "plan_id": plan_id,
                "months": int(months),
                "price_rub": int(s.PREMIUM_PRICES_RUB.get(plan_id, 0) or 0),
                "price_stars": int(s.PREMIUM_PRICES_STARS.get(plan_id, 0) or 0),
                "rub_enabled": bool(s.BILLING_ENABLED_RUB and s.YOOKASSA_PROVIDER_TOKEN),
                "stars_enabled": bool(s.BILLING_ENABLED_STARS),
            }
        )
    return rows


async def _build_newly_unlocked_details(conn, newly_keys: list[str]) -> list[dict]:
    clean_keys = [str(key or "").strip() for key in list(newly_keys or []) if str(key or "").strip()]
    if not clean_keys:
        return []
    placeholders = ",".join(["?"] * len(clean_keys))
    rows = await db.fetchall(
        conn,
        f"""SELECT key, title_ru, desc_ru, rarity, points, reward_json
            FROM achievements
            WHERE key IN ({placeholders})""",
        tuple(clean_keys),
    )
    by_key = {str(row["key"] or ""): row for row in rows}
    catalogs = {"themes": THEMES, "tab_skins": TAB_SKINS, "badges": BADGES}
    result = []
    for key in clean_keys:
        row = by_key.get(key)
        if not row:
            continue
        reward_raw = str(row["reward_json"] or "{}")
        try:
            reward_json = json.loads(reward_raw)
            if not isinstance(reward_json, dict):
                reward_json = {}
        except Exception:
            reward_json = {}
        result.append(
            {
                "key": key,
                "title_ru": str(row["title_ru"] or ""),
                "desc_ru": str(row["desc_ru"] or ""),
                "rarity": str(row["rarity"] or "common"),
                "points": int(row["points"] or 0),
                "reward_verbose": db.describe_rewards_ru(reward_json, catalogs),
            }
        )
    return result


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index():
    p = WEBAPP_DIR / "index.html"
    return p.read_text(encoding="utf-8")


@app.post("/api/telegram/verify")
def telegram_verify(body: VerifyBody):
    ok, parsed = verify_init_data(body.init_data, BOT_TOKEN)
    return JSONResponse({"ok": ok, "parsed": parsed})


@app.get("/api/me")
async def api_me(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    conn = await db.connect(s.DB_PATH)
    try:
        role = await db.get_role(conn, tg_user_id)
        perms = db.role_perms(role)
        is_admin = role != db.ROLE_NONE
        premium = is_admin or await db.is_premium_active(conn, tg_user_id)
        premium_until = None if is_admin else await db.get_premium_until(conn, tg_user_id)
        status = await db.get_user_status(conn, tg_user_id)
        cosmetics = await db.get_user_cosmetics(conn, tg_user_id)
    finally:
        await conn.close()
    return JSONResponse({
        "ok": True,
        "premium": premium,
        "premium_until_utc": premium_until,
        "plans": _plans_payload(),
        "is_admin": is_admin,
        "role": role,
        "perms": perms,
        "banned": bool(int(status.get("is_banned") or 0) or str(status.get("deleted_at_utc") or "").strip()),
        "banned_reason": str(status.get("banned_reason") or ""),
        "cosmetics": cosmetics,
    })


@app.get("/api/achievements/summary")
async def api_achievements_summary(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "code": "PREMIUM_REQUIRED"}, status_code=402)
    conn = await db.connect(s.DB_PATH)
    try:
        tz_name = await db.get_user_tz(conn, tg_user_id)
        local_date = db.get_user_local_date(tz_name or None)
        newly = await db.evaluate_and_unlock_achievements(conn, tg_user_id, local_date)
        metrics = await db.get_user_metrics(conn, tg_user_id, local_date)
        user_id = await db.get_or_create_user(conn, tg_user_id)
        unseen_count = await db.get_unseen_achievements_count(conn, tg_user_id)
        unseen_rows = await db.fetchall(
            conn,
            """SELECT achievement_key
               FROM user_achievements
               WHERE user_id=? AND COALESCE(seen_at_utc, '')=''
               ORDER BY unlocked_at_utc DESC, achievement_key ASC
               LIMIT 50""",
            (user_id,),
        )
        unseen_keys = [str(row["achievement_key"] or "") for row in unseen_rows if str(row["achievement_key"] or "").strip()]
        achievements_rows = await db.fetchall(conn, "SELECT key, title_ru, desc_ru, category, rarity, points, reward_json FROM achievements ORDER BY points ASC, key ASC")
        from achievements import ACHIEVEMENTS
        ach_rules = {str(item.get("key") or ""): dict(item.get("rule") or {}) for item in ACHIEVEMENTS}
        unlock_rows = await db.fetchall(conn, "SELECT achievement_key, unlocked_at_utc, seen_at_utc FROM user_achievements WHERE user_id=?", (user_id,))
        unlock_map = {str(r["achievement_key"]): {"unlocked_at_utc": str(r["unlocked_at_utc"] or ""), "seen_at_utc": str(r["seen_at_utc"] or "")} for r in unlock_rows}
        rewards = []
        achievements = []
        next_up_candidates = []
        points_total = 0
        for row in achievements_rows:
            reward = {}
            try:
                reward = json.loads(row["reward_json"] or "{}")
                if not isinstance(reward, dict):
                    reward = {}
            except Exception:
                reward = {}
            key = str(row["key"] or "")
            unlock_meta = unlock_map.get(key) or {}
            unlocked_at = str(unlock_meta.get("unlocked_at_utc") or "")
            seen_at = str(unlock_meta.get("seen_at_utc") or "")
            if unlocked_at:
                rewards.append(reward)
                points_total += int(row["points"] or 0)
            achievement_item = {
                "key": key,
                "title_ru": str(row["title_ru"] or ""),
                "desc_ru": str(row["desc_ru"] or ""),
                "category": str(row["category"] or ""),
                "rarity": str(row["rarity"] or "common"),
                "points": int(row["points"] or 0),
                "reward": reward,
                "unlocked": bool(unlocked_at),
                "unlocked_at_utc": unlocked_at,
                "seen_at_utc": seen_at,
            }
            achievements.append(achievement_item)
            if not unlocked_at:
                progress = db.get_achievement_progress({"rule": ach_rules.get(key) or {}}, metrics)
                pct = progress.get("pct")
                if pct is not None and int(pct) >= 40:
                    next_up_candidates.append({
                        "key": key,
                        "title_ru": achievement_item["title_ru"],
                        "desc_ru": achievement_item["desc_ru"],
                        "category": achievement_item["category"],
                        "rarity": achievement_item["rarity"],
                        "points": achievement_item["points"],
                        "pct": int(pct),
                        "hint_ru": str(progress.get("hint_ru") or ""),
                    })
        allowed = db.compute_allowed_cosmetics(rewards)
        level_status = db.get_level_status(points_total)
        next_up = sorted(next_up_candidates, key=lambda item: (-int(item.get("pct") or 0), int(item.get("points") or 0), str(item.get("key") or "")))[:5]
        catalogs_payload = {"themes": THEMES, "tab_skins": TAB_SKINS, "badges": BADGES}
        recent_rows = await db.get_recent_achievements(conn, user_id, limit=20)
        recent_achievements = [
            {
                "key": str(item.get("key") or ""),
                "title_ru": str(item.get("title_ru") or ""),
                "unlocked_at_utc": str(item.get("unlocked_at_utc") or ""),
                "reward_verbose": db.describe_rewards_ru(item.get("reward") or {}, catalogs_payload),
            }
            for item in recent_rows
        ]
        cosmetics = await db.get_user_cosmetics(conn, tg_user_id)
    finally:
        await conn.close()
    return JSONResponse({
        "ok": True,
        "date_local": local_date,
        "newly_unlocked": newly,
        "unseen_count": unseen_count,
        "unseen_keys": unseen_keys,
        "next_up": next_up,
        "level": int(level_status.get("level") or 1),
        "level_title_ru": str(level_status.get("level_title_ru") or "Новичок"),
        "level_progress_pct": int(level_status.get("level_progress_pct") or 0),
        "points_total": int(level_status.get("points_total") or 0),
        "next_level_points": level_status.get("next_level_points"),
        "recent_achievements": recent_achievements,
        "cosmetics": cosmetics,
        "allowed_cosmetics": allowed,
        "catalogs": catalogs_payload,
        "achievements": achievements,
    })


@app.post("/api/achievements/mark_seen")
async def api_achievements_mark_seen(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "code": "PREMIUM_REQUIRED"}, status_code=402)
    conn = await db.connect(s.DB_PATH)
    try:
        unseen_count = await db.mark_achievements_seen(conn, tg_user_id)
    finally:
        await conn.close()
    return JSONResponse({"ok": True, "unseen_count": unseen_count})


@app.post("/api/cosmetics/set")
async def api_cosmetics_set(body: CosmeticsSetBody, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "code": "PREMIUM_REQUIRED"}, status_code=402)
    conn = await db.connect(s.DB_PATH)
    try:
        user_id = await db.get_or_create_user(conn, tg_user_id)
        unlock_rows = await db.fetchall(conn, """SELECT a.reward_json
                                          FROM user_achievements ua
                                          JOIN achievements a ON a.key=ua.achievement_key
                                          WHERE ua.user_id=?""", (user_id,))
        rewards = []
        for row in unlock_rows:
            try:
                reward = json.loads(row["reward_json"] or "{}")
                if isinstance(reward, dict):
                    rewards.append(reward)
            except Exception:
                pass
        allowed = db.compute_allowed_cosmetics(rewards)
        updated = await db.set_user_cosmetics(conn, tg_user_id, body.model_dump(exclude_none=True), allowed)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    finally:
        await conn.close()
    return JSONResponse({"ok": True, "cosmetics": updated})


@app.get("/api/admin/supplements")
async def api_admin_supplements(
    q: str = "",
    is_active: str = "",
    evidence: str = "",
    page: int = 0,
    page_size: int = 20,
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    ok_perm, forbidden, _, _ = await _require_perm(tg_user_id, "content_edit")
    if not ok_perm:
        return forbidden
    active_filter = None
    if str(is_active).strip() in {"0", "1"}:
        active_filter = int(is_active)
    evidence_filter = str(evidence or "").strip().upper()
    if evidence_filter not in {"", "A", "B", "C", "D"}:
        evidence_filter = ""
    conn = await db.connect(s.DB_PATH)
    try:
        payload = await db.admin_search_supplements(
            conn,
            q=str(q or ""),
            is_active=active_filter,
            evidence_grade=(evidence_filter or None),
            page=max(0, int(page)),
            page_size=max(1, min(int(page_size), 100)),
        )
    finally:
        await conn.close()
    return JSONResponse({"ok": True, **payload})


@app.get("/api/admin/supplement/{supplement_id}")
async def api_admin_supplement(
    supplement_id: int,
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    ok_perm, forbidden, _, _ = await _require_perm(tg_user_id, "content_edit")
    if not ok_perm:
        return forbidden
    conn = await db.connect(s.DB_PATH)
    try:
        item = await db.get_supplement_admin(conn, int(supplement_id))
        if not item:
            return JSONResponse({"ok": False, "code": "NOT_FOUND"}, status_code=404)
        audit = await db.get_admin_audit_for_supplement(conn, int(supplement_id), limit=10)
    finally:
        await conn.close()
    return JSONResponse({"ok": True, "item": item, "audit": audit})


class AdminSetFieldBody(BaseModel):
    field: str
    value: str | None = None


@app.post("/api/admin/supplement/{supplement_id}/set_field")
async def api_admin_supplement_set_field(
    supplement_id: int,
    body: AdminSetFieldBody,
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    ok_perm, forbidden, role, _ = await _require_perm(tg_user_id, "content_edit")
    if not ok_perm:
        return forbidden
    safe_field = str(body.field or "")
    if role == db.ROLE_EDITOR and safe_field in {"evidence_grade", "confidence"}:
        return JSONResponse({"ok": False, "code": "PERMISSION_DENIED"}, status_code=403)
    conn = await db.connect(s.DB_PATH)
    try:
        await db.admin_set_supplement_field(conn, int(tg_user_id), int(supplement_id), safe_field, body.value)
    except ValueError as exc:
        return JSONResponse({"ok": False, "code": str(exc)}, status_code=400)
    finally:
        await conn.close()
    return JSONResponse({"ok": True})


class AdminToggleActiveBody(BaseModel):
    is_active: int


class AdminBulkBody(BaseModel):
    ids: list[int]
    op: str
    params: dict = {}


class AdminResolveDuplicateBody(BaseModel):
    keep_id: int
    remove_id: int


@app.post("/api/admin/supplement/{supplement_id}/toggle_active")
async def api_admin_supplement_toggle_active(
    supplement_id: int,
    body: AdminToggleActiveBody,
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    ok_perm, forbidden, _, _ = await _require_perm(tg_user_id, "content_edit")
    if not ok_perm:
        return forbidden
    target = 1 if int(body.is_active) else 0
    conn = await db.connect(s.DB_PATH)
    try:
        await db.admin_toggle_supplement_active(conn, int(tg_user_id), int(supplement_id), target)
    except ValueError as exc:
        return JSONResponse({"ok": False, "code": str(exc)}, status_code=400)
    finally:
        await conn.close()
    return JSONResponse({"ok": True})


@app.post("/api/admin/bulk/preview")
async def api_admin_bulk_preview(
    body: AdminBulkBody,
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    ok_perm, forbidden, _, _ = await _require_perm(tg_user_id, "content_bulk")
    if not ok_perm:
        return forbidden
    if not body.ids:
        return JSONResponse({"ok": False, "error": "VALIDATION"}, status_code=400)
    conn = await db.connect(s.DB_PATH)
    try:
        summary = await db.admin_bulk_apply(conn, int(tg_user_id), list(body.ids), str(body.op or ""), dict(body.params or {}), dry_run=True)
    except ValueError:
        return JSONResponse({"ok": False, "error": "VALIDATION"}, status_code=400)
    finally:
        await conn.close()
    return JSONResponse({"ok": True, **summary})


@app.post("/api/admin/bulk/apply")
async def api_admin_bulk_apply(
    body: AdminBulkBody,
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    ok_perm, forbidden, _, _ = await _require_perm(tg_user_id, "content_bulk")
    if not ok_perm:
        return forbidden
    if not body.ids:
        return JSONResponse({"ok": False, "error": "VALIDATION"}, status_code=400)
    conn = await db.connect(s.DB_PATH)
    try:
        summary = await db.admin_bulk_apply(conn, int(tg_user_id), list(body.ids), str(body.op or ""), dict(body.params or {}), dry_run=False)
    except ValueError:
        return JSONResponse({"ok": False, "error": "VALIDATION"}, status_code=400)
    finally:
        await conn.close()
    return JSONResponse({"ok": True, **summary})


@app.get("/api/admin/duplicates")
async def api_admin_duplicates(
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    ok_perm, forbidden, _, _ = await _require_perm(tg_user_id, "content_edit")
    if not ok_perm:
        return forbidden
    conn = await db.connect(s.DB_PATH)
    try:
        groups = await db.admin_list_name_duplicates(conn)
    finally:
        await conn.close()
    return JSONResponse({"ok": True, "groups": groups})


@app.post("/api/admin/duplicates/resolve")
async def api_admin_duplicates_resolve(
    body: AdminResolveDuplicateBody,
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    ok_perm, forbidden, _, _ = await _require_perm(tg_user_id, "content_edit")
    if not ok_perm:
        return forbidden
    conn = await db.connect(s.DB_PATH)
    try:
        await db.admin_resolve_name_duplicate(conn, int(tg_user_id), int(body.keep_id), int(body.remove_id))
    except ValueError as exc:
        return JSONResponse({"ok": False, "code": str(exc)}, status_code=400)
    finally:
        await conn.close()
    return JSONResponse({"ok": True})

@app.get("/api/settings")
async def api_settings_get(
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    conn = await db.connect(s.DB_PATH)
    try:
        tz_name = await db.get_user_tz(conn, tg_user_id)
    finally:
        await conn.close()
    return JSONResponse({"ok": True, "tz_name": str(tz_name or "UTC")})


@app.post("/api/settings/timezone")
async def api_settings_timezone_set(
    body: UserTimezoneBody,
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    tz_name = str(body.tz_name or "").strip().upper()
    if not tz_name.startswith("UTC"):
        return JSONResponse({"ok": False, "code": "VALIDATION"}, status_code=400)
    conn = await db.connect(s.DB_PATH)
    try:
        await db.set_user_tz(conn, tg_user_id, tz_name)
        await conn.commit()
    finally:
        await conn.close()
    return JSONResponse({"ok": True, "tz_name": tz_name})



@app.post("/api/billing/create_invoice_link")
async def api_billing_create_invoice_link(
    body: BillingInvoiceBody,
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    await _auth_user_id(x_telegram_init_data)
    plan_id = str(body.plan_id or "")
    method = str(body.method or "").lower()
    if plan_id not in s.PREMIUM_PLANS:
        raise HTTPException(status_code=400, detail="Unknown plan")
    if method not in {"stars", "yookassa"}:
        raise HTTPException(status_code=400, detail="Unknown method")

    payload = f"sub:{plan_id}:{method}:{db.utc_now_iso()}"
    req = {
        "title": "Premium подписка",
        "description": "Доступ к WebApp + AI + расширенным функциям",
        "payload": payload,
        "need_shipping_address": False,
        "is_flexible": False,
    }
    if method == "stars":
        if not s.BILLING_ENABLED_STARS:
            raise HTTPException(status_code=400, detail="Stars disabled")
        req["currency"] = "XTR"
        req["provider_token"] = ""
        req["prices"] = [{"label": "Premium", "amount": int(s.PREMIUM_PRICES_STARS.get(plan_id, 0) or 0)}]
    else:
        if not s.BILLING_ENABLED_RUB or not s.YOOKASSA_PROVIDER_TOKEN:
            raise HTTPException(status_code=400, detail="RUB disabled")
        req["currency"] = "RUB"
        req["provider_token"] = s.YOOKASSA_PROVIDER_TOKEN
        req["prices"] = [{"label": "Premium", "amount": int(s.PREMIUM_PRICES_RUB.get(plan_id, 0) or 0)}]

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/createInvoiceLink"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, json=req)
    data = resp.json()
    if not data.get("ok"):
        raise HTTPException(status_code=400, detail=str(data))
    return JSONResponse({"ok": True, "invoice_link": data.get("result")})


async def _today_payload(tg_user_id: int) -> dict:
    conn = await db.connect(s.DB_PATH)
    try:
        tz_name = await db.get_user_tz(conn, tg_user_id)
        day = db.get_user_local_date(tz_name or None)
        groups = await db.get_today_plan_groups(conn, tg_user_id, day)
        status_map = await db.get_day_status_map(conn, tg_user_id, day)
        data_slots = []
        for group in groups:
            items = []
            for item in list(group.get("items") or []):
                sid = int(item.get("supplement_id") or 0)
                items.append({"supplement_id": sid, "name": str(item.get("name_ru") or item.get("slug") or ""), "slug": str(item.get("slug") or ""), "status": status_map.get(sid, "none")})
            complexes = []
            for c in list(group.get("complexes") or []):
                c_items = []
                for ci in list(c.get("items") or []):
                    sid = int(ci.get("supplement_id") or 0)
                    c_items.append({"supplement_id": sid, "name": str(ci.get("name_ru") or ci.get("slug") or ""), "slug": str(ci.get("slug") or ""), "status": status_map.get(sid, "none")})
                taken = sum(1 for ci in c_items if ci.get("status") == "taken")
                total = len(c_items)
                c_status = "none" if taken == 0 else ("taken" if taken == total and total > 0 else "partial")
                complexes.append({
                    "complex_id": int(c.get("complex_id") or 0),
                    "name": str(c.get("name") or ""),
                    "status": c_status,
                    "taken_count": taken,
                    "total_count": total,
                    "items": c_items,
                })
            data_slots.append({"slot_id": str(group.get("slot_id") or ""), "title": str(group.get("title") or "Слот"), "time_local": str(group.get("time") or "--:--"), "complexes": complexes, "items": items})
        return {"day": day, "slots": data_slots}
    finally:
        await conn.close()




class AdminUserBanBody(BaseModel):
    is_banned: int
    reason: str = ""


@app.get("/api/admin/user/{tg_user_id}")
async def api_admin_user_get(
    tg_user_id: int,
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    admin_tg_id = await _auth_user_id(x_telegram_init_data)
    ok_perm, forbidden, _, _ = await _require_perm(admin_tg_id, "users_view")
    if not ok_perm:
        return forbidden
    conn = await db.connect(s.DB_PATH)
    try:
        item = await db.admin_get_user(conn, int(tg_user_id))
    finally:
        await conn.close()
    if not item:
        return JSONResponse({"ok": False, "code": "NOT_FOUND"}, status_code=404)
    return JSONResponse({"ok": True, "item": item})


@app.post("/api/admin/user/{tg_user_id}/ban")
async def api_admin_user_ban(
    tg_user_id: int,
    body: AdminUserBanBody,
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    admin_tg_id = await _auth_user_id(x_telegram_init_data)
    ok_perm, forbidden, _, _ = await _require_perm(admin_tg_id, "users_ban")
    if not ok_perm:
        return forbidden
    conn = await db.connect(s.DB_PATH)
    try:
        await db.admin_set_ban(conn, int(admin_tg_id), int(tg_user_id), int(body.is_banned), str(body.reason or ""))
    except ValueError as exc:
        return JSONResponse({"ok": False, "code": str(exc)}, status_code=404)
    finally:
        await conn.close()
    return JSONResponse({"ok": True})


@app.post("/api/admin/user/{tg_user_id}/delete")
async def api_admin_user_delete(
    tg_user_id: int,
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    admin_tg_id = await _auth_user_id(x_telegram_init_data)
    ok_perm, forbidden, _, _ = await _require_perm(admin_tg_id, "users_delete")
    if not ok_perm:
        return forbidden
    conn = await db.connect(s.DB_PATH)
    try:
        await db.admin_delete_user(conn, int(admin_tg_id), int(tg_user_id))
    except ValueError as exc:
        return JSONResponse({"ok": False, "code": str(exc)}, status_code=404)
    finally:
        await conn.close()
    return JSONResponse({"ok": True})


class AdminRoleSetBody(BaseModel):
    tg_user_id: int
    role: str
    note: str = ""


@app.get("/api/admin/roles/get")
async def api_admin_roles_get(
    tg_user_id: int,
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    actor_tg_id = await _auth_user_id(x_telegram_init_data)
    ok_perm, forbidden, _, _ = await _require_perm(actor_tg_id, "roles_manage")
    if not ok_perm:
        return forbidden
    conn = await db.connect(s.DB_PATH)
    try:
        role = await db.get_role(conn, int(tg_user_id))
    finally:
        await conn.close()
    return JSONResponse({"ok": True, "tg_user_id": int(tg_user_id), "role": role})


@app.post("/api/admin/roles/set")
async def api_admin_roles_set(
    body: AdminRoleSetBody,
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    actor_tg_id = await _auth_user_id(x_telegram_init_data)
    ok_perm, forbidden, _, _ = await _require_perm(actor_tg_id, "roles_manage")
    if not ok_perm:
        return forbidden
    target_id = int(body.tg_user_id)
    role = str(body.role or "").strip().lower()
    conn = await db.connect(s.DB_PATH)
    try:
        if role == db.ROLE_NONE:
            await db.remove_role(conn, int(actor_tg_id), target_id)
        else:
            await db.set_role(conn, int(actor_tg_id), target_id, role, str(body.note or ""))
        await conn.commit()
        current_role = await db.get_role(conn, target_id)
    except ValueError as exc:
        return JSONResponse({"ok": False, "code": str(exc)}, status_code=400)
    finally:
        await conn.close()
    return JSONResponse({"ok": True, "tg_user_id": target_id, "role": current_role})

@app.post("/api/admin/import_purchase_urls")
async def api_admin_import_purchase_urls(
    raw_json_text: str = Body(..., media_type="text/plain"),
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    ok_perm, forbidden, _, _ = await _require_perm(tg_user_id, "content_edit")
    if not ok_perm:
        return forbidden
    conn = await db.connect(s.DB_PATH)
    try:
        report = await db.admin_import_purchase_urls(conn, int(tg_user_id), raw_json_text)
    except ValueError as exc:
        return JSONResponse({"ok": False, "code": str(exc)}, status_code=400)
    finally:
        await conn.close()
    return JSONResponse({"ok": True, **report})


@app.get("/api/today")
async def api_today(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "code": "PREMIUM_REQUIRED"}, status_code=402)
    payload = await _today_payload(tg_user_id)
    return JSONResponse({"ok": True, **payload})


@app.post("/api/today/toggle")
async def api_today_toggle(body: TodayToggleBody, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "code": "PREMIUM_REQUIRED"}, status_code=402)
    if body.status not in {"taken", "skipped", "none"}:
        raise HTTPException(status_code=400, detail="status must be taken/skipped/none")
    conn = await db.connect(s.DB_PATH)
    try:
        tz_name = await db.get_user_tz(conn, tg_user_id)
        day = db.get_user_local_date(tz_name or None)
        await db.set_day_status(conn, tg_user_id, day, int(body.supplement_id), body.status)
        newly = await db.evaluate_and_unlock_achievements(conn, tg_user_id, day)
        newly_unlocked_details = await _build_newly_unlocked_details(conn, newly)
    finally:
        await conn.close()
    return JSONResponse({"ok": True, "newly_unlocked": newly, "newly_unlocked_details": newly_unlocked_details})





@app.post("/api/today/toggle_complex")
async def api_today_toggle_complex(body: TodayToggleComplexBody, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "code": "PREMIUM_REQUIRED"}, status_code=402)
    if body.status not in {"taken", "none"}:
        raise HTTPException(status_code=400, detail="status must be taken/none")
    conn = await db.connect(s.DB_PATH)
    try:
        tz_name = await db.get_user_tz(conn, tg_user_id)
        day = db.get_user_local_date(tz_name or None)
        await db.toggle_complex_for_day(conn, tg_user_id, day, int(body.complex_id), body.status)
        newly = await db.evaluate_and_unlock_achievements(conn, tg_user_id, day)
        newly_unlocked_details = await _build_newly_unlocked_details(conn, newly)
    finally:
        await conn.close()
    return JSONResponse({"ok": True, "newly_unlocked": newly, "newly_unlocked_details": newly_unlocked_details})


@app.get("/api/complexes")
async def api_complexes(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "code": "PREMIUM_REQUIRED"}, status_code=402)
    conn = await db.connect(s.DB_PATH)
    try:
        items = await db.list_complexes(conn, tg_user_id)
    finally:
        await conn.close()
    return JSONResponse({"ok": True, "items": items})


@app.post("/api/complexes/create")
async def api_complexes_create(body: ComplexCreateBody, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "code": "PREMIUM_REQUIRED"}, status_code=402)
    conn = await db.connect(s.DB_PATH)
    try:
        item = await db.create_complex(conn, tg_user_id, body.name, body.note or "")
    finally:
        await conn.close()
    return JSONResponse({"ok": True, "item": item})


@app.post("/api/complexes/rename")
async def api_complexes_rename(body: ComplexRenameBody, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "code": "PREMIUM_REQUIRED"}, status_code=402)
    conn = await db.connect(s.DB_PATH)
    try:
        await db.rename_complex(conn, tg_user_id, int(body.complex_id), body.new_name)
    finally:
        await conn.close()
    return JSONResponse({"ok": True})


@app.post("/api/complexes/delete")
async def api_complexes_delete(body: ComplexDeleteBody, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "code": "PREMIUM_REQUIRED"}, status_code=402)
    conn = await db.connect(s.DB_PATH)
    try:
        await db.delete_complex(conn, tg_user_id, int(body.complex_id))
    finally:
        await conn.close()
    return JSONResponse({"ok": True})


@app.get("/api/complexes/{complex_id}/items")
async def api_complex_items(complex_id: int, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "code": "PREMIUM_REQUIRED"}, status_code=402)
    conn = await db.connect(s.DB_PATH)
    try:
        items = await db.list_complex_items(conn, tg_user_id, int(complex_id))
    finally:
        await conn.close()
    return JSONResponse({"ok": True, "items": items})


@app.post("/api/complexes/{complex_id}/items/add")
async def api_complex_items_add(complex_id: int, body: ComplexItemBody, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "code": "PREMIUM_REQUIRED"}, status_code=402)
    conn = await db.connect(s.DB_PATH)
    try:
        await db.add_item_to_complex(conn, tg_user_id, int(complex_id), int(body.supplement_id), body.dose_override or "")
    finally:
        await conn.close()
    return JSONResponse({"ok": True})


@app.post("/api/complexes/{complex_id}/items/remove")
async def api_complex_items_remove(complex_id: int, body: ComplexItemBody, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "code": "PREMIUM_REQUIRED"}, status_code=402)
    conn = await db.connect(s.DB_PATH)
    try:
        await db.remove_item_from_complex(conn, tg_user_id, int(complex_id), int(body.supplement_id))
    finally:
        await conn.close()
    return JSONResponse({"ok": True})


@app.get("/api/complex_schedules")
async def api_complex_schedules(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "code": "PREMIUM_REQUIRED"}, status_code=402)
    conn = await db.connect(s.DB_PATH)
    try:
        items = await db.list_complex_schedules(conn, tg_user_id)
    finally:
        await conn.close()
    return JSONResponse({"ok": True, "items": items})


@app.post("/api/complex_schedules/set")
async def api_complex_schedules_set(body: ComplexScheduleSetBody, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "code": "PREMIUM_REQUIRED"}, status_code=402)
    conn = await db.connect(s.DB_PATH)
    try:
        await db.set_complex_schedule(conn, tg_user_id, int(body.complex_id), body.slot_id, body.time_local)
    finally:
        await conn.close()
    return JSONResponse({"ok": True})


@app.post("/api/complex_schedules/remove")
async def api_complex_schedules_remove(body: ComplexScheduleRemoveBody, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "code": "PREMIUM_REQUIRED"}, status_code=402)
    conn = await db.connect(s.DB_PATH)
    try:
        await db.remove_complex_schedule(conn, tg_user_id, int(body.complex_id), body.slot_id)
    finally:
        await conn.close()
    return JSONResponse({"ok": True})




@app.get("/api/stats/summary")
async def api_stats_summary(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    conn = await db.connect(s.DB_PATH)
    try:
        tz_name = await db.get_user_tz(conn, tg_user_id)
        day = db.get_user_local_date(tz_name or None)
        compact = await db.build_stats_compact(conn, tg_user_id, day)
        misses_full = await db.get_top_misses(conn, tg_user_id, day, 7, limit=5)
        stack_ids = await db.get_user_stack_supplement_ids(conn, tg_user_id)
        streaks_full = await db.get_streaks(conn, tg_user_id, day, stack_ids, max_back=90)
    finally:
        await conn.close()

    misses_map = {str(x.get("slug") or ""): str(x.get("name_ru") or "") for x in misses_full}
    streaks_map = {str(x.get("slug") or ""): str(x.get("name_ru") or "") for x in streaks_full}

    summary = dict(compact)
    summary["top_misses_7"] = [
        {"slug": str(x.get("slug") or ""), "name_ru": misses_map.get(str(x.get("slug") or ""), ""), "missed": int(x.get("missed") or 0)}
        for x in list(compact.get("top_misses_7") or [])
    ]
    summary["top_streaks"] = [
        {"slug": str(x.get("slug") or ""), "name_ru": streaks_map.get(str(x.get("slug") or ""), ""), "streak": int(x.get("streak") or 0)}
        for x in list(compact.get("top_streaks") or [])
    ]
    return JSONResponse({"ok": True, "summary": summary, "date_local": target_date})


@app.get("/api/body_map")
async def api_body_map(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "code": "PREMIUM_REQUIRED"}, status_code=402)

    conn = await db.connect(s.DB_PATH)
    try:
        tz_name = await db.get_user_tz(conn, tg_user_id)
        day = db.get_user_local_date(tz_name or None)
        payload = await db.body_map_aggregate(conn, tg_user_id, day)
    finally:
        await conn.close()
    return JSONResponse({"ok": True, **payload})

@app.get("/api/catalog")
async def api_catalog(
    q: str = "",
    page: int = 0,
    page_size: int = 20,
    evidence: str = "",
    targets: str = "",
    burdens: str = "",
    sort: str = "",
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "error": "PREMIUM_REQUIRED", "code": "PREMIUM_REQUIRED"}, status_code=402)

    evidence_list = [item.strip().upper() for item in evidence.split(",") if item.strip()]
    targets_list = [item.strip().lower() for item in targets.split(",") if item.strip()]
    burdens_list = [item.strip().lower() for item in burdens.split(",") if item.strip()]

    conn = await db.connect(s.DB_PATH)
    try:
        payload = await db.catalog_search(
            conn,
            tg_user_id=tg_user_id,
            q=(q or "").strip() or None,
            evidence=evidence_list or None,
            targets=targets_list or None,
            burdens=burdens_list or None,
            sort=(sort or "").strip() or None,
            page=page,
            page_size=page_size,
        )
    finally:
        await conn.close()
    return JSONResponse({"ok": True, **payload})


@app.get("/api/supplement/{slug}")
async def api_supplement_detail(slug: str, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "error": "PREMIUM_REQUIRED", "code": "PREMIUM_REQUIRED"}, status_code=402)

    conn = await db.connect(s.DB_PATH)
    try:
        item = await db.get_supplement_detail_for_user(conn, tg_user_id, slug)
    finally:
        await conn.close()

    if not item:
        return JSONResponse({"ok": False, "error": "NOT_FOUND"}, status_code=404)
    return JSONResponse({"ok": True, "item": item})




@app.get("/api/my_supplements")
async def api_my_supplements(
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "error": "PREMIUM_REQUIRED", "code": "PREMIUM_REQUIRED"}, status_code=402)

    conn = await db.connect(s.DB_PATH)
    try:
        items = await db.get_user_supplements_with_enabled(conn, tg_user_id)
    finally:
        await conn.close()

    active = [x for x in items if int(x.get("enabled", 0)) == 1]
    frozen = [x for x in items if int(x.get("enabled", 0)) == 0]
    return JSONResponse({"ok": True, "active": active, "frozen": frozen})


@app.post("/api/my_supplements/freeze")
async def api_my_supplements_freeze(
    body: MySuppFreezeBody,
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "error": "PREMIUM_REQUIRED", "code": "PREMIUM_REQUIRED"}, status_code=402)

    enabled = 1 if int(body.enabled) else 0

    conn = await db.connect(s.DB_PATH)
    try:
        user_id = await db.get_or_create_user(conn, tg_user_id, tz_name="")
        updated = await db.set_user_supp_enabled(conn, user_id, int(body.supplement_id), enabled)
    finally:
        await conn.close()

    if not updated:
        return JSONResponse({"ok": False, "error": "NOT_FOUND"}, status_code=404)
    return JSONResponse({"ok": True, "supplement_id": int(body.supplement_id), "enabled": enabled})

@app.post("/api/my_supplements/update")
async def api_my_supplements_update(
    body: MySuppUpdateBody,
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "error": "PREMIUM_REQUIRED", "code": "PREMIUM_REQUIRED"}, status_code=402)

    conn = await db.connect(s.DB_PATH)
    try:
        updated = await db.set_user_supp_fields(
            conn,
            tg_user_id=tg_user_id,
            supplement_id=int(body.supplement_id),
            dose_text=str(body.dose_text or "").strip(),
            note_text=str(body.note_text or "").strip(),
        )
    finally:
        await conn.close()

    if not updated:
        return JSONResponse({"ok": False, "error": "NOT_FOUND"}, status_code=404)
    return JSONResponse({"ok": True})


@app.get("/api/reminders/for_supplement")
async def api_reminders_for_supplement(
    supplement_id: int,
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "error": "PREMIUM_REQUIRED", "code": "PREMIUM_REQUIRED"}, status_code=402)

    conn = await db.connect(s.DB_PATH)
    try:
        times = await db.list_reminders_for_supplement(conn, tg_user_id, int(supplement_id))
    finally:
        await conn.close()
    return JSONResponse({"ok": True, "times": times})


@app.post("/api/reminders/set")
async def api_reminders_set(
    body: ReminderSetBody,
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "error": "PREMIUM_REQUIRED", "code": "PREMIUM_REQUIRED"}, status_code=402)

    mode = str(body.mode or "").strip().lower()
    if mode not in {"slot", "custom"}:
        return JSONResponse({"ok": False, "error": "INVALID_MODE"}, status_code=400)

    conn = await db.connect(s.DB_PATH)
    try:
        await db.set_reminders_for_supplement(
            conn,
            tg_user_id=tg_user_id,
            supplement_id=int(body.supplement_id),
            mode=mode,
            times=list(body.times or []),
        )
        times = await db.list_reminders_for_supplement(conn, tg_user_id, int(body.supplement_id))
    finally:
        await conn.close()

    return JSONResponse({"ok": True, "mode": mode, "times": times})


@app.post("/api/my_supplements/toggle")
async def api_my_supplements_toggle(
    body: CatalogToggleBody,
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "error": "PREMIUM_REQUIRED", "code": "PREMIUM_REQUIRED"}, status_code=402)

    action = str(body.action or "").strip().lower()
    if action not in {"add", "remove"}:
        return JSONResponse({"ok": False, "error": "INVALID_ACTION"}, status_code=400)

    conn = await db.connect(s.DB_PATH)
    try:
        if action == "add":
            await db.add_user_supplement(conn, tg_user_id, int(body.supplement_id))
            in_stack = True
        else:
            await db.remove_user_supplement(conn, tg_user_id, int(body.supplement_id))
            in_stack = False
    finally:
        await conn.close()

    return JSONResponse({"ok": True, "in_stack": in_stack})


@app.post("/api/my_supplements/remove")
async def api_my_supplements_remove(
    body: MySuppDeleteBody,
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "error": "PREMIUM_REQUIRED", "code": "PREMIUM_REQUIRED"}, status_code=402)

    conn = await db.connect(s.DB_PATH)
    try:
        await db.remove_user_supplement(conn, tg_user_id, int(body.supplement_id))
    finally:
        await conn.close()
    return JSONResponse({"ok": True})


@app.get("/api/my_supplements/history")
async def api_my_supplements_history(
    supplement_id: int,
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "error": "PREMIUM_REQUIRED", "code": "PREMIUM_REQUIRED"}, status_code=402)

    conn = await db.connect(s.DB_PATH)
    try:
        payload = await db.get_user_supplement_intake_history(conn, tg_user_id, int(supplement_id), limit=20)
    finally:
        await conn.close()
    return JSONResponse({"ok": True, **payload})


@app.get("/api/schedule")
async def api_schedule(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "code": "PREMIUM_REQUIRED"}, status_code=402)
    conn = await db.connect(s.DB_PATH)
    try:
        user_id = await db.get_or_create_user(conn, tg_user_id, tz_name="")
        slots = await db.get_user_slots(conn, user_id)
        schedules_map = await db.get_user_schedule_map(conn, user_id)
        schedules = [{"supplement_id": sid, "slot_id": str(data.get("slot_id") or "morning"), "time_hhmm": data.get("time_hhmm")} for sid, data in schedules_map.items()]
    finally:
        await conn.close()
    return JSONResponse({"ok": True, "slots": slots, "schedules": schedules})


@app.post("/api/schedule/slot_time")
async def api_schedule_slot_time(body: SlotTimeBody, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "code": "PREMIUM_REQUIRED"}, status_code=402)
    conn = await db.connect(s.DB_PATH)
    try:
        user_id = await db.get_or_create_user(conn, tg_user_id, tz_name="")
        await db.set_user_slot_time(conn, user_id, body.slot_id, body.time_local)
    finally:
        await conn.close()
    return JSONResponse({"ok": True})


@app.post("/api/schedule/add_slot")
async def api_schedule_add_slot(body: AddSlotBody, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "code": "PREMIUM_REQUIRED"}, status_code=402)
    conn = await db.connect(s.DB_PATH)
    try:
        user_id = await db.get_or_create_user(conn, tg_user_id, tz_name="")
        slot_id = await db.add_custom_slot(conn, user_id, body.time_local, body.title_ru)
    finally:
        await conn.close()
    return JSONResponse({"ok": True, "slot_id": slot_id})


@app.post("/api/schedule/set")
async def api_schedule_set(body: ScheduleSetBody, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "code": "PREMIUM_REQUIRED"}, status_code=402)
    conn = await db.connect(s.DB_PATH)
    try:
        user_id = await db.get_or_create_user(conn, tg_user_id, tz_name="")
        await db.set_schedule_slot(conn, user_id, int(body.supplement_id), body.slot_id)
        await db.set_schedule_time_override(conn, user_id, int(body.supplement_id), body.time_hhmm)
    finally:
        await conn.close()
    return JSONResponse({"ok": True})


def _validate_answers(payload: dict) -> dict:
    energy = str(payload.get("energy") or "").strip().lower()
    sleep = str(payload.get("sleep") or "").strip().lower()
    stress = str(payload.get("stress") or "").strip().lower()
    training = str(payload.get("training") or "").strip().lower()
    goals = [str(x).strip().lower() for x in list(payload.get("goals") or []) if str(x).strip()]
    note = str(payload.get("note") or "").strip()[:280]
    if energy not in {"low", "ok", "high"}:
        raise HTTPException(status_code=400, detail="invalid energy")
    if sleep not in {"poor", "ok", "great"}:
        raise HTTPException(status_code=400, detail="invalid sleep")
    if stress not in {"low", "medium", "high"}:
        raise HTTPException(status_code=400, detail="invalid stress")
    if training not in {"none", "light", "hard"}:
        raise HTTPException(status_code=400, detail="invalid training")
    allowed_goals = {"brain", "mood", "sleep", "recovery", "gut", "immune", "joints", "performance"}
    goals = [g for g in goals if g in allowed_goals][:8]
    return {"energy": energy, "sleep": sleep, "stress": stress, "training": training, "goals": goals, "note": note}


def _prompt_hash(answers: dict, ctx: dict) -> str:
    raw = json.dumps({"answers": answers, "ctx": ctx}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _ai_recommendations(answers: dict, ctx: dict) -> tuple[list[dict], dict]:
    if not s.OPENAI_API_KEY:
        raise RuntimeError("AI_UNAVAILABLE")
    cfg = OpenAIConfig(api_key=s.OPENAI_API_KEY, model=s.OPENAI_MODEL or "gpt-4o-mini")
    instructions = (
        "Ты ассистент по БАДам. Верни строго JSON-массив, без markdown и лишнего текста. "
        "Формат каждого элемента: action, slug, name_ru, reason_ru, priority, suggested_time, suggested_slot_id, prefer_existing_time. "
        "action только add/remove/adjust. priority только 1..3. "
        "suggested_time обязателен и строго в формате HH:MM. prefer_existing_time всегда true: выбирай из уже существующих слотов пользователя, если возможно. "
        "Консервативный стиль, без медицинских заявлений и без рецептурных препаратов. "
        "Поле answers.note трактуй как приоритетный свободный запрос пользователя и явно учитывай его в причине каждой рекомендации. "
        "Контекст context.last7 и context.stats содержит историю прошлых и сегодняшних приёмов — обязательно используй её."
    )
    prompt = json.dumps({"answers": answers, "context": ctx}, ensure_ascii=False)
    text = await __import__('asyncio').to_thread(_sync_call, cfg, instructions=instructions, input_text=prompt, max_output_tokens=800, timeout_s=20)
    data = json.loads(text)
    if not isinstance(data, list):
        raise RuntimeError("AI_BAD_FORMAT")
    return data, {"model": cfg.model}


async def _normalize_recommendations(conn, recs: list[dict]) -> list[dict]:
    out = []
    for item in recs:
        slug = str(item.get("slug") or "").strip().lower()
        if not slug:
            continue
        row = await db.fetchone(conn, "SELECT id, COALESCE(NULLIF(name_ru,''), NULLIF(name_en,''), name) AS name_ru, slug FROM supplements WHERE slug=?", (slug,))
        if not row:
            continue
        action = str(item.get("action") or "add").strip().lower()
        if action not in {"add", "remove", "adjust"}:
            action = "add"
        suggested_time = db._normalize_hhmm(str(item.get("suggested_time") or "").strip())
        suggested_slot_id = str(item.get("suggested_slot_id") or "").strip() or None
        out.append({
            "action": action,
            "slug": str(row["slug"]),
            "name_ru": str(item.get("name_ru") or row["name_ru"] or ""),
            "reason": str(item.get("reason_ru") or "").strip(),
            "priority": min(3, max(1, int(item.get("priority") or 2))),
            "suggested_time": suggested_time,
            "suggested_slot_id": suggested_slot_id,
            "prefer_existing_time": True,
            "supplement_id": int(row["id"]),
        })
        if len(out) >= 12:
            break
    return out


def _resolve_target_day(tz_name: str | None, target_raw: str | None) -> tuple[str, str, object]:
    now_local = db.local_now(tz_name or None)
    target = str(target_raw or "auto").strip().lower() or "auto"
    if target not in {"today", "tomorrow", "auto"}:
        target = "auto"
    if target == "auto":
        target = "tomorrow" if now_local.hour >= 18 else "today"
    local_date = (now_local.date() + timedelta(days=1)).isoformat() if target == "tomorrow" else now_local.date().isoformat()
    return target, local_date, now_local


@app.post("/api/checkin")
async def api_checkin(body: CheckinBody, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "code": "PREMIUM_REQUIRED"}, status_code=402)

    answers = _validate_answers(body.answers or {})
    conn = await db.connect(s.DB_PATH)
    try:
        tz_name = await db.get_user_tz(conn, tg_user_id)
        target_raw = body.target_day if body.target_day is not None else body.target
        target, local_date, now_local = _resolve_target_day(tz_name, target_raw)
        checkin_id = await db.upsert_daily_checkin(conn, tg_user_id, local_date, answers)
        ctx = await db.build_ai_context_compact(conn, tg_user_id, local_date)
        ctx["now_local_hhmm"] = now_local.strftime("%H:%M")
        ctx["target_day"] = target
        if body.client_local_iso:
            ctx["client_local_iso"] = body.client_local_iso
        p_hash = _prompt_hash(answers, ctx)
        try:
            recs_raw, meta = await _ai_recommendations(answers, ctx)
            recs = await _normalize_recommendations(conn, recs_raw)
            await db.save_ai_result(conn, checkin_id, recs, p_hash)
            return JSONResponse({"ok": True, "checkin_id": checkin_id, "recommendations": recs, "meta": meta, "date_local": local_date, "target": target})
        except Exception:
            await db.save_ai_result(conn, checkin_id, {"error": "AI_UNAVAILABLE"}, p_hash)
            return JSONResponse({"ok": False, "checkin_id": checkin_id, "error": "AI_UNAVAILABLE"})
    finally:
        await conn.close()


@app.post("/api/checkin/apply")
async def api_checkin_apply(body: CheckinApplyBody, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "code": "PREMIUM_REQUIRED"}, status_code=402)
    conn = await db.connect(s.DB_PATH)
    try:
        checkin = await db.get_daily_checkin_by_id(conn, int(body.checkin_id), tg_user_id)
        if not checkin:
            return JSONResponse({"ok": False, "error": "NOT_FOUND"}, status_code=404)
        recs = json.loads(checkin.get("ai_raw_json") or "[]") if checkin.get("ai_raw_json") else []
        if not isinstance(recs, list):
            recs = []
        if body.mode == "all":
            picked = recs
        else:
            selected = set(int(x) for x in (body.selected_ids or []))
            picked = [x for x in recs if int(x.get("supplement_id") or 0) in selected]
        tz_name = await db.get_user_tz(conn, tg_user_id)
        _, local_date, _ = _resolve_target_day(tz_name, None)
        target_date = str(checkin.get("date_local") or "").strip() or local_date
        user_id = await db.get_or_create_user(conn, tg_user_id, tz_name="")
        prepared, created_times = await engine.assign_slots_by_time(
            conn,
            user_id,
            picked,
            action_field="action",
            add_actions={"add", "adjust"},
            time_field="suggested_time",
            slot_field="suggested_slot_id",
        )
        summary = await db.apply_recommendations_to_today(conn, tg_user_id, target_date, prepared, mode=(body.mode or "selected"))
        reminders_added = await engine.ensure_supplement_reminders(
            conn,
            tg_user_id,
            prepared,
            action_field="action",
            add_actions={"add", "adjust"},
            supplement_field="supplement_id",
            time_field="suggested_time",
            only_times=created_times,
        )
        summary["new_time_groups"] = sorted(created_times)
        summary["reminders_added"] = reminders_added
        newly = await db.evaluate_and_unlock_achievements(conn, tg_user_id, target_date)
        newly_unlocked_details = await _build_newly_unlocked_details(conn, newly)
    finally:
        await conn.close()
    return JSONResponse({
        "ok": True,
        "summary": summary,
        "date_local": target_date,
        "newly_unlocked": newly,
        "newly_unlocked_details": newly_unlocked_details,
    })


@app.post("/api/recommendations/apply_all")
async def api_recommendations_apply_all(body: RecommendationsApplyBody, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "code": "PREMIUM_REQUIRED"}, status_code=402)
    recs = list(body.recommendations or [])
    if len(recs) > 60:
        return JSONResponse({"ok": False, "error": "TOO_MANY_RECOMMENDATIONS"}, status_code=400)
    conn = await db.connect(s.DB_PATH)
    try:
        tz_name = await db.get_user_tz(conn, tg_user_id)
        local_date = str(body.local_date or "").strip() or db.get_user_local_date(tz_name or None)
        user_id = await db.get_or_create_user(conn, tg_user_id, tz_name="")
        prepared, created_times = await engine.assign_slots_by_time(
            conn,
            user_id,
            recs,
            action_field="action",
            add_actions={"add", "adjust"},
            time_field="suggested_time",
            slot_field="suggested_slot_id",
        )
        summary = await db.apply_recommendations_to_today(conn, tg_user_id, local_date, prepared, mode="apply_all")
        reminders_added = await engine.ensure_supplement_reminders(
            conn,
            tg_user_id,
            prepared,
            action_field="action",
            add_actions={"add", "adjust"},
            supplement_field="supplement_id",
            time_field="suggested_time",
            only_times=created_times,
        )
        summary["new_time_groups"] = sorted(created_times)
        summary["reminders_added"] = reminders_added
    finally:
        await conn.close()
    today = await _today_payload(tg_user_id)
    return JSONResponse({"ok": True, "summary": summary, "today": today})


@app.get("/api/checkin/today")
async def api_checkin_today(target: str | None = None, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "code": "PREMIUM_REQUIRED"}, status_code=402)
    conn = await db.connect(s.DB_PATH)
    try:
        tz_name = await db.get_user_tz(conn, tg_user_id)
        resolved_target, local_date, _ = _resolve_target_day(tz_name, target)
        row = await db.get_today_checkin(conn, tg_user_id, local_date)
    finally:
        await conn.close()
    if not row:
        return JSONResponse({"ok": True, "checkin": None, "date_local": local_date, "target": resolved_target})
    recs = []
    try:
        parsed = json.loads(row.get("ai_raw_json") or "[]")
        recs = parsed if isinstance(parsed, list) else []
    except Exception:
        recs = []
    answers = {}
    try:
        answers = json.loads(row.get("answers_json") or "{}")
    except Exception:
        pass
    return JSONResponse({"ok": True, "checkin": {"id": int(row.get("id") or 0), "answers": answers, "recommendations": recs}, "date_local": local_date, "target": resolved_target})


@app.post("/api/admin/diagnostics/run")
async def api_admin_diagnostics_run(
    body: DiagnosticsRunBody,
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    ok_perm, forbidden, _, _ = await _require_perm(tg_user_id, "system_diag")
    if not ok_perm:
        return forbidden
    conn = await db.connect(s.DB_PATH)
    try:
        payload = await db.run_diagnostics(conn, apply=bool(body.apply))
    finally:
        await conn.close()
    return JSONResponse(payload)
