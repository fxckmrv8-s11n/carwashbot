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
import sys, os, hashlib, hmac, json, tempfile, asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from urllib.parse import parse_qsl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import (
    TOKEN, OWNER_ID, BRANCHES, BODY_TYPES, BODY_TYPE_ORDER, SERVICES, PRODUCTS,
    PAYMENT_TYPES, get_service_price,
)
from sessions import (
    get_session, save_sessions, save_to_archive, reset_session,
    get_branch_workers, get_branch_admin, is_branch_admin, set_branch_admin,
    add_branch_worker, remove_branch_worker,
    load_archive, load_users, save_users, add_user, remove_user,
    set_worker_schedule, clear_worker_schedule, get_worker_schedule,
    get_schedule_status, is_working_on,
)
from calculator import calculate_summary
from pdf_generator import generate_pdf
from xlsx_generator import generate_xlsx
from history_log import log_action, get_history
from presets import list_presets, add_preset, delete_preset
from notify import notify_user

MONTHS_RU = {
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4,
    "май": 5, "июнь": 6, "июль": 7, "август": 8,
    "сентябрь": 9, "октябрь": 10, "ноябрь": 11, "декабрь": 12,
}

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


def auth_optional(x_init_data: str = Header(default="")) -> dict:
    try:
        return verify_init_data(x_init_data)
    except HTTPException:
        return {}


def current_user_id(x_init_data: str = Header(default="")) -> int:
    user = auth_optional(x_init_data)
    return int(user.get("id", 0))


def current_user_name(x_init_data: str = Header(default="")) -> str:
    user = auth_optional(x_init_data)
    return user.get("first_name", "") or user.get("username", "") or "—"


def find_user_id_by_name(name: str) -> int:
    """Ищем telegram id пользователя по имени среди тех, кому уже выдан доступ
    (используется, чтобы уведомить сотрудника при добавлении, если он уже
    есть в списке пользователей)."""
    for uid, uname in load_users().items():
        if uname.strip().lower() == name.strip().lower():
            return int(uid)
    return 0


def require_branch_admin(branch: str, x_init_data: str = Header(default="")):
    uid = current_user_id(x_init_data)
    if not is_branch_admin(uid, branch):
        raise HTTPException(403, "Нет прав администратора филиала")
    return uid


def require_owner(x_init_data: str = Header(default="")):
    uid = current_user_id(x_init_data)
    if uid != OWNER_ID:
        raise HTTPException(403, "Только для владельца")
    return uid


# ── Модели запросов ─────────────────────────────────────────────────────────
class CarIn(BaseModel):
    branch: str
    employee: str
    body_type: str
    service_keys: list[str] = []
    custom_services: list[dict] = []       # [{"name","price","percent"}]
    car: str = ""
    payment: str
    payment_split: Optional[Dict[str, int]] = None   # {"нал": 800, "безнал": 1200}
    comment: str = ""


class LoyaltyIn(BaseModel):
    branch: str
    car_num: int
    discount: int


class ExpenseIn(BaseModel):
    branch: str
    name: str
    amount: int


class IncomeIn(BaseModel):
    branch: str
    name: str
    amount: int
    payment: str = "нал"
    payment_split: Optional[Dict[str, int]] = None


class WorkerIn(BaseModel):
    branch: str
    name: str
    x_init_data: str = ""


class ScheduleIn(BaseModel):
    branch: str
    name: str
    work_days: int
    rest_days: int
    start_date: str  # YYYY-MM-DD


class BranchAdminIn(BaseModel):
    branch: str
    user_id: int


class CarEditIn(BaseModel):
    employee: Optional[str] = None
    body_type: Optional[str] = None
    service_keys: Optional[list[str]] = None
    custom_services: Optional[list[dict]] = None
    car: Optional[str] = None
    payment: Optional[str] = None
    payment_split: Optional[Dict[str, int]] = None
    comment: Optional[str] = None
    status: Optional[str] = None


class CarStatusIn(BaseModel):
    status: str  # "in_progress" | "done"


