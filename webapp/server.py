"""
Backend Mini App для CarWash-бота.
Переиспользует существующие sessions.py / calculator.py / config.py —
никакой отдельной базы данных, те же файлы, что использует сам бот.

Запуск (для теста локально):
    pip install fastapi uvicorn --break-system-packages
    uvicorn webapp.server:app --reload --port 8000

Для Telegram Mini App нужен публичный HTTPS-адрес (ngrok / Render / Railway),
см. README в этой папке.
"""
import sys, os, hashlib, hmac, json
from urllib.parse import parse_qsl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import (
    TOKEN, BRANCHES, BODY_TYPES, BODY_TYPE_ORDER, SERVICES, PRODUCTS,
    PAYMENT_TYPES, get_service_price,
)
from sessions import (
    get_session, save_sessions, get_branch_workers, get_branch_admin,
    is_branch_admin, load_archive,
)
from calculator import calculate_summary

app = FastAPI(title="CarWash Mini App API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── Проверка подлинности данных Telegram WebApp ────────────────────────────
def verify_init_data(init_data: str) -> dict:
    """Проверяет подпись initData, которую Telegram передаёт при открытии Mini App.
    См. https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app"""
    if not init_data:
        raise HTTPException(401, "Нет данных авторизации")
    parsed = dict(parse_qsl(init_data))
    recv_hash = parsed.pop("hash", "")
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if calc_hash != recv_hash:
        raise HTTPException(401, "Неверная подпись initData")
    return json.loads(parsed.get("user", "{}"))


def auth(x_init_data: str = Header(default="")) -> dict:
    # В деве можно временно закомментировать verify и просто распарсить user.
    return verify_init_data(x_init_data)


# ── Модели запросов ─────────────────────────────────────────────────────────
class CarIn(BaseModel):
    branch: str
    employee: str
    body_type: str
    service_keys: list[str] = []
    custom_services: list[dict] = []       # [{"name","price","percent"}]
    car: str = ""
    payment: str


class LoyaltyIn(BaseModel):
    branch: str
    car_num: int
    discount: int


# ── Справочники (без авторизации — статичные данные) ───────────────────────
@app.get("/api/config")
def api_config():
    return {
        "branches": BRANCHES,
        "body_types": [{"key": k, "name": BODY_TYPES[k]} for k in BODY_TYPE_ORDER],
        "services": [
            {"key": k, "name": v["name"], "percent": v["percent"],
             "prices": v["prices"] if isinstance(v["prices"], dict) else
                       {bt: v["prices"] for bt in BODY_TYPE_ORDER}}
            for k, v in SERVICES.items()
        ],
        "products": [{"key": k, "name": v["name"], "price": v["price"]} for k, v in PRODUCTS.items()],
        "payment_types": PAYMENT_TYPES,
    }


@app.get("/api/workers")
def api_workers(branch: str):
    return {"workers": get_branch_workers(branch), "admin_id": get_branch_admin(branch)}


# ── Смена ────────────────────────────────────────────────────────────────
@app.get("/api/session")
def api_session(branch: str):
    session = get_session(branch)
    summary = calculate_summary(session)
    return {"session": session, "summary": summary}


@app.post("/api/car")
def api_add_car(body: CarIn):
    session = get_session(body.branch)
    body_type = body.body_type

    breakdown = {}
    for k in body.service_keys:
        if k not in SERVICES:
            continue
        breakdown[k] = {
            "name": SERVICES[k]["name"],
            "price": get_service_price(k, body_type),
            "percent": SERVICES[k]["percent"],
        }
    for i, c in enumerate(body.custom_services):
        breakdown[f"custom_{i}"] = {
            "name": c["name"], "price": int(c["price"]), "percent": float(c["percent"]) / 100,
        }

    if not breakdown:
        raise HTTPException(400, "Нужна хотя бы одна услуга")

    num = len(session["cars"]) + 1
    car = {
        "num": num,
        "employee": body.employee,
        "body_type": body_type,
        "service_keys": body.service_keys,
        "custom_services": body.custom_services,
        "price_breakdown": breakdown,
        "service": " + ".join(v["name"] for v in breakdown.values()),
        "price": sum(v["price"] for v in breakdown.values()),
        "car": body.car,
        "payment": body.payment,
    }
    session["cars"].append(car)
    save_sessions()
    return {"ok": True, "car": car, "summary": calculate_summary(session)}


@app.delete("/api/car/{branch}/{num}")
def api_delete_car(branch: str, num: int):
    session = get_session(branch)
    session["cars"] = [c for c in session["cars"] if c["num"] != num]
    save_sessions()
    return {"ok": True, "summary": calculate_summary(session)}


@app.post("/api/loyalty")
def api_add_loyalty(body: LoyaltyIn):
    session = get_session(body.branch)
    session.setdefault("loyalty", []).append({"car_num": body.car_num, "discount": body.discount})
    save_sessions()
    return {"ok": True, "summary": calculate_summary(session)}


# ── Отчёты ───────────────────────────────────────────────────────────────
@app.get("/api/archive")
def api_archive(branch: str, limit: int = 14):
    archive = load_archive()
    days = archive.get(branch, [])
    return {"days": days[-limit:][::-1]}


# ── Статика (сама Mini App) ─────────────────────────────────────────────
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
