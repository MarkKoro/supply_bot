import hashlib
import json
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from telegram_webapp_auth import verify_init_data
import db
from settings import get_settings
from openai_client import OpenAIConfig, _sync_call

BASE_DIR = Path(__file__).resolve().parent
WEBAPP_DIR = BASE_DIR / "webapp"

app = FastAPI(title="SupplyBot WebApp")
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


class CheckinBody(BaseModel):
    answers: dict


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


@app.exception_handler(HTTPException)
async def http_exception_handler(_, exc: HTTPException):
    if exc.status_code == 403 and isinstance(exc.detail, dict) and exc.detail.get("code") == "BANNED":
        payload = {"ok": False, "code": "BANNED", "reason": str(exc.detail.get("reason") or "")}
        return JSONResponse(payload, status_code=403)
    return await http_exception_handler(_, exc)


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
    })


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
    finally:
        await conn.close()
    return JSONResponse({"ok": True})





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
    finally:
        await conn.close()
    return JSONResponse({"ok": True})


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
    return JSONResponse({"ok": True, "summary": summary})


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
        "Формат каждого элемента: action, slug, name_ru, reason_ru, priority, suggested_time, suggested_slot_id. "
        "action только add/remove/adjust. priority только 1..3. "
        "Консервативный стиль, без медицинских заявлений и без рецептурных препаратов."
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
        suggested_time = str(item.get("suggested_time") or "").strip() or None
        suggested_slot_id = str(item.get("suggested_slot_id") or "").strip() or None
        out.append({
            "action": action,
            "slug": str(row["slug"]),
            "name_ru": str(item.get("name_ru") or row["name_ru"] or ""),
            "reason": str(item.get("reason_ru") or "").strip(),
            "priority": min(3, max(1, int(item.get("priority") or 2))),
            "suggested_time": suggested_time,
            "suggested_slot_id": suggested_slot_id,
            "supplement_id": int(row["id"]),
        })
        if len(out) >= 12:
            break
    return out


@app.post("/api/checkin")
async def api_checkin(body: CheckinBody, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "code": "PREMIUM_REQUIRED"}, status_code=402)

    answers = _validate_answers(body.answers or {})
    conn = await db.connect(s.DB_PATH)
    try:
        tz_name = await db.get_user_tz(conn, tg_user_id)
        local_date = db.get_user_local_date(tz_name or None)
        checkin_id = await db.upsert_daily_checkin(conn, tg_user_id, local_date, answers)
        ctx = await db.build_ai_context_compact(conn, tg_user_id, local_date)
        p_hash = _prompt_hash(answers, ctx)
        try:
            recs_raw, meta = await _ai_recommendations(answers, ctx)
            recs = await _normalize_recommendations(conn, recs_raw)
            await db.save_ai_result(conn, checkin_id, recs, p_hash)
            return JSONResponse({"ok": True, "checkin_id": checkin_id, "recommendations": recs, "meta": meta})
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
        tz_name = await db.get_user_tz(conn, tg_user_id)
        local_date = db.get_user_local_date(tz_name or None)
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
        selected_items = [
            {
                "action": str(x.get("action") or "add"),
                "slug": str(x.get("slug") or ""),
                "supplement_id": int(x.get("supplement_id") or 0),
                "reason": str(x.get("reason") or ""),
                "suggested_time": x.get("suggested_time"),
                "slot_id": x.get("suggested_slot_id"),
            }
            for x in picked
        ]
        await db.apply_ai_plan_items(conn, tg_user_id, local_date, selected_items)
    finally:
        await conn.close()
    return JSONResponse({"ok": True})


@app.get("/api/checkin/today")
async def api_checkin_today(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    if not await _premium_guard(tg_user_id):
        return JSONResponse({"ok": False, "code": "PREMIUM_REQUIRED"}, status_code=402)
    conn = await db.connect(s.DB_PATH)
    try:
        tz_name = await db.get_user_tz(conn, tg_user_id)
        local_date = db.get_user_local_date(tz_name or None)
        row = await db.get_today_checkin(conn, tg_user_id, local_date)
    finally:
        await conn.close()
    if not row:
        return JSONResponse({"ok": True, "checkin": None})
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
    return JSONResponse({"ok": True, "checkin": {"id": int(row.get("id") or 0), "answers": answers, "recommendations": recs}})