class PresetIn(BaseModel):
    branch: str
    name: str
    service_keys: list[str] = []
    custom_services: list[dict] = []


class UserIn(BaseModel):
    user_id: int
    name: str = "Без имени"


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
    return {
        "workers": get_branch_workers(branch),
        "admin_id": get_branch_admin(branch),
        "schedule": get_schedule_status(branch),
    }


@app.post("/api/schedule")
def api_set_schedule(body: ScheduleIn, x_init_data: str = Header(default="")):
    require_branch_admin(body.branch, x_init_data)
    if body.name not in get_branch_workers(body.branch):
        raise HTTPException(404, "Сотрудник не найден")
    if body.work_days <= 0 or body.rest_days < 0:
        raise HTTPException(400, "Некорректный график")
    set_worker_schedule(body.branch, body.name, body.work_days, body.rest_days, body.start_date)
    return {"ok": True, "schedule": get_schedule_status(body.branch)}


@app.delete("/api/schedule/{branch}/{name}")
def api_clear_schedule(branch: str, name: str, x_init_data: str = Header(default="")):
    require_branch_admin(branch, x_init_data)
    clear_worker_schedule(branch, name)
    return {"ok": True, "schedule": get_schedule_status(branch)}


@app.get("/api/schedule/week")
def api_schedule_week(branch: str, monday: str = ""):
    """График Пн–Пт для всех мойщиков филиала.
    monday — дата понедельника (YYYY-MM-DD); по умолчанию — понедельник текущей недели."""
    from datetime import date as _date, timedelta as _timedelta
    if monday:
        try:
            start = _date.fromisoformat(monday)
        except ValueError:
            raise HTTPException(400, "Некорректная дата")
    else:
        today = _date.today()
        start = today - _timedelta(days=today.weekday())

    days = [start + _timedelta(days=i) for i in range(7)]  # Пн..Вс
    day_labels = [d.strftime("%d.%m") for d in days]
    weekdays_ru = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

    workers = get_branch_workers(branch)
    rows = {}
    for w in workers:
        rows[w] = [is_working_on(branch, w, d) for d in days]

    return {
        "monday": start.isoformat(),
        "day_labels": day_labels,
        "weekday_labels": weekdays_ru,
        "workers": rows,
    }


@app.get("/api/me")
def api_me(branch: str = "", x_init_data: str = Header(default="")):
    user = auth_optional(x_init_data)
    uid = int(user.get("id", 0))
    users = load_users()
    employee_name = users.get(str(uid), "")
    is_worker = bool(employee_name) and branch and employee_name in get_branch_workers(branch)
    return {
        "user_id": uid,
        "name": user.get("first_name", ""),
        "is_owner": uid == OWNER_ID,
        "is_branch_admin": is_branch_admin(uid, branch) if branch else False,
        "employee_name": employee_name,
        "is_worker": is_worker,
    }


@app.post("/api/workers")
def api_add_worker(body: WorkerIn, x_init_data: str = Header(default="")):
    require_branch_admin(body.branch, x_init_data)
    added = add_branch_worker(body.branch, body.name.strip())
    if not added:
        raise HTTPException(400, "Такой сотрудник уже есть")
    uid = find_user_id_by_name(body.name.strip())
    if uid:
        notify_user(uid, f"Вас добавили сотрудником в филиал «{body.branch}» ✅")
    return {"ok": True, "workers": get_branch_workers(body.branch)}


@app.delete("/api/workers/{branch}/{name}")
def api_remove_worker(branch: str, name: str, x_init_data: str = Header(default="")):
    require_branch_admin(branch, x_init_data)
    remove_branch_worker(branch, name)
    return {"ok": True, "workers": get_branch_workers(branch)}


