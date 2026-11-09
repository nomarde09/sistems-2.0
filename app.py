"""Система заявок: монтаж камер видеонаблюдения и автоматических ворот.

Вся программа в одном файле: входы, страницы, правила доступа, лог приложения.
Технический раздел — в tech.py, статистика — в stats.py.
"""
import hmac
import logging
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from logging.handlers import RotatingFileHandler
from urllib.parse import quote

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_from_directory, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix

import backup
import stats

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
LOG_DIR = os.environ.get("LOG_DIR") or os.path.join(BASE_DIR, "data", "logs")
LOG_PATH = os.path.join(LOG_DIR, "app.log")


# ---------------------------------------------------------------- настройки (.env)

def _computer_tz_offset():
    seconds = datetime.now().astimezone().utcoffset().total_seconds()
    return f"{seconds / 3600:g}"


def ensure_env_file():
    """Если файла .env нет — создать его. Пароли сотрудников здесь не хранятся (только хеши в базе)."""
    if os.path.exists(ENV_PATH):
        return
    text = (
        "# Настройки системы заявок. Этот файл НЕ попадает в Git.\n"
        "# Пароли сотрудников хранятся в базе только в виде хешей. Новые временные пароли:\n"
        "#   python app.py --novye-paroli\n"
        f"SECRET_KEY={secrets.token_hex(32)}\n"
        "# Часовой пояс: сколько часов прибавить к UTC (Москва = 3, Астана = 5).\n"
        f"TZ_OFFSET_HOURS={_computer_tz_offset()}\n"
        "# На хостинге с https: COOKIE_SECURE=1. За прокси хостинга: TRUST_PROXY=1.\n"
        "COOKIE_SECURE=0\n"
        "# Технический раздел /tech выключен. Включить: python app.py --tech-parol\n"
    )
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write(text)


def load_env():
    """Свой простой разбор .env. Переменные окружения важнее файла."""
    try:
        with open(ENV_PATH, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


ensure_env_file()
load_env()


def tz_offset_hours():
    try:
        return float(os.environ.get("TZ_OFFSET_HOURS", "3"))
    except ValueError:
        return 3.0


def now_local():
    """Местное время без микросекунд и без tzinfo."""
    tz = timezone(timedelta(hours=tz_offset_hours()))
    return datetime.now(tz).replace(microsecond=0, tzinfo=None)


def now_iso():
    return now_local().isoformat()


# ---------------------------------------------------------------- приложение

SECURE_COOKIES = os.environ.get("COOKIE_SECURE") == "1"
if SECURE_COOKIES and len(os.environ.get("SECRET_KEY", "")) < 32:
    raise RuntimeError("Для боевого сайта (COOKIE_SECURE=1) нужен SECRET_KEY не короче 32 символов в .env.")

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=SECURE_COOKIES,
    # префикс __Host- браузер принимает только по https и только для этого сайта
    SESSION_COOKIE_NAME="__Host-zayavki" if SECURE_COOKIES else "session",
    MAX_CONTENT_LENGTH=256 * 1024,
)
_allowed_hosts = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()]
if _allowed_hosts:
    app.config["TRUSTED_HOSTS"] = _allowed_hosts
if os.environ.get("TRUST_PROXY") == "1":
    # сайт за прокси хостинга: настоящий IP посетителя и https берём из заголовков прокси
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# ---------------------------------------------------------------- справочники

USERS = {
    "rukovoditel": {"name": "Руководитель", "role": "boss"},
    "manager1": {"name": "Менеджер 1", "role": "office"},
    "manager2": {"name": "Менеджер 2", "role": "office"},
    "brigada1": {"name": "Бригада 1", "role": "brigade"},
    "brigada2": {"name": "Бригада 2", "role": "brigade"},
}
BRIGADE_IDS = [u for u, v in USERS.items() if v["role"] == "brigade"]
OFFICE_IDS = [u for u, v in USERS.items() if v["role"] in ("boss", "office")]

STATUSES = {"new": "Новая", "in_work": "В работе", "done": "Выполнена", "cancelled": "Отменена"}
STATUS_TABS = [("new", "Новые"), ("in_work", "В работе"), ("done", "Выполнены"),
               ("cancelled", "Отменены"), ("all", "Все")]
PAYMENTS = {"kaspi_transfer": "Перевод на Kaspi", "kaspi_pay": "Kaspi Pay", "cash": "Наличными"}
REQUEST_TYPES = {
    "montazh": "Монтаж (новый объект)",
    "remont": "Ремонт / неисправность",
    "to": "Плановое ТО",
    "diagnostika": "Диагностика / выезд на осмотр",
    "doosnashenie": "Дооснащение / доработка",
    "demontazh": "Демонтаж",
    "drugoe": "Другое",
}
EQUIPMENT = {
    "vorota": "Автоматика ворот / шлагбаум",
    "domofon": "Домофон",
    "kamery": "Камеры / видеонаблюдение",
    "skud": "СКУД / замки",
    "drugoe": "Другое",
}
AGREED_TIMES = [f"{h:02d}:{m:02d}" for h in range(7, 23) for m in (0, 30) if not (h == 22 and m == 30)]
EVENT_LABELS = {
    "created": "Создал(а) заявку",
    "taken": "Взял(а) в работу",
    "returned": "Вернул(а) в свободные",
    "done": "Выполнил(а)",
    "edited": "Исправил(а) заявку",
    "cancelled": "Отменил(а) заявку",
    "warranty": "Изменил(а) гарантию",
}
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
              "сентября", "октября", "ноября", "декабря"]
MONTHS_NOM = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август",
              "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]

OLD_WORK_TYPES = {"cameras": "Камеры", "gates": "Ворота", "both": "Камеры и ворота"}


# ---------------------------------------------------------------- пользователи

def person_name(uid):
    return (os.environ.get("NAME_" + uid.upper(), "").strip()) or USERS[uid]["name"]


def login_of(uid):
    return (os.environ.get("LOGIN_" + uid.upper(), "").strip()) or uid


def find_user_by_login(text):
    wanted = (text or "").strip().casefold()
    if not wanted:
        return None
    for uid in USERS:
        if login_of(uid).casefold() == wanted:
            return uid
    return None


def secrets_equal(a, b):
    """Сравнение секретов по байтам: кириллица не роняет сайт."""
    return hmac.compare_digest(str(a).encode("utf-8"), str(b).encode("utf-8"))


