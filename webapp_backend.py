import json
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from telegram_webapp_auth import verify_init_data
import db
from settings import get_settings

BASE_DIR = Path(__file__).resolve().parent
WEBAPP_DIR = BASE_DIR / "webapp"

app = FastAPI(title="SupplyBot WebApp")
s = get_settings()
BOT_TOKEN = s.TG_TOKEN


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
    return tg_user_id


async def _premium_guard(tg_user_id: int) -> bool:
    conn = await db.connect(s.DB_PATH)
    try:
        return await db.is_premium_active(conn, tg_user_id)
    finally:
        await conn.close()


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
        premium = await db.is_premium_active(conn, tg_user_id)
        premium_until = await db.get_premium_until(conn, tg_user_id)
    finally:
        await conn.close()
    return JSONResponse({"ok": True, "premium": premium, "premium_until_utc": premium_until, "plans": _plans_payload()})


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
            data_slots.append({"slot_id": str(group.get("slot_id") or ""), "title": str(group.get("title") or "Слот"), "time_local": str(group.get("time") or "--:--"), "items": items})
        return {"day": day, "slots": data_slots}
    finally:
        await conn.close()


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