@app.post("/api/branch-admin")
def api_set_branch_admin(body: BranchAdminIn, x_init_data: str = Header(default="")):
    uid = current_user_id(x_init_data)
    if uid != OWNER_ID and not is_branch_admin(uid, body.branch):
        raise HTTPException(403, "Нет прав")
    set_branch_admin(body.branch, body.user_id)
    notify_user(body.user_id, f"Вас назначили администратором филиала «{body.branch}» 🛡️")
    return {"ok": True, "admin_id": get_branch_admin(body.branch)}


@app.get("/api/users")
def api_list_users(x_init_data: str = Header(default="")):
    require_owner(x_init_data)
    users = load_users()
    return {"users": [{"user_id": int(uid), "name": name} for uid, name in users.items()]}


@app.post("/api/users")
def api_add_user(body: UserIn, x_init_data: str = Header(default="")):
    require_owner(x_init_data)
    add_user(body.user_id, body.name)
    return {"ok": True}


@app.delete("/api/users/{user_id}")
def api_remove_user(user_id: int, x_init_data: str = Header(default="")):
    require_owner(x_init_data)
    remove_user(user_id)
    return {"ok": True}


# ── Смена ────────────────────────────────────────────────────────────────
@app.get("/api/session")
def api_session(branch: str):
    session = get_session(branch)
    summary = calculate_summary(session)
    return {"session": session, "summary": summary}


@app.post("/api/car")
def api_add_car(body: CarIn, x_init_data: str = Header(default="")):
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

    total_price = sum(v["price"] for v in breakdown.values())
    if body.payment_split:
        split_sum = sum(body.payment_split.values())
        if split_sum != total_price:
            raise HTTPException(400, f"Сумма раздельной оплаты ({split_sum}₽) не совпадает со стоимостью ({total_price}₽)")

    num = len(session["cars"]) + 1
    car = {
        "num": num,
        "employee": body.employee,
        "body_type": body_type,
        "service_keys": body.service_keys,
        "custom_services": body.custom_services,
        "price_breakdown": breakdown,
        "service": " + ".join(v["name"] for v in breakdown.values()),
        "price": total_price,
        "car": body.car,
        "payment": body.payment,
        "payment_split": body.payment_split,
        "comment": body.comment,
        "status": "in_progress",
        "time": datetime.now().strftime("%H:%M"),
    }
    session["cars"].append(car)
    save_sessions()
    log_action(body.branch, "add", current_user_id(x_init_data), current_user_name(x_init_data),
               f"{car['car'] or 'машина'} · {car['service']} · {total_price}₽")
    return {"ok": True, "car": car, "summary": calculate_summary(session)}


def _rebuild_car_breakdown(body_type: str, service_keys: list, custom_services: list) -> dict:
    breakdown = {}
    for k in service_keys:
        if k not in SERVICES:
            continue
        breakdown[k] = {
            "name": SERVICES[k]["name"],
            "price": get_service_price(k, body_type),
            "percent": SERVICES[k]["percent"],
        }
    for i, c in enumerate(custom_services):
        breakdown[f"custom_{i}"] = {
            "name": c["name"], "price": int(c["price"]), "percent": float(c["percent"]) / 100,
        }
    return breakdown