# ---------------------------------------------------------------- лог приложения

def safe_for_log(value, limit=300):
    out = []
    for ch in str(value):
        if ch.isprintable():
            out.append(ch)
        elif ord(ch) < 256:
            out.append("\\x%02x" % ord(ch))
        else:
            out.append("\\u%04x" % ord(ch))
    text = "".join(out)
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


class LocalTimeFormatter(logging.Formatter):
    def converter(self, timestamp):
        return time.gmtime(timestamp + tz_offset_hours() * 3600)


def setup_logging():
    logger = logging.getLogger("zayavki")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return logger
    os.makedirs(LOG_DIR, exist_ok=True)
    handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(LocalTimeFormatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    return logger


log = setup_logging()


# ---------------------------------------------------------------- защита от подбора пароля

class AttemptLimiter:
    """Неверные попытки в памяти: 5 за 15 минут — блокировка."""

    def __init__(self, limit=5, window=15 * 60, soft=2000, hard=5000):
        self.limit, self.window, self.soft, self.hard = limit, window, soft, hard
        self.attempts = {}
        self.lock = threading.Lock()

    def _fresh(self, key, now):
        items = [t for t in self.attempts.get(key, []) if now - t < self.window]
        if items:
            self.attempts[key] = items
        else:
            self.attempts.pop(key, None)
        return items

    def blocked(self, key):
        with self.lock:
            return len(self._fresh(key, time.time())) >= self.limit

    def fail(self, key):
        with self.lock:
            now = time.time()
            self.attempts.setdefault(key, []).append(now)
            if len(self.attempts) > self.soft:
                for k in list(self.attempts):
                    self._fresh(k, now)
            if len(self.attempts) > self.hard:
                newest = sorted(self.attempts.items(), key=lambda kv: max(kv[1]), reverse=True)
                self.attempts = dict(newest[:self.soft])

    def reset(self, key):
        with self.lock:
            self.attempts.pop(key, None)

    def clear(self):
        with self.lock:
            self.attempts.clear()


LOGIN_LIMITER = AttemptLimiter()
IP_LIMITER = AttemptLimiter(limit=20)  # с одного адреса под любыми логинами
PASSWORD_LIMITER = AttemptLimiter()  # неверный текущий пароль при смене пароля
TECH_LIMITER = AttemptLimiter()


# ---------------------------------------------------------------- база данных

def db_path():
    return os.environ.get("DATABASE_PATH") or os.path.join(BASE_DIR, "data", "zayavki.db")


def _connect():
    path = db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.create_function("pylower", 1, lambda s: s.casefold() if isinstance(s, str) else "")
    db.create_function("digits", 1, lambda s: re.sub(r"\D", "", s) if isinstance(s, str) else "")
    return db


def get_db():
    if "db" not in g:
        g.db = _connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_name TEXT NOT NULL, phone TEXT NOT NULL, address TEXT NOT NULL,
    work_type TEXT NOT NULL,
    agreed_date TEXT,
    urgent INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    created_by TEXT NOT NULL, created_at TEXT NOT NULL,
    taken_by TEXT, taken_at TEXT, done_at TEXT,
    report_work TEXT, report_price INTEGER, report_comment TEXT,
    cancelled_by TEXT, cancelled_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL REFERENCES requests(id),
    action TEXT NOT NULL, user_id TEXT NOT NULL, at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_request ON events(request_id);
CREATE TABLE IF NOT EXISTS credentials (
    user_id TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    must_change INTEGER NOT NULL DEFAULT 1,
    version INTEGER NOT NULL DEFAULT 1,
    changed_at TEXT
);
"""


def ensure_column(db, table, column, definition):
    columns = [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    db = _connect()
    try:
        db.executescript(SCHEMA)
        ensure_column(db, "requests", "cancel_reason", "TEXT")
        ensure_column(db, "requests", "report_payment", "TEXT")
        ensure_column(db, "requests", "location", "TEXT")
        ensure_column(db, "requests", "request_type", "TEXT")
        ensure_column(db, "requests", "equipment", "TEXT")
        ensure_column(db, "requests", "warranty", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(db, "requests", "agreed_time", "TEXT")
        ensure_column(db, "events", "details", "TEXT")
        for old, new in OLD_WORK_TYPES.items():
            db.execute("UPDATE requests SET work_type=? WHERE work_type=?", (new, old))
        db.execute(
            "INSERT INTO events (request_id, action, user_id, at) "
            "SELECT id, 'created', created_by, created_at FROM requests "
            "WHERE id NOT IN (SELECT request_id FROM events)"
        )
        db.commit()
    finally:
        db.close()


init_db()


# ---------------------------------------------------------------- пароли: только хеши argon2id

PASSWORD_HASHER = PasswordHasher()  # argon2id с солью; пароль в открытом виде нигде не хранится
MIN_PASSWORD_LENGTH = 8
_TEMP_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # без похожих l/I/1/O/0
DUMMY_HASH = PASSWORD_HASHER.hash("выравнивание-времени-ответа")


def hash_password(password):
    return PASSWORD_HASHER.hash(password)


def check_hash(stored, password):
    try:
        return PASSWORD_HASHER.verify(stored, password)
    except (VerificationError, InvalidHashError):
        return False


def make_temp_password():
    """Временный пароль вида Xk7m-Qp4t-Ra9w-Zc2n: 16 случайных знаков."""
    return "-".join("".join(secrets.choice(_TEMP_ALPHABET) for _ in range(4)) for _ in range(4))


def set_password(user_id, password, must_change, conn=None, password_hash=None):
    own = conn is None
    conn = conn or _connect()
    try:
        conn.execute(
            "INSERT INTO credentials (user_id, password_hash, must_change, version, changed_at) "
            "VALUES (?, ?, ?, 1, ?) ON CONFLICT(user_id) DO UPDATE SET "
            "password_hash = excluded.password_hash, must_change = excluded.must_change, "
            "version = credentials.version + 1, changed_at = excluded.changed_at",
            (user_id, password_hash or hash_password(password), 1 if must_change else 0, now_iso()))
        conn.commit()
    finally:
        if own:
            conn.close()


def issue_temp_passwords(user_ids=None):
    """Выдать новые временные пароли. Все прежние входы этих сотрудников закрываются."""
    result = {}
    for uid in (user_ids or list(USERS)):
        result[uid] = make_temp_password()
        set_password(uid, result[uid], must_change=True)
    return result


def get_credential(db, user_id):
    return db.execute("SELECT * FROM credentials WHERE user_id = ?", (user_id,)).fetchone()


def verify_password(db, user_id, password):
    """Вернуть запись, если пароль верный. Для чужих логинов время ответа такое же."""
    cred = get_credential(db, user_id) if user_id else None
    ok = check_hash(cred["password_hash"] if cred else DUMMY_HASH, password)
    if cred and ok and PASSWORD_HASHER.check_needs_rehash(cred["password_hash"]):
        db.execute("UPDATE credentials SET password_hash = ? WHERE user_id = ?", (hash_password(password), user_id))
        db.commit()
    return cred if (cred and ok) else None


def password_problem(new, current, uid):
    """Текст ошибки, если новый пароль не подходит, иначе None."""
    if len(new) < MIN_PASSWORD_LENGTH:
        return f"Новый пароль слишком короткий: нужно не меньше {MIN_PASSWORD_LENGTH} символов."
    if new == current:
        return "Новый пароль должен отличаться от текущего."
    low = new.casefold()
    for bad in (uid, login_of(uid), person_name(uid), USERS[uid]["name"]):
        if bad and len(bad) >= 3 and bad.casefold() in low:
            return "Пароль не должен содержать ваше имя или логин."
    if len(set(new)) < 6 or new.isdigit():
        return "Пароль слишком простой: добавьте разные буквы, цифры или знаки."
    return None


def add_event(db, request_id, action, user_id, details=None):
    db.execute(
        "INSERT INTO events (request_id, action, user_id, at, details) VALUES (?, ?, ?, ?, ?)",
        (request_id, action, user_id, now_iso(), details),
    )


# ---------------------------------------------------------------- шаблонные фильтры и функции

def _to_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


@app.template_filter("dt")
def dt_filter(value):
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return str(value)


@app.template_filter("d")
def d_filter(value):
    if not value:
        return ""
    try:
        day = _to_date(value)
    except ValueError:
        return str(value)
    text = f"{day.day} {MONTHS_GEN[day.month - 1]}"
    if day.year != now_local().year:
        text += f" {day.year}"
    return text


@app.template_filter("money")
def money_filter(value):
    try:
        return f"{int(value or 0):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "0"


@app.template_filter("month_name")
def month_name_filter(value):
    try:
        year, month = str(value).split("-")
        return f"{MONTHS_NOM[int(month) - 1]} {year}"
    except (ValueError, IndexError):
        return str(value)


@app.template_filter("tel")
def tel_filter(value):
    text = str(value or "").strip()
    digits = re.sub(r"\D", "", text)
    return ("+" if text.startswith("+") else "") + digits


@app.template_filter("who")
def who_filter(uid):
    if uid in USERS:
        return person_name(uid)
    return uid or ""


COORD_RE = re.compile(r"^\s*(-?\d{1,2}(?:[.,]\d+)?)\s*[,;\s]\s*(-?\d{1,3}(?:[.,]\d+)?)\s*$")
URL_RE = re.compile(r"https?://\S+")


def parse_coords(text):
    m = COORD_RE.match(text or "")
    if not m:
        return None
    lat = float(m.group(1).replace(",", "."))
    lon = float(m.group(2).replace(",", "."))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def normalize_location(text):
    """Вернуть (значение или None, ошибка или None)."""
    text = (text or "").strip()
    if not text:
        return None, None
    coords = parse_coords(text)
    if coords:
        return f"{coords[0]:.6f}, {coords[1]:.6f}", None
    m = URL_RE.search(text)
    if m:
        link = m.group(0).rstrip('.,;)»"')
        if len(link) <= 1000:
            return link, None
    return None, "Вставьте ссылку на карту (https://…) или координаты, например 43.2389, 76.8897."


@app.template_global("map_links")
def map_links(location, address):
    """Список кнопок карты: [{label, url}, ...]. Первая — крупная."""
    location = (location or "").strip()
    if location:
        coords = parse_coords(location)
        if coords:
            lat, lon = coords
            return [
                {"label": "Открыть в 2ГИС", "url": f"https://2gis.kz/geo/{lon:.6f},{lat:.6f}"},
                {"label": "Яндекс Карты", "url": f"https://yandex.kz/maps/?pt={lon:.6f},{lat:.6f}&z=17&l=map"},
                {"label": "Google Карты", "url": f"https://www.google.com/maps/search/?api=1&query={lat:.6f},{lon:.6f}"},
            ]
        if URL_RE.match(location):
            return [{"label": "Открыть на карте", "url": location}]
    if (address or "").strip():
        return [{"label": "Найти адрес в 2ГИС", "url": "https://2gis.kz/search/" + quote(address.strip())}]
    return []


# ---------------------------------------------------------------- вход и общие правила

def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


@app.before_request
def load_user():
    g.user = None
    g.must_change = False
    uid = session.get("user_id")
    if uid in USERS:
        cred = get_credential(get_db(), uid)
        # смена пароля закрывает все прежние входы: номер версии в сессии должен совпасть
        if cred is not None and session.get("pv") == cred["version"]:
            g.user = {"id": uid, "name": USERS[uid]["name"], "person": person_name(uid),
                      "role": USERS[uid]["role"]}
            g.must_change = bool(cred["must_change"])
        else:
            session.clear()


@app.before_request
def force_password_change():
    if g.user and g.must_change and request.endpoint not in ("change_password", "logout", "static", "favicon"):
        return redirect(url_for("change_password"))


@app.before_request
def csrf_protect():
    if request.blueprint == "tech" and not tech.enabled():
        abort(404)
    if request.method == "POST":
        sent = request.form.get("csrf", "")
        expected = session.get("csrf", "")
        if not sent or not expected or not secrets_equal(sent, expected):
            abort(400)


@app.context_processor
def inject_globals():
    user = getattr(g, "user", None)
    counts = {"free": 0, "mine": 0}
    if user and user["role"] == "brigade":
        db = get_db()
        counts["free"] = db.execute("SELECT COUNT(*) FROM requests WHERE status='new'").fetchone()[0]
        counts["mine"] = db.execute(
            "SELECT COUNT(*) FROM requests WHERE status='in_work' AND taken_by=?", (user["id"],)
        ).fetchone()[0]
    return {
        "user": user, "counts": counts, "csrf_token": csrf_token, "USERS": USERS,
        "STATUSES": STATUSES, "PAYMENTS": PAYMENTS, "REQUEST_TYPES": REQUEST_TYPES,
        "EQUIPMENT": EQUIPMENT, "AGREED_TIMES": AGREED_TIMES, "EVENT_LABELS": EVENT_LABELS,
    }


@app.after_request
def add_headers(resp):
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; manifest-src 'self'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; object-src 'none'",
    )
    resp.headers.setdefault("Permissions-Policy", "geolocation=(self), camera=(), microphone=(), payment=()")
    resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    resp.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    resp.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
    if not request.path.startswith("/static/"):
        resp.headers.setdefault("Cache-Control", "no-store")  # данные клиентов не остаются в кэше
    if SECURE_COOKIES:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp


@app.after_request
def log_request(resp):
    if request.path.startswith("/static/"):
        return resp
    if session.get("tech_admin"):
        who = "суперадмин"
    else:
        user = getattr(g, "user", None)
        who = user["id"] if user else "-"
    log.info("%s %s %s %s %s", request.remote_addr, who, request.method,
             safe_for_log(request.path), resp.status_code)
    return resp


ERROR_TEXTS = {
    400: "Страница устарела. Вернитесь назад, обновите её и попробуйте ещё раз.",
    404: "Такой страницы или заявки нет.",
    413: "Слишком много текста в форме. Сократите и попробуйте ещё раз.",
    500: "Что-то пошло не так. Обновите страницу и попробуйте ещё раз.",
}


def render_error(code):
    return render_template("error.html", code=code, message=ERROR_TEXTS[code]), code


@app.errorhandler(400)
def err400(_e):
    return render_error(400)


@app.errorhandler(404)
def err404(_e):
    return render_error(404)


@app.errorhandler(413)
def err413(_e):
    return render_error(413)


@app.errorhandler(500)
def err500(_e):
    log.error("ОШИБКА 500 на %s %s", request.method, safe_for_log(request.path))
    return render_error(500)


@app.route("/robots.txt")
def robots():
    return "User-agent: *\nDisallow: /\n", 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(os.path.join(BASE_DIR, "static"), "favicon-32.png", mimetype="image/png")


def login_required(*roles):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if not g.user:
                return redirect(url_for("login"))
            if roles and g.user["role"] not in roles:
                return redirect(url_for("index"))
            return func(*args, **kwargs)
        return wrapper
    return decorator


@app.route("/")
def index():
    if not g.user:
        return redirect(url_for("login"))
    if g.user["role"] == "brigade":
        return redirect(url_for("brigade_free"))
    return redirect(url_for("ofis"))


@app.route("/vhod", methods=["GET", "POST"])
def login():
    error = None
    login_text = ""
    if request.method == "POST":
        login_text = request.form.get("login", "").strip()
        pin = request.form.get("pin", "")
        ip = request.remote_addr
        uid = find_user_by_login(login_text)
        key = (ip, login_text.casefold())
        if not login_text or not pin:
            error = "Введите логин и пароль."
        elif LOGIN_LIMITER.blocked(key) or IP_LIMITER.blocked(ip):
            log.warning("ВХОД заблокирован (много попыток): %s с %s", uid or "?", ip)
            error = "Слишком много неверных попыток. Подождите 15 минут."
        else:
            cred = verify_password(get_db(), uid, pin)
            if cred is not None:
                LOGIN_LIMITER.reset(key)
                session.clear()
                session.permanent = True
                session["user_id"] = uid
                session["pv"] = cred["version"]
                log.info("ВХОД успешно: %s с %s", uid, ip)
                return redirect(url_for("index"))
            LOGIN_LIMITER.fail(key)
            IP_LIMITER.fail(ip)
            log.warning("ВХОД неудачно: %s с %s", uid or "?", ip)
            error = "Неверный логин или пароль."
    elif g.user:
        return redirect(url_for("index"))
    return render_template("login.html", error=error, login_text=login_text)


@app.route("/vyhod", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/parol", methods=["GET", "POST"])
@login_required()
def change_password():
    error = None
    uid = g.user["id"]
    if request.method == "POST":
        current = request.form.get("current", "")
        new1 = request.form.get("new1", "")
        new2 = request.form.get("new2", "")
        ip = request.remote_addr
        if PASSWORD_LIMITER.blocked(uid):
            error = "Слишком много неверных попыток. Подождите 15 минут."
        elif verify_password(get_db(), uid, current) is None:
            PASSWORD_LIMITER.fail(uid)
            log.warning("ПАРОЛЬ смена неудачно: %s с %s", uid, ip)
            error = "Текущий пароль указан неверно."
        elif new1 != new2:
            error = "Новые пароли не совпадают."
        else:
            error = password_problem(new1, current, uid)
        if error is None:
            db = get_db()
            set_password(uid, new1, must_change=False, conn=db)
            PASSWORD_LIMITER.reset(uid)
            version = get_credential(db, uid)["version"]
            session.clear()  # другие устройства с этим логином выйдут
            session.permanent = True
            session["user_id"] = uid
            session["pv"] = version
            log.info("ПАРОЛЬ изменён: %s с %s", uid, ip)
            flash("Пароль изменён.", "ok")
            return redirect(url_for("index"))
    return render_template("parol.html", error=error, forced=g.must_change,
                           min_len=MIN_PASSWORD_LENGTH, title="Смена пароля")


# ---------------------------------------------------------------- разбор форм заявки

def month_options(selected_iso=None):
    today = now_local().date()
    keys = []
    y, m = today.year, today.month
    for _ in range(12):
        keys.append((y, m))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    if selected_iso:
        try:
            sy, sm = int(selected_iso[:4]), int(selected_iso[5:7])
            if (sy, sm) not in keys:
                keys.append((sy, sm))
                keys.sort()
        except ValueError:
            pass
    out = []
    for y, m in keys:
        label = MONTHS_NOM[m - 1] + (f" {y}" if y != today.year else "")
        out.append((f"{y}-{m:02d}", label))
    return out


def parse_agreed(form):
    """(agreed_date, agreed_time, ошибка)."""
    day = form.get("agreed_day", "").strip()
    month = form.get("agreed_month", "").strip()
    time_ = form.get("agreed_time", "").strip()
    if time_ and time_ not in AGREED_TIMES:
        return None, None, "Выберите время из списка."
    if not day:
        if time_:
            return None, None, "Время указано, а день — нет. Выберите и день."
        return None, None, None
    try:
        day_num = int(day)
        year_s, month_s = month.split("-")
        year, month_num = int(year_s), int(month_s)
        if not 1 <= month_num <= 12:
            raise ValueError
    except ValueError:
        return None, None, "Выберите день и месяц."
    try:
        value = date(year, month_num, day_num)
    except ValueError:
        return None, None, f"В месяце «{MONTHS_NOM[month_num - 1].lower()}» нет {day_num}-го числа."
    return value.isoformat(), (time_ or None), None


def parse_report(form):
    """Поля отчёта. Вернуть (значения для показа, ошибки, очищенные значения)."""
    work = form.get("report_work", "").strip()
    price_text = form.get("report_price", "").strip()
    payment = form.get("report_payment", "").strip()
    comment = form.get("report_comment", "").strip()
    errors, clean = {}, {}
    if not work:
        errors["report_work"] = "Напишите, что сделали."
    clean["report_work"] = work
    digits = re.sub(r"\D", "", price_text)
    price = None
    if not digits:
        errors["report_price"] = "Укажите цену цифрами."
    else:
        price = int(digits)
        if price > 100_000_000:
            errors["report_price"] = "Слишком большая сумма, проверьте."
    clean["report_price"] = price
    if price and price > 0:
        if payment not in PAYMENTS:
            errors["report_payment"] = "Выберите, как заплатил клиент."
        clean["report_payment"] = payment if payment in PAYMENTS else None
    else:
        clean["report_payment"] = None
    clean["report_comment"] = comment or None
    shown = {"report_work": work, "report_price": price_text, "report_payment": payment,
             "report_comment": comment}
    return shown, errors, clean


def parse_request_form(form, editing=False):
    """Вернуть (значения для показа, ошибки, очищенные значения)."""
    v = {
        "client_name": form.get("client_name", "").strip(),
        "phone": form.get("phone", "").strip(),
        "address": form.get("address", "").strip(),
        "location": form.get("location", "").strip(),
        "request_type": form.get("request_type", "").strip(),
        "equipment": form.get("equipment", "").strip(),
        "agreed_day": form.get("agreed_day", "").strip(),
        "agreed_month": form.get("agreed_month", "").strip(),
        "agreed_time": form.get("agreed_time", "").strip(),
        "urgent": bool(form.get("urgent")),
        "warranty": bool(form.get("warranty")) if editing else False,
        "description": form.get("description", "").strip(),
    }
    errors = {}
    if not v["client_name"]:
        errors["client_name"] = "Укажите имя клиента."
    if not v["phone"]:
        errors["phone"] = "Укажите телефон."
    elif len(re.sub(r"\D", "", v["phone"])) < 5:
        errors["phone"] = "В телефоне слишком мало цифр."
    if not v["address"]:
        errors["address"] = "Укажите адрес."
    location, loc_err = normalize_location(v["location"])
    if loc_err:
        errors["location"] = loc_err
    if v["request_type"] not in REQUEST_TYPES:
        errors["request_type"] = "Выберите тип заявки."
    if v["equipment"] not in EQUIPMENT:
        errors["equipment"] = "Выберите оборудование."
    agreed_date, agreed_time, agreed_err = parse_agreed(form)
    if agreed_err:
        errors["agreed_date"] = agreed_err
    if not v["description"]:
        errors["description"] = "Опишите, что нужно сделать."
    clean = {
        "client_name": v["client_name"], "phone": v["phone"], "address": v["address"],
        "location": location, "request_type": v["request_type"], "equipment": v["equipment"],
        "agreed_date": agreed_date, "agreed_time": agreed_time,
        "urgent": 1 if v["urgent"] else 0, "warranty": 1 if v["warranty"] else 0,
        "description": v["description"],
    }
    return v, errors, clean


def form_values_from_row(row):
    day = month = ""
    if row["agreed_date"]:
        day = str(int(row["agreed_date"][8:10]))
        month = row["agreed_date"][:7]
    return {
        "client_name": row["client_name"], "phone": row["phone"], "address": row["address"],
        "location": row["location"] or "", "request_type": row["request_type"] or "",
        "equipment": row["equipment"] or "", "agreed_day": day, "agreed_month": month,
        "agreed_time": row["agreed_time"] or "", "urgent": bool(row["urgent"]),
        "warranty": bool(row["warranty"]), "description": row["description"],
        "report_work": row["report_work"] or "",
        "report_price": "" if row["report_price"] is None else str(row["report_price"]),
        "report_payment": row["report_payment"] or "", "report_comment": row["report_comment"] or "",
    }


def render_request_form(values, errors, row=None):
    today = now_local().date()
    selected_month = values.get("agreed_month") or f"{today.year}-{today.month:02d}"
    selected_date = (row["agreed_date"] if row is not None else None) or (
        selected_month + "-01" if values.get("agreed_month") else None)
    return render_template(
        "request_form.html", v=values, errors=errors, row=row,
        month_options=month_options(selected_date), selected_month=selected_month,
        today_iso=today.isoformat(), days=range(1, 32),
        title="Исправить заявку" if row is not None else "Новая заявка",
    )


def get_request(request_id):
    row = get_db().execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
    if row is None:
        abort(404)
    return row


# ---------------------------------------------------------------- офис: список

LIKE_ESC = " ESCAPE '\\'"


def _like(text):
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def build_search(q):
    clauses, params = [], []
    needle = _like(q.casefold())
    for col in ("client_name", "address", "phone", "work_type", "description", "report_work", "report_comment"):
        clauses.append(f"pylower({col}) LIKE ?{LIKE_ESC}")
        params.append(needle)
    for column, labels in (("request_type", REQUEST_TYPES), ("equipment", EQUIPMENT)):
        keys = [k for k, label in labels.items() if q.casefold() in label.casefold()]
        if keys:
            clauses.append(f"{column} IN ({','.join('?' * len(keys))})")
            params.extend(keys)
    m = re.match(r"^№?\s*(\d+)$", q.strip())
    if m:
        clauses.append("id = ?")
        params.append(int(m.group(1)))
    qd = re.sub(r"\D", "", q)
    if len(qd) >= 4:
        clauses.append("digits(phone) LIKE ?")
        params.append("%" + qd + "%")
        if len(qd) >= 7 and qd[0] in "78":
            clauses.append("digits(phone) LIKE ?")
            params.append("%" + qd[1:] + "%")
    return "(" + " OR ".join(clauses) + ")", params


PERIOD_OPTIONS = [("today", "сегодня"), ("yesterday", "вчера"), ("week", "за 7 дней"),
                  ("month", "в этом месяце"), ("prev_month", "в прошлом месяце")]


def period_range(key, today):
    if key == "today":
        return today, today
    if key == "yesterday":
        y = today - timedelta(days=1)
        return y, y
    if key == "week":
        return today - timedelta(days=6), today
    if key == "month":
        return today.replace(day=1), today
    if key == "prev_month":
        last = today.replace(day=1) - timedelta(days=1)
        return last.replace(day=1), last
    return None


@app.route("/ofis")
@login_required("boss", "office")
def ofis():
    db = get_db()
    args = request.args
    q = args.get("q", "").strip()
    fv = {
        "tip": args.get("tip", "") if args.get("tip", "") in REQUEST_TYPES else "",
        "oborud": args.get("oborud", "") if args.get("oborud", "") in EQUIPMENT else "",
        "garantiya": args.get("garantiya", "") if args.get("garantiya", "") in ("da", "net") else "",
        "brigada": args.get("brigada", "") if args.get("brigada", "") in BRIGADE_IDS else "",
        "avtor": args.get("avtor", "") if args.get("avtor", "") in OFFICE_IDS else "",
        "period": args.get("period", "") if args.get("period", "") in dict(PERIOD_OPTIONS) else "",
        "oplata": args.get("oplata", "") if args.get("oplata", "") in PAYMENTS else "",
    }
    clauses, params = [], []
    if q:
        sql, p = build_search(q)
        clauses.append(sql)
        params += p
    if fv["tip"]:
        clauses.append("request_type = ?")
        params.append(fv["tip"])
    if fv["oborud"]:
        clauses.append("equipment = ?")
        params.append(fv["oborud"])
    if fv["garantiya"]:
        clauses.append("warranty = ?")
        params.append(1 if fv["garantiya"] == "da" else 0)
    if fv["brigada"]:
        clauses.append("taken_by = ?")
        params.append(fv["brigada"])
    if fv["avtor"]:
        clauses.append("created_by = ?")
        params.append(fv["avtor"])
    if fv["period"]:
        start, end = period_range(fv["period"], now_local().date())
        clauses.append("status = 'done' AND substr(done_at, 1, 10) BETWEEN ? AND ?")
        params += [start.isoformat(), end.isoformat()]
    if fv["oplata"]:
        clauses.append("report_payment = ?")
        params.append(fv["oplata"])

    tab = args.get("status", "")
    if tab not in dict(STATUS_TABS):
        if q:
            tab = "all"
        elif fv["period"] or fv["oplata"]:
            tab = "done"
        else:
            tab = "new"

    base_where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    counts = {k: 0 for k, _ in STATUS_TABS}
    for row in db.execute(f"SELECT status, COUNT(*) AS c FROM requests {base_where} GROUP BY status", params):
        if row["status"] in counts:
            counts[row["status"]] = row["c"]
    counts["all"] = sum(counts[k] for k in STATUSES)

    where_list, where_params = list(clauses), list(params)
    if tab != "all":
        where_list.append("status = ?")
        where_params.append(tab)
    where = ("WHERE " + " AND ".join(where_list)) if where_list else ""
    rows = db.execute(
        f"SELECT * FROM requests {where} "
        "ORDER BY CASE WHEN urgent = 1 AND status IN ('new','in_work') THEN 0 ELSE 1 END, "
        "CASE status WHEN 'new' THEN 0 WHEN 'in_work' THEN 1 ELSE 2 END, "
        "COALESCE(done_at, cancelled_at, created_at) DESC, id DESC",
        where_params,
    ).fetchall()

    keep = {"q": q, **fv}
    keep = {k: v for k, v in keep.items() if v}
    tabs = []
    for key, label in STATUS_TABS:
        tabs.append({"key": key, "label": label, "count": counts[key], "active": key == tab,
                     "url": url_for("ofis", status=key, **keep)})
    extra_count = sum(1 for v in fv.values() if v)
    has_filters = bool(q) or extra_count > 0

    done_rows = [r for r in rows if r["status"] == "done"]
    pay_summary = []
    if done_rows:
        for key, label in list(PAYMENTS.items()) + [("", "не указана")]:
            group = [r for r in done_rows if (r["report_payment"] or "") == key]
            if group:
                pay_summary.append((label, sum(r["report_price"] or 0 for r in group)))
    other_tabs = [t for t in tabs if not t["active"] and t["key"] != "all" and t["count"] > 0]

    return render_template(
        "ofis.html", rows=rows, tabs=tabs, tab=tab, q=q, fv=fv, extra_count=extra_count,
        has_filters=has_filters, done_count=len(done_rows),
        done_sum=sum(r["report_price"] or 0 for r in done_rows), pay_summary=pay_summary,
        other_tabs=other_tabs, total_all=counts["all"], period_options=PERIOD_OPTIONS,
        brigade_ids=BRIGADE_IDS, office_ids=OFFICE_IDS,
        reset_url=url_for("ofis", status=tab) if tab != "new" else url_for("ofis"),
        title="Заявки",
    )


# ---------------------------------------------------------------- офис: создание, исправление

@app.route("/ofis/novaya", methods=["GET", "POST"])
@login_required("boss", "office")
def ofis_new():
    if request.method == "POST":
        values, errors, c = parse_request_form(request.form)
        if errors:
            return render_request_form(values, errors)
        db = get_db()
        now = now_iso()
        cur = db.execute(
            "INSERT INTO requests (client_name, phone, address, work_type, agreed_date, agreed_time, urgent, "
            "description, status, created_by, created_at, location, request_type, equipment, warranty) "
            "VALUES (?, ?, ?, '', ?, ?, ?, ?, 'new', ?, ?, ?, ?, ?, 0)",
            (c["client_name"], c["phone"], c["address"], c["agreed_date"], c["agreed_time"], c["urgent"],
             c["description"], g.user["id"], now, c["location"], c["request_type"], c["equipment"]),
        )
        add_event(db, cur.lastrowid, "created", g.user["id"])
        db.commit()
        return redirect(url_for("request_card", request_id=cur.lastrowid, created=1))
    return render_request_form({"urgent": False, "warranty": False}, {})


@app.route("/zayavka/<int:request_id>")
@login_required("boss", "office")
def request_card(request_id):
    row = get_request(request_id)
    events = get_db().execute(
        "SELECT * FROM events WHERE request_id=? ORDER BY at, id", (request_id,)).fetchall()
    return render_template("request.html", r=row, events=events,
                           created=request.args.get("created") == "1",
                           title=f"Заявка №{request_id}")


FIELD_LABELS = [
    ("client_name", "имя клиента"), ("phone", "телефон"), ("address", "адрес"),
    ("request_type", "тип заявки"), ("equipment", "оборудование"), ("agreed_date", "день"),
    ("agreed_time", "время"), ("urgent", "срочность"), ("warranty", "гарантия"),
    ("description", "описание"), ("location", "геолокация"),
]
REPORT_LABELS = [("report_work", "что сделано"), ("report_price", "цена"),
                 ("report_payment", "оплата"), ("report_comment", "комментарий")]


def _same(a, b):
    return (a if a not in ("",) else None) == (b if b not in ("",) else None)


@app.route("/zayavka/<int:request_id>/ispravit", methods=["GET", "POST"])
@login_required("boss", "office")
def request_edit(request_id):
    row = get_request(request_id)
    if row["status"] == "cancelled":
        flash("Отменённую заявку исправить нельзя.", "error")
        return redirect(url_for("request_card", request_id=request_id))
    is_done = row["status"] == "done"
    if request.method == "POST":
        values, errors, clean = parse_request_form(request.form, editing=True)
        labels = list(FIELD_LABELS)
        if is_done:
            shown, rep_errors, rep_clean = parse_report(request.form)
            values.update(shown)
            errors.update(rep_errors)
            clean.update(rep_clean)
            labels += REPORT_LABELS
        if errors:
            return render_request_form(values, errors, row)
        changed = [label for key, label in labels if not _same(row[key], clean[key])]
        if not changed:
            flash("Ничего не изменилось.", "error")
            return redirect(url_for("request_card", request_id=request_id))
        db = get_db()
        keys = [key for key, _ in labels]
        sets = ", ".join(f"{k} = ?" for k in keys) + ", updated_at = ?"
        db.execute(f"UPDATE requests SET {sets} WHERE id = ?",
                   [clean[k] for k in keys] + [now_iso(), request_id])
        add_event(db, request_id, "edited", g.user["id"], "исправлено: " + ", ".join(changed))
        db.commit()
        flash("Заявка исправлена.", "ok")
        return redirect(url_for("request_card", request_id=request_id))
    return render_request_form(form_values_from_row(row), {}, row)


@app.route("/zayavka/<int:request_id>/garantiya", methods=["POST"])
@login_required("boss", "office")
def request_warranty(request_id):
    row = get_request(request_id)
    if row["status"] == "cancelled":
        flash("У отменённой заявки гарантию не меняют.", "error")
        return redirect(url_for("request_card", request_id=request_id))
    new_value = 0 if row["warranty"] else 1
    db = get_db()
    db.execute("UPDATE requests SET warranty=?, updated_at=? WHERE id=?", (new_value, now_iso(), request_id))
    add_event(db, request_id, "warranty", g.user["id"], "гарантийная" if new_value else "платная")
    db.commit()
    flash("Заявка отмечена как гарантийная." if new_value else "Заявка отмечена как платная.", "ok")
    return redirect(url_for("request_card", request_id=request_id))


@app.route("/zayavka/<int:request_id>/otmenit", methods=["POST"])
@login_required("boss", "office")
def request_cancel(request_id):
    get_request(request_id)
    reason = request.form.get("reason", "").strip() or None
    db = get_db()
    now = now_iso()
    cur = db.execute(
        "UPDATE requests SET status='cancelled', cancelled_by=?, cancelled_at=?, cancel_reason=?, updated_at=? "
        "WHERE id=? AND status IN ('new','in_work')",
        (g.user["id"], now, reason, now, request_id),
    )
    if cur.rowcount == 0:
        db.rollback()
        flash("Эту заявку уже нельзя отменить.", "error")
        return redirect(url_for("request_card", request_id=request_id))
    add_event(db, request_id, "cancelled", g.user["id"], reason)
    db.commit()
    flash(f"Заявка №{request_id} отменена.", "ok")
    return redirect(url_for("request_card", request_id=request_id))


# ---------------------------------------------------------------- статистика руководителя

def stats_data():
    return stats.compute(get_db(), now_local().date(), request.args.get("p", ""), request.args.get("d", ""),
                         BRIGADE_IDS, OFFICE_IDS, PAYMENTS, REQUEST_TYPES, EQUIPMENT)


@app.route("/ofis/statistika")
@login_required("boss")
def ofis_stats():
    return render_template("stats.html", s=stats_data(), base_url=url_for("ofis_stats"), title="Статистика")


# ---------------------------------------------------------------- бригада

BRIGADE_ORDER = "ORDER BY urgent DESC, agreed_date IS NULL, agreed_date, id"


@app.route("/brigada")
@login_required("brigade")
def brigade_free():
    rows = get_db().execute(f"SELECT * FROM requests WHERE status='new' {BRIGADE_ORDER}").fetchall()
    return render_template("brigade_free.html", rows=rows, title="Свободные заявки")


@app.route("/brigada/moi")
@login_required("brigade")
def brigade_mine():
    db = get_db()
    rows = db.execute(
        f"SELECT * FROM requests WHERE status='in_work' AND taken_by=? {BRIGADE_ORDER}",
        (g.user["id"],)).fetchall()
    recent = db.execute(
        "SELECT * FROM requests WHERE status='done' AND taken_by=? ORDER BY done_at DESC, id DESC LIMIT 20",
        (g.user["id"],)).fetchall()
    return render_template("brigade_mine.html", rows=rows, recent=recent, title="Мои заявки")


@app.route("/brigada/zayavka/<int:request_id>")
@login_required("brigade")
def brigade_request(request_id):
    row = get_request(request_id)
    mine = row["taken_by"] == g.user["id"] and row["status"] in ("in_work", "done")
    if row["status"] != "new" and not mine:
        flash("Эту заявку уже взяла другая бригада или её отменили.", "error")
        return redirect(url_for("brigade_free"))
    return render_template("brigade_request.html", r=row, mine=mine, title=f"Заявка №{request_id}")


@app.route("/brigada/zayavka/<int:request_id>/vzyat", methods=["POST"])
@login_required("brigade")
def brigade_take(request_id):
    get_request(request_id)
    db = get_db()
    now = now_iso()
    cur = db.execute(
        "UPDATE requests SET status='in_work', taken_by=?, taken_at=?, updated_at=? "
        "WHERE id=? AND status='new'", (g.user["id"], now, now, request_id))
    if cur.rowcount == 0:
        db.rollback()
        flash("Не получилось: эту заявку только что взяла другая бригада.", "error")
        return redirect(url_for("brigade_free"))
    add_event(db, request_id, "taken", g.user["id"])
    db.commit()
    flash(f"Заявка №{request_id} ваша. Она теперь в «Мои заявки».", "ok")
    return redirect(url_for("brigade_request", request_id=request_id))


@app.route("/brigada/zayavka/<int:request_id>/vernut", methods=["POST"])
@login_required("brigade")
def brigade_return(request_id):
    get_request(request_id)
    db = get_db()
    cur = db.execute(
        "UPDATE requests SET status='new', taken_by=NULL, taken_at=NULL, updated_at=? "
        "WHERE id=? AND status='in_work' AND taken_by=?", (now_iso(), request_id, g.user["id"]))
    if cur.rowcount == 0:
        db.rollback()
        flash("Эту заявку нельзя вернуть: она не у вас в работе.", "error")
        return redirect(url_for("brigade_mine"))
    add_event(db, request_id, "returned", g.user["id"])
    db.commit()
    flash(f"Заявка №{request_id} возвращена в свободные.", "ok")
    return redirect(url_for("brigade_mine"))


@app.route("/brigada/zayavka/<int:request_id>/otchet", methods=["GET", "POST"])
@login_required("brigade")
def brigade_report(request_id):
    row = get_request(request_id)
    if row["status"] != "in_work" or row["taken_by"] != g.user["id"]:
        flash("Отчёт можно написать только по своей заявке в работе.", "error")
        return redirect(url_for("brigade_mine"))
    values, errors = {"report_work": "", "report_price": "", "report_payment": "", "report_comment": ""}, {}
    if request.method == "POST":
        values, errors, clean = parse_report(request.form)
        if not errors:
            db = get_db()
            now = now_iso()
            cur = db.execute(
                "UPDATE requests SET status='done', done_at=?, report_work=?, report_price=?, "
                "report_payment=?, report_comment=?, updated_at=? "
                "WHERE id=? AND status='in_work' AND taken_by=?",
                (now, clean["report_work"], clean["report_price"], clean["report_payment"],
                 clean["report_comment"], now, request_id, g.user["id"]))
            if cur.rowcount == 0:
                db.rollback()
                flash("Не получилось сохранить: заявка уже не у вас в работе.", "error")
                return redirect(url_for("brigade_mine"))
            add_event(db, request_id, "done", g.user["id"])
            db.commit()
            flash(f"Отчёт сохранён. Заявка №{request_id} выполнена.", "ok")
            return redirect(url_for("brigade_mine"))
    return render_template("brigade_report.html", r=row, v=values, errors=errors,
                           title=f"Отчёт по заявке №{request_id}")


# ---------------------------------------------------------------- технический раздел

import tech  # noqa: E402

tech.init_app(app, sys.modules[__name__])


# ---------------------------------------------------------------- резервные копии

def start_backup_thread():
    """Раз в день сама делает копию базы и фото. Выключить: BACKUP_AUTO=0."""
    if os.environ.get("BACKUP_AUTO", "1") == "0":
        return

    def loop():
        while True:
            try:
                path = backup.run_daily(db_path(), now_local().date())
                if path:
                    log.info("КОПИЯ создана: %s", safe_for_log(os.path.basename(path)))
            except Exception as exc:  # сбой копии не должен ронять сайт
                log.error("КОПИЯ не удалась: %s", safe_for_log(exc))
            time.sleep(3600)

    threading.Thread(target=loop, name="backup", daemon=True).start()


start_backup_thread()


def print_temp_passwords(passwords):
    print("\nВРЕМЕННЫЕ ПАРОЛИ (показываются один раз; при первом входе сотрудник задаст свой):")
    for uid, password in passwords.items():
        print(f"  {person_name(uid):<14} логин: {login_of(uid):<14} пароль: {password}")
    print()


def set_env_value(key, value):
    """Записать одну настройку в .env (остальные строки не трогаем)."""
    lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            lines = f.read().splitlines()
    out, done = [], False
    for line in lines:
        name = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and name == key:
            if not done:
                out.append(f"{key}={value}")
                done = True
            continue
        out.append(line)
    if not done:
        out.append(f"{key}={value}")
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


if __name__ == "__main__":
    if "--novye-paroli" in sys.argv:
        print_temp_passwords(issue_temp_passwords())
        sys.exit(0)
    if "--tech-parol" in sys.argv:
        new = secrets.token_urlsafe(18)
        tech_login = os.environ.get("SUPERADMIN_LOGIN") or "admin"
        set_env_value("SUPERADMIN_LOGIN", tech_login)
        set_env_value("SUPERADMIN_PASSWORD_HASH", hash_password(new))
        print(f"\nТехнический вход /tech/vhod: логин {tech_login}, пароль {new}")
        print("Пароль показан один раз. Перезапустите сайт.\n")
        sys.exit(0)
    conn = _connect()
    no_passwords = conn.execute("SELECT COUNT(*) FROM credentials").fetchone()[0] == 0
    conn.close()
    if no_passwords:  # самый первый запуск: выдаём временные пароли
        print_temp_passwords(issue_temp_passwords())
    port = int(os.environ.get("PORT", "5000"))
    if "--telefon" in sys.argv:
        app.run(host="0.0.0.0", port=port, debug=False)
    else:
        # режим отладки показывает код при ошибке: только у себя на компьютере и только с --otladka
        app.run(host="127.0.0.1", port=port, debug="--otladka" in sys.argv and not SECURE_COOKIES)
