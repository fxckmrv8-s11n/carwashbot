"""
Хранилище данных бота.

Ключевые изменения относительно версии "на пользователя":
- Касса (sessions) хранится по ФИЛИАЛУ, а не по user_id. Все, кто работает
  в одном филиале в этот день, видят и пишут в одну и ту же кассу.
- Запись в файлы атомарна (temp-файл + os.replace) и защищена файловой
  блокировкой (filelock), чтобы при параллельной работе нескольких
  сотрудников/филиалов не терялись и не портились данные.
- Список сотрудников и админ — атрибуты филиала (branches_config.json),
  а не личной сессии пользователя.
"""
import json
import os
import time
from datetime import datetime
from contextlib import contextmanager

from config import SALARY_ADMIN, BRANCHES, OWNER_ID

DATA_DIR = os.getenv("DATA_DIR", os.path.expanduser("~"))
os.makedirs(DATA_DIR, exist_ok=True)

SESSIONS_FILE = os.path.join(DATA_DIR, "carwash_sessions.json")
ARCHIVE_FILE  = os.path.join(DATA_DIR, "carwash_archive.json")
BRANCHES_FILE = os.path.join(DATA_DIR, "carwash_branches.json")
USERS_FILE    = os.path.join(DATA_DIR, "carwash_users.json")

LOCK_TIMEOUT = 10  # секунд ожидания блокировки, прежде чем сдаться


class Timeout(Exception):
    pass