@app.put("/api/car/{branch}/{num}")
def api_edit_car(branch: str, num: int, body: CarEditIn, x_init_data: str = Header(default="")):
    """Редактирование существующей машины (услуги/оплата/мойщик и т.д.),
    вместо удаления и создания заново."""
    session = get_session(branch)
    car = next((c for c in session["cars"] if c["num"] == num), None)
    if not car:
        raise HTTPException(404, "Машина не найдена")

    if body.employee is not None:
        car["employee"] = body.employee
    if body.car is not None:
        car["car"] = body.car
    if body.comment is not None:
        car["comment"] = body.comment
    if body.payment is not None:
        car["payment"] = body.payment
    if body.payment_split is not None:
        car["payment_split"] = body.payment_split or None

    if body.body_type is not None or body.service_keys is not None or body.custom_services is not None:
        body_type = body.body_type or car["body_type"]
        service_keys = body.service_keys if body.service_keys is not None else car["service_keys"]
        custom_services = body.custom_services if body.custom_services is not None else car["custom_services"]
        breakdown = _rebuild_car_breakdown(body_type, service_keys, custom_services)
        if not breakdown:
            raise HTTPException(400, "Нужна хотя бы одна услуга")
        total_price = sum(v["price"] for v in breakdown.values())
        if car.get("payment_split"):
            split_sum = sum(car["payment_split"].values())
            if split_sum != total_price:
                raise HTTPException(400, f"Сумма раздельной оплаты ({split_sum}₽) не совпадает со стоимостью ({total_price}₽)")
        car["body_type"] = body_type
        car["service_keys"] = service_keys
        car["custom_services"] = custom_services
        car["price_breakdown"] = breakdown
        car["service"] = " + ".join(v["name"] for v in breakdown.values())
        car["price"] = total_price

    save_sessions()
    log_action(branch, "edit", current_user_id(x_init_data), current_user_name(x_init_data),
               f"{car['car'] or 'машина'} · {car['service']} · {car['price']}₽")
    return {"ok": True, "car": car, "summary": calculate_summary(session)}


@app.patch("/api/car/{branch}/{num}/status")
def api_set_car_status(branch: str, num: int, body: CarStatusIn, x_init_data: str = Header(default="")):
    """Переключение статуса 'в работе' / 'оплачено'. Это отметка для персонала —
    на кассу и расчёты никак не влияет (машина учитывается в кассе сразу при добавлении)."""
    if body.status not in ("in_progress", "done"):
        raise HTTPException(400, "Статус может быть 'in_progress' или 'done'")
    session = get_session(branch)
    car = next((c for c in session["cars"] if c["num"] == num), None)
    if not car:
        raise HTTPException(404, "Машина не найдена")
    car["status"] = body.status
    save_sessions()
    log_action(branch, "status", current_user_id(x_init_data), current_user_name(x_init_data),
               f"{car.get('car') or 'машина'} · статус → {'оплачено' if body.status=='done' else 'в работе'}")
    return {"ok": True, "car": car}


@app.delete("/api/car/{branch}/{num}")
def api_delete_car(branch: str, num: int, x_init_data: str = Header(default="")):
    session = get_session(branch)
    car = next((c for c in session["cars"] if c["num"] == num), None)
    session["cars"] = [c for c in session["cars"] if c["num"] != num]
    save_sessions()
    if car:
        log_action(branch, "delete", current_user_id(x_init_data), current_user_name(x_init_data),
                   f"{car.get('car') or 'машина'} · {car.get('service','')} · {car.get('price',0)}₽")
    return {"ok": True, "summary": calculate_summary(session)}


@app.post("/api/loyalty")
def api_add_loyalty(body: LoyaltyIn):
    session = get_session(body.branch)
    session.setdefault("loyalty", []).append({"car_num": body.car_num, "discount": body.discount})
    save_sessions()
    return {"ok": True, "summary": calculate_summary(session)}


@app.post("/api/expense")
def api_add_expense(body: ExpenseIn, x_init_data: str = Header(default="")):
    session = get_session(body.branch)
    session.setdefault("expenses", []).append({"name": body.name, "amount": body.amount})
    save_sessions()
    log_action(body.branch, "expense_add", current_user_id(x_init_data), current_user_name(x_init_data),
               f"{body.name} · -{body.amount}₽")
    return {"ok": True, "summary": calculate_summary(session)}


@app.delete("/api/expense/{branch}/{idx}")
def api_delete_expense(branch: str, idx: int, x_init_data: str = Header(default="")):
    session = get_session(branch)
    expenses = session.get("expenses", [])
    if not (0 <= idx < len(expenses)):
        raise HTTPException(404, "Расход не найден")
    removed = expenses.pop(idx)
    save_sessions()
    log_action(branch, "expense_delete", current_user_id(x_init_data), current_user_name(x_init_data),
               f"{removed['name']} · -{removed['amount']}₽")
    return {"ok": True, "summary": calculate_summary(session)}


