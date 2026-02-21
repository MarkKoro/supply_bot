import os
import json
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv

from telegram_webapp_auth import verify_init_data
import db
from settings import get_settings

BASE_DIR = Path(__file__).resolve().parent
WEBAPP_DIR = BASE_DIR / "webapp"

load_dotenv(BASE_DIR / ".env")  # берём TG_TOKEN и прочее из /srv/supply_bot/.env
BOT_TOKEN = os.getenv("TG_TOKEN", "")

app = FastAPI(title="SupplyBot WebApp")
s = get_settings()

class VerifyBody(BaseModel):
    init_data: str


class TodayToggleBody(BaseModel):
    supplement_id: int
    status: str


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


async def _today_payload(tg_user_id: int) -> dict:
    conn = await db.connect(s.DB_PATH)
    try:
        user_id = await db.get_or_create_user(conn, tg_user_id, tz_name="")
        tz_name = await db.get_user_tz(conn, tg_user_id)
        day = db.get_user_local_date(tz_name or None)
        slots = await db.get_user_slots(conn, user_id)
        status_map = await db.get_day_status_map(conn, tg_user_id, day)
        stack = await db.get_user_base_stack(conn, user_id)
        enabled_slots = [slot for slot in slots if slot.get("is_enabled", True)]

        def _slot_id_for_item(item: dict) -> str:
            tm = str(item.get("time_local") or "").strip()
            if not tm:
                return str(enabled_slots[0].get("slot_id") or "morning") if enabled_slots else "morning"
            try:
                hour = int(tm[:2])
            except Exception:
                return str(enabled_slots[0].get("slot_id") or "morning") if enabled_slots else "morning"
            if hour < 11:
                pref = "morning"
            elif hour < 16:
                pref = "noon"
            elif hour < 21:
                pref = "evening"
            else:
                pref = "night"
            available = {str(slot.get("slot_id") or "") for slot in enabled_slots}
            if pref in available:
                return pref
            return str(enabled_slots[0].get("slot_id") or "morning") if enabled_slots else "morning"

        grouped: dict[str, list[dict]] = {}
        for item in stack:
            sid = int(item.get("supplement_id") or 0)
            slot_id = _slot_id_for_item(item)
            grouped.setdefault(slot_id, []).append(
                {
                    "supplement_id": sid,
                    "name": str(item.get("ru_name") or item.get("slug") or ""),
                    "slug": str(item.get("slug") or ""),
                    "status": status_map.get(sid, "none"),
                }
            )

        data_slots = []
        for slot in enabled_slots:
            slot_id = str(slot.get("slot_id") or "")
            items = grouped.get(slot_id, [])
            items.sort(key=lambda x: x["name"])
            data_slots.append(
                {
                    "slot_id": slot_id,
                    "title": str(slot.get("title_ru") or "Слот"),
                    "time_local": str(slot.get("time_local") or "--:--"),
                    "items": items,
                }
            )
        return {"day": day, "slots": data_slots}
    finally:
        await conn.close()

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
    # Можно дополнительно вытащить user из parsed["user"] (там JSON строка)
    return JSONResponse({"ok": ok, "parsed": parsed})


@app.get("/api/today")
async def api_today(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
    payload = await _today_payload(tg_user_id)
    return JSONResponse({"ok": True, **payload})


@app.post("/api/today/toggle")
async def api_today_toggle(
    body: TodayToggleBody,
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    tg_user_id = await _auth_user_id(x_telegram_init_data)
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
