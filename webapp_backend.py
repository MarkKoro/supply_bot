import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv

from telegram_webapp_auth import verify_init_data

BASE_DIR = Path(__file__).resolve().parent
WEBAPP_DIR = BASE_DIR / "webapp"

load_dotenv(BASE_DIR / ".env")  # берём TG_TOKEN и прочее из /srv/supply_bot/.env
BOT_TOKEN = os.getenv("TG_TOKEN", "")

app = FastAPI(title="SupplyBot WebApp")

class VerifyBody(BaseModel):
    init_data: str

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