@app.post("/api/income")
def api_add_income(body: IncomeIn, x_init_data: str = Header(default="")):
    session = get_session(body.branch)
    entry = {"name": body.name, "amount": body.amount}
    if body.payment_split:
        entry["payment_split"] = body.payment_split
    else:
        entry["payment"] = body.payment
    session.setdefault("incomes", []).append(entry)
    save_sessions()
    log_action(body.branch, "income_add", current_user_id(x_init_data), current_user_name(x_init_data),
               f"{body.name} · +{body.amount}₽")
    return {"ok": True, "summary": calculate_summary(session)}


@app.delete("/api/income/{branch}/{idx}")
def api_delete_income(branch: str, idx: int, x_init_data: str = Header(default="")):
    session = get_session(branch)
    incomes = session.get("incomes", [])
    if not (0 <= idx < len(incomes)):
        raise HTTPException(404, "Доход не найден")
    removed = incomes.pop(idx)
    save_sessions()
    log_action(branch, "income_delete", current_user_id(x_init_data), current_user_name(x_init_data),
               f"{removed['name']} · +{removed['amount']}₽")
    return {"ok": True, "summary": calculate_summary(session)}


# ── Отчёты ───────────────────────────────────────────────────────────────
@app.get("/api/archive")
def api_archive(branch: str, limit: int = 14):
    archive = load_archive()
    days = archive.get(branch, [])
    return {"days": days[-limit:][::-1]}


@app.post("/api/newday")
def api_newday(branch: str, x_init_data: str = Header(default="")):
    require_branch_admin(branch, x_init_data)
    session = get_session(branch)
    if session.get("cars") or session.get("products"):
        save_to_archive(branch, session)
    reset_session(branch)
    return {"ok": True}


@app.get("/api/reports/today")
def api_report_today(branch: str):
    session = get_session(branch)
    summary = calculate_summary(session)
    svc_count = {}
    for c in session["cars"]:
        svc_count[c.get("service", "—")] = svc_count.get(c.get("service", "—"), 0) + 1
    top = sorted(svc_count.items(), key=lambda x: x[1], reverse=True)[:5]
    return {"session": session, "summary": summary, "top_services": top}