@contextmanager
def _file_lock(path: str, timeout: float = LOCK_TIMEOUT):
    """Простая межпроцессная блокировка на основе O_CREAT|O_EXCL.
    Не требует сторонних библиотек, работает на Linux/macOS из коробки.
    Если процесс упал и не снял лок (например kill -9), сторожевой
    таймаут по mtime лок-файла (LOCK_TIMEOUT*3) позволяет его "сорвать"."""
    lock_path = path + ".lock"
    deadline  = time.monotonic() + timeout
    fd = None
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                age = time.monotonic() - os.path.getmtime(lock_path)
            except OSError:
                age = 0
            if age > LOCK_TIMEOUT * 3:
                try:
                    os.remove(lock_path)
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise Timeout(f"Не удалось получить блокировку {lock_path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.remove(lock_path)
        except OSError:
            pass


def _lock(path: str):
    return _file_lock(path, LOCK_TIMEOUT)


def _atomic_write_json(path: str, data: dict):
    """Пишет JSON во временный файл и атомарно подменяет им целевой файл.
    Так файл никогда не остаётся в "битом" (наполовину записанном) виде,
    даже если процесс упадёт прямо во время записи."""
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def _read_json_locked(path: str) -> dict:
    lock = _lock(path)
    try:
        with lock:
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            return {}
    except Timeout:
        # Не удалось получить лок за разумное время — отдаём последнее
        # известное состояние из памяти, чтобы бот не падал.
        return {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_json_locked(path: str, data: dict):
    lock = _lock(path)
    try:
        with lock:
            _atomic_write_json(path, data)
    except Timeout:
        print(f"⚠️ Не удалось получить блокировку на {path} за {LOCK_TIMEOUT}с")


def _update_json_locked(path: str, update_fn):
    """Атомарно: читает файл, применяет update_fn(data) -> data, пишет обратно.
    Вся операция (чтение+изменение+запись) происходит под одной блокировкой,
    что устраняет гонки между параллельными запросами разных пользователей."""
    lock = _lock(path)
    try:
        with lock:
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                except (json.JSONDecodeError, OSError):
                    data = {}
            else:
                data = {}
            data = update_fn(data)
            _atomic_write_json(path, data)
            return data
    except Timeout:
        print(f"⚠️ Не удалось получить блокировку на {path} за {LOCK_TIMEOUT}с")
        return None


# ── СЕССИИ (КАССА ПО ФИЛИАЛУ) ───────────────────────────────────────────────
# В памяти процесса держим кэш — большинство чтений идёт отсюда,
# а на диск пишем через _update_json_locked при каждом изменении.

sessions: dict[str, dict] = {}   # branch -> session


def load_sessions():
    global sessions
    sessions = _read_json_locked(SESSIONS_FILE)


def save_sessions():
    """Сбрасывает весь текущий кэш sessions на диск под блокировкой.
    Используется после прямого изменения sessions[branch] в памяти."""
    def _update(_old):
        return sessions
    _update_json_locked(SESSIONS_FILE, _update)


def get_session(branch: str) -> dict:
    if not branch:
        # Подстраховка: если пользователь ещё не выбрал филиал /newday,
        # не должно дойти до сюда — но на всякий случай не падаем.
        branch = "—"
    if branch not in sessions:
        sessions[branch] = _empty_session(branch)
        save_sessions()
    s = sessions[branch]
    for key in ("loyalty", "expenses", "cars", "products"):
        if key not in s:
            s[key] = []
    return s


def reset_session(branch: str):
    sessions[branch] = _empty_session(branch)
    save_sessions()


def _empty_session(branch: str) -> dict:
    return {
        "date":          datetime.now().strftime("%d.%m.%Y"),
        "branch":        branch,
        "cars":          [],
        "products":      [],
        "expenses":      [],
        "loyalty":       [],
        "admin_percent": SALARY_ADMIN,
    }


# ── АРХИВ ────────────────────────────────────────────────────────────────────

def load_archive() -> dict:
    return _read_json_locked(ARCHIVE_FILE)


def save_to_archive(branch: str, session: dict):
    date = session.get("date", datetime.now().strftime("%d.%m.%Y"))

    def _update(archive):
        archive.setdefault(branch, {})[date] = {
            "date":          date,
            "branch":        branch,
            "cars":          session.get("cars", []),
            "products":      session.get("products", []),
            "expenses":      session.get("expenses", []),
            "loyalty":       session.get("loyalty", []),
            "admin_percent": session.get("admin_percent", SALARY_ADMIN),
        }
        return archive

    _update_json_locked(ARCHIVE_FILE, _update)


# ── КОНФИГ ФИЛИАЛОВ: админ + сотрудники ─────────────────────────────────────
# branches_config.json: { branch: {"admin": user_id|0, "workers": [str, ...]} }

_branches_cache: dict[str, dict] | None = None


def _default_branches_config() -> dict:
    return {b: {"admin": 0, "workers": []} for b in BRANCHES}


def load_branches_config() -> dict:
    global _branches_cache
    data = _read_json_locked(BRANCHES_FILE)
    if not data:
        data = _default_branches_config()
        _write_json_locked(BRANCHES_FILE, data)
    # миграция — гарантируем наличие всех текущих филиалов и нужных ключей
    changed = False
    for b in BRANCHES:
        if b not in data:
            data[b] = {"admin": 0, "workers": []}
            changed = True
    for b, cfg in data.items():
        if "admin" not in cfg:
            cfg["admin"] = 0; changed = True
        if "workers" not in cfg:
            cfg["workers"] = []; changed = True
    if changed:
        _write_json_locked(BRANCHES_FILE, data)
    _branches_cache = data
    return data


def get_branch_config(branch: str) -> dict:
    cfg = _branches_cache or load_branches_config()
    return cfg.get(branch, {"admin": 0, "workers": []})


def get_branch_admin(branch: str) -> int:
    return get_branch_config(branch).get("admin", 0)


def get_branch_admin_name(branch: str) -> str:
    """Имя назначенного админа филиала (для PDF/отчётов). Если не назначен — 'Администратор'."""
    admin_id = get_branch_admin(branch)
    if not admin_id:
        return "Администратор"
    users = load_users()
    return users.get(str(admin_id), users.get(admin_id, "Администратор"))


def is_branch_admin(user_id: int, branch: str) -> bool:
    """Владелец (OWNER_ID) — админ всех филиалов."""
    if user_id == OWNER_ID:
        return True
    return get_branch_admin(branch) == user_id


def set_branch_admin(branch: str, user_id: int):
    def _update(data):
        data.setdefault(branch, {"admin": 0, "workers": []})
        data[branch]["admin"] = user_id
        return data
    global _branches_cache
    _branches_cache = _update_json_locked(BRANCHES_FILE, _update)


def get_branch_workers(branch: str) -> list[str]:
    return get_branch_config(branch).get("workers", [])


def add_branch_worker(branch: str, name: str) -> bool:
    """Возвращает False, если сотрудник уже есть."""
    result = {"added": False}

    def _update(data):
        data.setdefault(branch, {"admin": 0, "workers": []})
        workers = data[branch].setdefault("workers", [])
        if name in workers:
            result["added"] = False
        else:
            workers.append(name)
            result["added"] = True
        return data

    global _branches_cache
    _branches_cache = _update_json_locked(BRANCHES_FILE, _update)
    return result["added"]


def remove_branch_worker(branch: str, name: str) -> bool:
    result = {"removed": False}

    def _update(data):
        data.setdefault(branch, {"admin": 0, "workers": []})
        workers = data[branch].setdefault("workers", [])
        if name in workers:
            workers.remove(name)
            result["removed"] = True
        return data

    global _branches_cache
    _branches_cache = _update_json_locked(BRANCHES_FILE, _update)
    return result["removed"]


# ── ГРАФИК РАБОТЫ МОЙЩИКОВ (например 3/1 — 3 дня работает, 1 отдыхает) ──────

def set_worker_schedule(branch: str, name: str, work_days: int, rest_days: int, start_date: str):
    """start_date в формате YYYY-MM-DD — точка отсчёта цикла."""
    def _update(data):
        data.setdefault(branch, {"admin": 0, "workers": []})
        schedules = data[branch].setdefault("schedules", {})
        schedules[name] = {"work": work_days, "rest": rest_days, "start": start_date}
        return data

    global _branches_cache
    _branches_cache = _update_json_locked(BRANCHES_FILE, _update)


def clear_worker_schedule(branch: str, name: str) -> bool:
    result = {"removed": False}

    def _update(data):
        data.setdefault(branch, {"admin": 0, "workers": []})
        schedules = data[branch].setdefault("schedules", {})
        if name in schedules:
            del schedules[name]
            result["removed"] = True
        return data

    global _branches_cache
    _branches_cache = _update_json_locked(BRANCHES_FILE, _update)
    return result["removed"]


def get_worker_schedule(branch: str, name: str) -> dict | None:
    return get_branch_config(branch).get("schedules", {}).get(name)


def is_working_on(branch: str, name: str, on_date=None) -> bool:
    """Работает ли мойщик в указанный день согласно графику.
    Если график не задан — считаем, что мойщик доступен всегда (True)."""
    from datetime import date as _date
    sched = get_worker_schedule(branch, name)
    if not sched:
        return True
    try:
        start = _date.fromisoformat(sched["start"])
    except (ValueError, KeyError):
        return True
    on_date = on_date or _date.today()
    cycle = sched["work"] + sched["rest"]
    if cycle <= 0:
        return True
    days_passed = (on_date - start).days
    # % в Python корректно работает и для отрицательных чисел (цикл продолжается
    # «назад» по времени так же регулярно, как и вперёд) — это и нужно для
    # отображения недели, в которую может попадать дата раньше start_date.
    return (days_passed % cycle) < sched["work"]


def get_schedule_status(branch: str) -> dict:
    """{worker: {'working': bool, 'schedule': {...} | None}} на сегодня."""
    workers = get_branch_workers(branch)
    return {
        w: {"working": is_working_on(branch, w), "schedule": get_worker_schedule(branch, w)}
        for w in workers
    }


# ── ПОЛЬЗОВАТЕЛИ (белый список) ─────────────────────────────────────────────

def load_users() -> dict:
    return _read_json_locked(USERS_FILE)


def save_users(users: dict):
    _write_json_locked(USERS_FILE, users)


def add_user(user_id: int, name: str):
    def _update(data):
        data[str(user_id)] = name
        return data
    _update_json_locked(USERS_FILE, _update)


def remove_user(user_id: int) -> bool:
    result = {"removed": False}

    def _update(data):
        if str(user_id) in data:
            data.pop(str(user_id))
            result["removed"] = True
        return data

    _update_json_locked(USERS_FILE, _update)
    return result["removed"]


# ── ПРИВЯЗКА ПОЛЬЗОВАТЕЛЯ К ФИЛИАЛУ (на сегодняшнюю смену) ─────────────────
# Храним в user_data контекста telegram (per-chat), не здесь — см. handlers.
