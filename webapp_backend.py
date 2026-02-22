import hashlib
import json
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException
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
