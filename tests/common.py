"""Общая основа автопроверок: временная база, свои логины и пароли, помощники."""
import atexit
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import timedelta

TMP = tempfile.mkdtemp(prefix="zayavki-test-")
os.environ["DATABASE_PATH"] = os.path.join(TMP, "test.db")
os.environ["TZ_OFFSET_HOURS"] = "3"
os.environ["COOKIE_SECURE"] = "0"
os.environ["SUPERADMIN_LOGIN"] = "tech-test"
os.environ["BACKUP_AUTO"] = "0"  # в тестах фоновая копия не нужна
os.environ["LOG_DIR"] = os.path.join(TMP, "logs")  # свой журнал, не мешает работающему сайту
os.environ["BACKUP_DIR"] = os.path.join(TMP, "backups")
os.environ["UPLOADS_DIR"] = os.path.join(TMP, "uploads")
# Тестовые пароли сотрудников (в базе хранятся только их хеши)
PINS = {"rukovoditel": "Test-Boss-pass1", "manager1": "Test-Man1-pass2", "manager2": "Test-Man2-pass3",
        "brigada1": "Test-Brig1-pass4", "brigada2": "Test-Brig2-pass5"}
for _uid in PINS:
    os.environ.pop("PIN_" + _uid.upper(), None)
    os.environ["LOGIN_" + _uid.upper()] = ""
    os.environ["NAME_" + _uid.upper()] = ""

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as zapp  # noqa: E402
from argon2 import PasswordHasher  # noqa: E402

PRODUCTION_HASHER = zapp.PASSWORD_HASHER
# В тестах хеш argon2 «полегче», чтобы сотни входов не шли минутами. Алгоритм тот же.
zapp.PASSWORD_HASHER = PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1)
TEST_HASHES = {uid: zapp.hash_password(pw) for uid, pw in PINS.items()}

atexit.register(lambda: shutil.rmtree(TMP, ignore_errors=True))
CSRF_RE = re.compile(r'name="csrf" value="([^"]+)"')
TECH_LOGIN, TECH_PASSWORD = "tech-test", "test-password-123"
TECH_HASH = zapp.hash_password(TECH_PASSWORD)
os.environ["SUPERADMIN_PASSWORD_HASH"] = TECH_HASH
os.environ.pop("SUPERADMIN_PASSWORD", None)


def reset_credentials():
    conn = db()
    conn.execute("DELETE FROM credentials")
    for uid, password_hash in TEST_HASHES.items():
        conn.execute("INSERT INTO credentials (user_id, password_hash, must_change, version, changed_at) "
                     "VALUES (?, ?, 0, 1, '2026-01-01T00:00:00')", (uid, password_hash))
    conn.commit()
    conn.close()


def db():
    conn = sqlite3.connect(os.environ["DATABASE_PATH"])
    conn.row_factory = sqlite3.Row
    return conn


def now():
    return zapp.now_local()


def valid_form(**over):
    n = now()
    data = {
        "client_name": "Иван Петров", "phone": "+7 701 123-45-67", "address": "ул. Абая, 10",
        "location": "", "request_type": "remont", "equipment": "kamery",
        "agreed_day": "", "agreed_month": f"{n.year}-{n.month:02d}", "agreed_time": "",
        "description": "Не работает камера",
    }
    data.update(over)
    return data


def insert_request(**over):
    """Вставить заявку прямо в базу (для проверки статистики и списков)."""
    stamp = now().isoformat()
    row = {
        "client_name": "Клиент", "phone": "+7 700 000-00-00", "address": "Адрес", "work_type": "",
        "urgent": 0, "description": "Описание", "status": "new", "created_by": "manager1",
        "created_at": stamp, "request_type": "remont", "equipment": "kamery", "warranty": 0,
    }
    row.update(over)
    conn = db()
    cols = ", ".join(row)
    cur = conn.execute(f"INSERT INTO requests ({cols}) VALUES ({','.join('?' * len(row))})", list(row.values()))
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    return rid


def insert_event(request_id, action, user_id, at=None, details=None):
    conn = db()
    conn.execute("INSERT INTO events (request_id, action, user_id, at, details) VALUES (?,?,?,?,?)",
                 (request_id, action, user_id, at or now().isoformat(), details))
    conn.commit()
    conn.close()


def get_row(request_id):
    conn = db()
    row = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
    conn.close()
    return row


def get_events(request_id):
    conn = db()
    rows = conn.execute("SELECT * FROM events WHERE request_id=? ORDER BY id", (request_id,)).fetchall()
    conn.close()
    return rows


def last_id():
    conn = db()
    value = conn.execute("SELECT MAX(id) FROM requests").fetchone()[0]
    conn.close()
    return value


class AppTestCase(unittest.TestCase):
    def setUp(self):
        zapp.app.config["TESTING"] = True
        conn = db()
        conn.execute("DELETE FROM events")
        conn.execute("DELETE FROM requests")
        conn.commit()
        conn.close()
        reset_credentials()
        for limiter in (zapp.LOGIN_LIMITER, zapp.IP_LIMITER, zapp.PASSWORD_LIMITER, zapp.TECH_LIMITER):
            limiter.clear()
        for uid in PINS:
            os.environ["LOGIN_" + uid.upper()] = ""
            os.environ["NAME_" + uid.upper()] = ""
        os.environ["SUPERADMIN_LOGIN"] = TECH_LOGIN
        os.environ["SUPERADMIN_PASSWORD_HASH"] = TECH_HASH
        os.environ.pop("SUPERADMIN_PASSWORD", None)

    # --- вход и формы
    def token(self, client):
        html = client.get("/", follow_redirects=True).get_data(as_text=True)
        found = CSRF_RE.search(html)
        return found.group(1) if found else ""

    def login(self, uid, client=None):
        client = client or zapp.app.test_client()
        resp = client.post("/vhod", data={"login": uid, "pin": PINS[uid], "csrf": self.token(client)})
        self.assertEqual(resp.status_code, 302, f"вход {uid} не удался")
        return client

    def post(self, client, url, data=None, **kw):
        data = dict(data or {})
        data["csrf"] = self.token(client)
        return client.post(url, data=data, **kw)

    def text(self, resp):
        return resp.get_data(as_text=True)

    def create(self, client, **over):
        resp = self.post(client, "/ofis/novaya", valid_form(**over))
        self.assertEqual(resp.status_code, 302, self.text(resp)[:500])
        return last_id()

    def boss(self):
        return self.login("rukovoditel")

    def manager(self, n=1):
        return self.login(f"manager{n}")

    def brigade(self, n=1):
        return self.login(f"brigada{n}")

    def tech_login(self, client=None):
        client = client or zapp.app.test_client()
        return client, client.post("/tech/vhod", data={"login": TECH_LOGIN, "password": TECH_PASSWORD,
                                                       "csrf": self.token(client)})

    def done_request(self, price=1000, payment="cash", brigade=1, **over):
        """Создать и довести до «Выполнена» через сайт."""
        office = self.manager()
        rid = self.create(office, **over)
        b = self.brigade(brigade)
        self.post(b, f"/brigada/zayavka/{rid}/vzyat")
        self.post(b, f"/brigada/zayavka/{rid}/otchet",
                  {"report_work": "Сделали", "report_price": str(price), "report_payment": payment})
        return rid