@app.get("/api/reports/week")
def api_report_week(branch: str):
    archive = load_archive()
    branch_archive = archive.get(branch, {})
    today = datetime.now()
    week_start = (today - timedelta(days=today.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)

    days = []
    grand = 0
    for date_str, day in branch_archive.items():
        try:
            day_dt = datetime.strptime(date_str, "%d.%m.%Y")
        except ValueError:
            continue
        if not (week_start <= day_dt <= today):
            continue
        s = calculate_summary(day)
        grand += s["grand_total"]
        days.append({"date": date_str, "cars": len(day.get("cars", [])), "total": s["grand_total"],
                     "washer_salaries": s["washer_salaries"]})

    session = get_session(branch)
    if session.get("cars"):
        s = calculate_summary(session)
        grand += s["grand_total"]
        days.append({"date": session.get("date"), "cars": len(session["cars"]), "total": s["grand_total"],
                     "washer_salaries": s["washer_salaries"]})

    days.sort(key=lambda d: datetime.strptime(d["date"], "%d.%m.%Y"))
    return {"from": week_start.strftime("%d.%m.%Y"), "to": today.strftime("%d.%m.%Y"),
            "grand_total": grand, "days": days}


@app.get("/api/reports/month")
def api_report_month(branch: str, month: str, year: int = 0):
    month_num = MONTHS_RU.get(month.lower())
    if not month_num:
        raise HTTPException(400, f"Не понял месяц '{month}'")
    year = year or datetime.now().year
    archive = load_archive()
    branch_archive = archive.get(branch, {})
    week_sal: dict[str, dict[int, int]] = {}
    grand = 0
    for date_str, day in branch_archive.items():
        try:
            dt = datetime.strptime(date_str, "%d.%m.%Y")
        except ValueError:
            continue
        if dt.month != month_num or dt.year != year:
            continue
        wk = (dt.day - 1) // 7 + 1
        s = calculate_summary(day)
        grand += s["grand_total"]
        for emp, sal in s["washer_salaries"].items():
            week_sal.setdefault(emp, {})
            week_sal[emp][wk] = week_sal[emp].get(wk, 0) + sal
    return {"month": month, "year": year, "grand_total": grand, "by_worker": week_sal}


def _employee_name_from_init(x_init_data: str) -> str:
    uid = current_user_id(x_init_data)
    return load_users().get(str(uid), "")


def _my_day_stats(day_data: dict, name: str) -> dict:
    """Статистика одного мойщика за один день (сессия сегодня или день из архива)."""
    s = calculate_summary(day_data)
    my_cars = [c for c in day_data.get("cars", []) if c.get("employee") == name]
    return {
        "cars": len(my_cars),
        "salary": s["washer_salaries"].get(name, 0),
        "revenue": sum(c["price"] for c in my_cars),
        "car_list": [
            {
                "num": c.get("num"),
                "car": c.get("car") or "",
                "service": c.get("service") or "",
                "price": c.get("price", 0),
                "payment": c.get("payment", ""),
                "time": c.get("time", ""),
            }
            for c in my_cars
        ],
    }


@app.get("/api/my-stats")
def api_my_stats(branch: str, period: str = "today", x_init_data: str = Header(default="")):
    """Личная статистика мойщика: сегодня / неделя / месяц.
    Мойщик авторизуется своим Telegram-аккаунтом — привязка идёт через
    белый список пользователей (load_users: user_id → имя), сверенный со
    списком сотрудников филиала (get_branch_workers)."""
    name = _employee_name_from_init(x_init_data)
    if not name or name not in get_branch_workers(branch):
        raise HTTPException(403, "Вы не привязаны как сотрудник этого филиала")

    if period == "today":
        session = get_session(branch)
        stats = _my_day_stats(session, name)
        stats["date"] = session.get("date")
        return {"name": name, "period": "today", "stats": stats}

    if period not in ("week", "month"):
        raise HTTPException(400, "period должен быть today|week|month")

    today = datetime.now()
    if period == "week":
        start = (today - timedelta(days=today.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        start = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    archive = load_archive()
    branch_archive = archive.get(branch, {})

    days_out = []
    total_cars = total_salary = total_revenue = 0
    for date_str, day in branch_archive.items():
        try:
            dt = datetime.strptime(date_str, "%d.%m.%Y")
        except ValueError:
            continue
        if not (start <= dt <= today):
            continue
        st = _my_day_stats(day, name)
        if st["cars"] == 0:
            continue
        days_out.append({"date": date_str, **st})
        total_cars += st["cars"]; total_salary += st["salary"]; total_revenue += st["revenue"]

    session = get_session(branch)
    if session.get("cars"):
        st = _my_day_stats(session, name)
        if st["cars"] > 0:
            days_out.append({"date": session.get("date"), **st})
            total_cars += st["cars"]; total_salary += st["salary"]; total_revenue += st["revenue"]

    days_out.sort(key=lambda d: datetime.strptime(d["date"], "%d.%m.%Y"))
    return {
        "name": name, "period": period,
        "from": start.strftime("%d.%m.%Y"), "to": today.strftime("%d.%m.%Y"),
        "total_cars": total_cars, "total_salary": total_salary, "total_revenue": total_revenue,
        "days": days_out,
    }


@app.get("/api/branches/summary")
def api_branches_summary():
    """Публичная сводка по всем филиалам (сегодня + тренд за 5 дней) —
    используется на экране выбора филиала, доступна любому пользователю бота."""
    archive = load_archive()
    today = datetime.now()
    out = []
    for branch in BRANCHES:
        session = get_session(branch)
        s = calculate_summary(session)
        branch_archive = archive.get(branch, {})
        trend = []
        for i in range(4, -1, -1):
            dt = today - timedelta(days=i)
            date_str = dt.strftime("%d.%m.%Y")
            if i == 0:
                trend.append(s["grand_total"])
            else:
                day = branch_archive.get(date_str)
                trend.append(calculate_summary(day)["grand_total"] if day else 0)
        out.append({
            "branch": branch,
            "total": s["grand_total"],
            "cars": len(session.get("cars", [])),
            "trend": trend,
        })
    return {"branches": out}


@app.get("/api/reports/allreport")
def api_report_allreport(x_init_data: str = Header(default="")):
    require_owner(x_init_data)
    branches_out = []
    grand = 0
    for branch in BRANCHES:
        session = get_session(branch)
        if not session.get("cars") and not session.get("products"):
            continue
        s = calculate_summary(session)
        grand += s["grand_total"]
        branches_out.append({
            "branch": branch, "cars": len(session["cars"]), "total": s["grand_total"],
            "cash": s["cash"], "visa": s["visa"], "beznal": s["beznal"],
        })
    return {"branches": branches_out, "grand_total": grand}


@app.get("/api/reports/pdf")
def api_report_pdf(branch: str, date: str = ""):
    archive = load_archive()
    if date:
        day_data = archive.get(branch, {}).get(date)
        if not day_data:
            raise HTTPException(404, f"Нет данных за {date} в «{branch}»")
    else:
        day_data = get_session(branch)
        date = day_data.get("date", datetime.now().strftime("%d.%m.%Y"))
        if not day_data.get("cars"):
            raise HTTPException(404, "Нет данных за сегодня")

    summary = calculate_summary(day_data)
    safe_branch = branch.replace(" ", "_")
    pdf_path = os.path.join(tempfile.gettempdir(), f"report_{safe_branch}_{date.replace('.', '')}.pdf")
    generate_pdf(day_data, summary, pdf_path)
    return FileResponse(pdf_path, media_type="application/pdf",
                        filename=f"Касса_{branch}_{date}.pdf")


@app.get("/api/reports/xlsx")
def api_report_xlsx(branch: str, date: str = ""):
    archive = load_archive()
    if date:
        day_data = archive.get(branch, {}).get(date)
        if not day_data:
            raise HTTPException(404, f"Нет данных за {date} в «{branch}»")
    else:
        day_data = get_session(branch)
        date = day_data.get("date", datetime.now().strftime("%d.%m.%Y"))
        if not day_data.get("cars"):
            raise HTTPException(404, "Нет данных за сегодня")

    summary = calculate_summary(day_data)
    safe_branch = branch.replace(" ", "_")
    xlsx_path = os.path.join(tempfile.gettempdir(), f"report_{safe_branch}_{date.replace('.', '')}.xlsx")
    generate_xlsx(day_data, summary, xlsx_path)
    return FileResponse(
        xlsx_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"Касса_{branch}_{date}.xlsx",
    )


# ── История изменений кассы ──────────────────────────────────────────────
@app.get("/api/history")
def api_history(branch: str, limit: int = 100):
    return {"entries": get_history(branch, limit)}


# ── Пресеты услуг ────────────────────────────────────────────────────────
@app.get("/api/presets")
def api_get_presets(branch: str):
    return {"presets": list_presets(branch)}


@app.post("/api/presets")
def api_add_preset(body: PresetIn):
    if not body.service_keys and not body.custom_services:
        raise HTTPException(400, "Нужна хотя бы одна услуга в пресете")
    presets = add_preset(body.branch, body.name.strip(), body.service_keys, body.custom_services)
    return {"ok": True, "presets": presets}


@app.delete("/api/presets/{branch}/{name}")
def api_delete_preset(branch: str, name: str):
    presets = delete_preset(branch, name)
    return {"ok": True, "presets": presets}


# ── Статика (сама Mini App) ─────────────────────────────────────────────
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


@app.get("/")
def index():
    return FileResponse(
        os.path.join(STATIC_DIR, "index.html"),
        headers=NO_CACHE_HEADERS,
    )
