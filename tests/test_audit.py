"""Тесты аудита безопасности: пароли, подбор, SQL-инъекции, обход прав, файлы, копии, секреты."""
import io
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import date, timedelta

import backup
import common
from common import AppTestCase, PINS, get_row, valid_form, zapp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class PasswordStorageTests(AppTestCase):
    def test_only_argon2_hashes_in_database(self):
        conn = common.db()
        rows = conn.execute("SELECT user_id, password_hash FROM credentials").fetchall()
        conn.close()
        self.assertEqual(len(rows), 5)
        for row in rows:
            self.assertTrue(row["password_hash"].startswith("$argon2id$"), row["user_id"])
            self.assertNotIn(PINS[row["user_id"]], row["password_hash"])

    def test_no_plain_password_in_db_file(self):
        temp = zapp.issue_temp_passwords(["manager1"])["manager1"]
        with open(os.environ["DATABASE_PATH"], "rb") as f:
            raw = f.read()
        self.assertNotIn(temp.encode(), raw)
        for pw in PINS.values():
            self.assertNotIn(pw.encode(), raw)

    def test_production_hasher_is_strong_argon2id(self):
        h = common.PRODUCTION_HASHER.hash("пример")
        self.assertTrue(h.startswith("$argon2id$v=19$m=65536,t=3,"))

    def test_same_password_gives_different_hashes(self):
        self.assertNotEqual(zapp.hash_password("Одинаковый-пароль1"), zapp.hash_password("Одинаковый-пароль1"))

    def test_old_pin_from_env_no_longer_works(self):
        os.environ["PIN_MANAGER1"] = "9999"
        try:
            client = zapp.app.test_client()
            resp = client.post("/vhod", data={"login": "manager1", "pin": "9999", "csrf": self.token(client)})
            self.assertIn("Неверный логин или пароль.", self.text(resp))
        finally:
            os.environ.pop("PIN_MANAGER1", None)

    def test_temp_password_format(self):
        seen = set()
        for _ in range(50):
            pw = zapp.make_temp_password()
            self.assertRegex(pw, r"^[A-Za-z2-9]{4}-[A-Za-z2-9]{4}-[A-Za-z2-9]{4}-[A-Za-z2-9]{4}$")
            self.assertFalse(set(pw) & set("lI1O0o"))
            seen.add(pw)
        self.assertEqual(len(seen), 50)

    def test_password_is_case_sensitive(self):
        client = zapp.app.test_client()
        resp = client.post("/vhod", data={"login": "manager1", "pin": PINS["manager1"].lower(),
                                          "csrf": self.token(client)})
        self.assertIn("Неверный логин или пароль.", self.text(resp))


class TempPasswordFlowTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.temp = zapp.issue_temp_passwords(["manager1"])["manager1"]

    def login_temp(self):
        client = zapp.app.test_client()
        resp = client.post("/vhod", data={"login": "manager1", "pin": self.temp, "csrf": self.token(client)})
        self.assertEqual(resp.status_code, 302)
        return client

    def change(self, client, current, new1, new2=None):
        return self.post(client, "/parol", {"current": current, "new1": new1, "new2": new1 if new2 is None else new2},
                         follow_redirects=True)

    def test_must_change_before_anything(self):
        client = self.login_temp()
        for url in ("/", "/ofis", "/ofis/novaya", "/zayavka/1", "/brigada"):
            resp = client.get(url)
            self.assertEqual(resp.headers["Location"], "/parol", url)
        html = self.text(client.get("/parol"))
        self.assertIn("Вы вошли по временному паролю.", html)

    def test_cannot_act_before_change(self):
        client = self.login_temp()
        resp = self.post(client, "/ofis/novaya", valid_form())
        self.assertEqual(resp.headers["Location"], "/parol")
        self.assertIsNone(common.last_id())

    def test_weak_passwords_rejected(self):
        client = self.login_temp()
        cases = {
            "корот1": "слишком короткий",
            "manager1-Пароль-x": "не должен содержать ваше имя или логин",
            "111111111111": "слишком простой",
            "aaaaaaaaaaaaaa": "слишком простой",
        }
        for new, message in cases.items():
            with self.subTest(new=new):
                self.assertIn(message, self.text(self.change(client, self.temp, new)))
        self.assertIn("Новые пароли не совпадают.", self.text(self.change(client, self.temp, "Good-Pass-2026x", "Other-Pass-2026x")))
        self.assertIn("должен отличаться от текущего", self.text(self.change(client, self.temp, self.temp)))
        self.assertIn("Текущий пароль указан неверно.", self.text(self.change(client, "wrong", "Good-Pass-2026x")))

    def test_name_in_password_rejected(self):
        os.environ["NAME_MANAGER1"] = "Яна"
        os.environ["LOGIN_MANAGER1"] = "Яна"
        client = zapp.app.test_client()
        client.post("/vhod", data={"login": "яна", "pin": self.temp, "csrf": self.token(client)})
        self.assertIn("не должен содержать", self.text(self.change(client, self.temp, "МояЯна-2026-xyz")))

    def test_successful_change(self):
        client = self.login_temp()
        other = self.login_temp()  # второе устройство
        resp = self.change(client, self.temp, "Good-Pass-2026x")
        self.assertIn("Пароль изменён.", self.text(resp))
        self.assertEqual(client.get("/ofis").status_code, 200)
        self.assertEqual(other.get("/ofis").headers["Location"], "/vhod")  # старый вход закрыт
        conn = common.db()
        cred = conn.execute("SELECT * FROM credentials WHERE user_id='manager1'").fetchone()
        conn.close()
        self.assertEqual(cred["must_change"], 0)
        fresh = zapp.app.test_client()
        bad = fresh.post("/vhod", data={"login": "manager1", "pin": self.temp, "csrf": self.token(fresh)})
        self.assertIn("Неверный логин или пароль.", self.text(bad))
        good = fresh.post("/vhod", data={"login": "manager1", "pin": "Good-Pass-2026x", "csrf": self.token(fresh)})
        self.assertEqual(good.headers["Location"], "/")

    def test_new_temp_password_logs_everyone_out(self):
        client = self.login("manager2")
        zapp.issue_temp_passwords(["manager2"])
        self.assertEqual(client.get("/ofis").headers["Location"], "/vhod")

    def test_change_password_brute_force_blocked(self):
        client = self.login_temp()
        for _ in range(5):
            self.change(client, "wrong-guess", "Good-Pass-2026x")
        resp = self.change(client, self.temp, "Good-Pass-2026x")
        self.assertIn("Слишком много неверных попыток.", self.text(resp))

    def test_voluntary_change_page(self):
        client = self.login("brigada1")
        html = self.text(client.get("/brigada"))
        self.assertIn('href="/parol"', html)
        resp = self.change(client, PINS["brigada1"], "Brig-New-Pass-77")
        self.assertIn("Пароль изменён.", self.text(resp))

    def test_change_requires_login_and_csrf(self):
        anon = zapp.app.test_client()
        self.assertEqual(anon.get("/parol").headers["Location"], "/vhod")
        client = self.login_temp()
        resp = client.post("/parol", data={"current": self.temp, "new1": "Good-Pass-2026x", "new2": "Good-Pass-2026x"})
        self.assertEqual(resp.status_code, 400)

    def test_password_change_logged_without_password(self):
        client = self.login_temp()
        self.change(client, self.temp, "Secret-Logged-99x")
        for handler in zapp.log.handlers:
            handler.flush()
        with open(zapp.LOG_PATH, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("ПАРОЛЬ изменён: manager1", text)
        self.assertNotIn("Secret-Logged-99x", text)
        self.assertNotIn(self.temp, text)


class BruteForceTests(AppTestCase):
    def test_per_login_lockout(self):
        client = zapp.app.test_client()
        for _ in range(5):
            client.post("/vhod", data={"login": "brigada1", "pin": "guess", "csrf": self.token(client)})
        resp = client.post("/vhod", data={"login": "brigada1", "pin": PINS["brigada1"], "csrf": self.token(client)})
        self.assertIn("Слишком много неверных попыток", self.text(resp))

    def test_per_ip_lockout_across_logins(self):
        client = zapp.app.test_client()
        for i in range(20):
            client.post("/vhod", data={"login": f"guess{i}", "pin": "x", "csrf": self.token(client)})
        resp = client.post("/vhod", data={"login": "manager2", "pin": PINS["manager2"], "csrf": self.token(client)})
        self.assertIn("Слишком много неверных попыток", self.text(resp))

    def test_unknown_and_known_logins_same_answer(self):
        client = zapp.app.test_client()
        a = self.text(client.post("/vhod", data={"login": "manager1", "pin": "x", "csrf": self.token(client)}))
        b = self.text(client.post("/vhod", data={"login": "nobody", "pin": "x", "csrf": self.token(client)}))
        self.assertIn("Неверный логин или пароль.", a)
        self.assertIn("Неверный логин или пароль.", b)


class InjectionTests(AppTestCase):
    PAYLOADS = ["' OR '1'='1", "1; DROP TABLE requests; --", "\" OR 1=1 --", "%' UNION SELECT password_hash, 1 FROM credentials --",
                "1) OR (1=1", "'; UPDATE credentials SET must_change=0; --"]

    def setUp(self):
        super().setUp()
        self.m = self.manager()
        self.rid = self.create(self.m, client_name="Обычный клиент")

    def counts(self):
        conn = common.db()
        out = (conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0],
               conn.execute("SELECT COUNT(*) FROM credentials").fetchone()[0])
        conn.close()
        return out

    def test_search_and_filters(self):
        before = self.counts()
        for payload in self.PAYLOADS:
            for param in ("q", "tip", "oborud", "brigada", "avtor", "period", "oplata", "status", "garantiya"):
                resp = self.m.get("/ofis", query_string={param: payload})
                self.assertEqual(resp.status_code, 200)
                self.assertNotIn("$argon2", self.text(resp))
        html = self.text(self.m.get("/ofis", query_string={"q": "' OR '1'='1"}))
        self.assertNotIn(f"№{self.rid}</span>", html)
        self.assertEqual(before, self.counts())

    def test_form_fields_stored_as_text(self):
        for payload in self.PAYLOADS:
            rid = self.create(self.m, client_name=payload, address=payload, description=payload)
            self.assertEqual(get_row(rid)["client_name"], payload)
        self.assertEqual(self.counts()[1], 5)

    def test_numeric_routes_reject_text(self):
        for url in ("/zayavka/1%20OR%201=1", "/zayavka/' OR 1=1", "/brigada/zayavka/1;DROP"):
            self.assertEqual(self.m.get(url).status_code, 404)

    def test_stats_and_tech_params(self):
        boss = self.boss()
        for payload in self.PAYLOADS:
            self.assertEqual(boss.get("/ofis/statistika", query_string={"p": payload, "d": payload}).status_code, 200)
        client, _ = self.tech_login()
        for payload in self.PAYLOADS:
            for params in ({"kto": payload}, {"deystvie": payload}, {"zayavka": payload}, {"soderzhit": payload}):
                self.assertEqual(client.get("/tech/zhurnal", query_string=params).status_code, 200)
        self.assertEqual(self.counts()[0], 1)

    def test_no_string_built_values_in_sql(self):
        """В SQL склеиваются только имена столбцов из кода; значения всегда идут через «?»."""
        for name in ("app.py", "tech.py", "stats.py", "backup.py"):
            with open(os.path.join(ROOT, name), encoding="utf-8") as f:
                code = f.read()
            for m in re.finditer(r'execute\(\s*f"([^"]*)"', code):
                for expr in re.findall(r"\{([^}]*)\}", m.group(1)):
                    self.assertRegex(expr.strip(), r"^(table|column|definition|base_where|where|sets|BRIGADE_ORDER|cols|"
                                                   r"','\.join\('\?' \* len\(\w+\)\))$", f"{name}: {expr}")


class XssTests(AppTestCase):
    def test_user_text_escaped_everywhere(self):
        evil = '<img src=x onerror=alert(1)>"\'<script>alert(2)</script>'
        m = self.manager()
        rid = self.create(m, client_name=evil, address=evil, description=evil)
        b = self.brigade()
        self.post(b, f"/brigada/zayavka/{rid}/vzyat")
        self.post(b, f"/brigada/zayavka/{rid}/otchet", {"report_work": evil, "report_price": "1", "report_payment": "cash",
                                                        "report_comment": evil})
        self.post(m, f"/zayavka/{rid}/ispravit", valid_form(client_name=evil + "!", address=evil, description=evil,
                                                             report_work=evil, report_price="1", report_payment="cash"))
        pages = [m.get(u) for u in ("/ofis?status=all", f"/zayavka/{rid}", f"/zayavka/{rid}/ispravit")]
        pages += [b.get(u) for u in ("/brigada/moi", f"/brigada/zayavka/{rid}")]
        client, _ = self.tech_login()
        pages += [client.get(u) for u in ("/tech/zhurnal", f"/tech/zayavka/{rid}")]
        for resp in pages:
            html = self.text(resp)
            self.assertNotIn("<img src=x", html)
            self.assertNotIn("<script>alert(2)", html)

    def test_javascript_link_not_rendered(self):
        m = self.manager()
        resp = self.post(m, "/ofis/novaya", valid_form(location="javascript:alert(1)"))
        self.assertIn("Вставьте ссылку на карту", self.text(resp))
        rid = self.create(m, location='https://evil.example/"onmouseover="alert(1)')
        html = self.text(m.get(f"/zayavka/{rid}"))
        self.assertNotIn('"onmouseover="alert(1)', html)


class HeaderTests(AppTestCase):
    def test_strict_csp_no_inline_scripts(self):
        boss = self.boss()
        rid = self.done_request()
        pages = [boss.get(u) for u in ("/ofis", "/ofis/novaya", f"/zayavka/{rid}", f"/zayavka/{rid}/ispravit",
                                       "/ofis/statistika", "/parol")]
        b = self.brigade()
        rid2 = self.create(self.manager())
        self.post(b, f"/brigada/zayavka/{rid2}/vzyat")
        pages += [b.get("/brigada"), b.get(f"/brigada/zayavka/{rid2}"), zapp.app.test_client().get("/vhod")]
        for resp in pages:
            self.assertNotIn("unsafe-inline", resp.headers["Content-Security-Policy"])
            html = self.text(resp)
            self.assertNotRegex(html, r"<script(?![^>]*\ssrc=)")  # только внешние файлы скриптов
            self.assertNotRegex(html, r"\son[a-z]+=\"")  # никаких onclick/onsubmit
        self.assertIn('data-confirm="Вернуть заявку', self.text(b.get(f"/brigada/zayavka/{rid2}")))

    def test_extra_headers(self):
        client = self.manager()
        resp = client.get("/ofis")
        self.assertEqual(resp.headers["Cache-Control"], "no-store")
        self.assertEqual(resp.headers["Cross-Origin-Opener-Policy"], "same-origin")
        self.assertEqual(resp.headers["Cross-Origin-Resource-Policy"], "same-origin")
        self.assertEqual(resp.headers["X-Robots-Tag"], "noindex, nofollow")
        self.assertNotIn("Strict-Transport-Security", resp.headers)  # только на https
        with client.get("/static/style.css") as static:
            self.assertNotEqual(static.headers.get("Cache-Control"), "no-store")

    def test_robots_txt(self):
        resp = zapp.app.test_client().get("/robots.txt")
        self.assertEqual(self.text(resp), "User-agent: *\nDisallow: /\n")

    def test_debug_is_off(self):
        self.assertFalse(zapp.app.debug)
        with open(os.path.join(ROOT, "app.py"), encoding="utf-8") as f:
            code = f.read()
        self.assertNotIn("debug=True", code)
        self.assertIn('"--otladka" in sys.argv and not SECURE_COOKIES', code)


class ProductionConfigTests(unittest.TestCase):
    def run_app(self, env_extra):
        env = dict(os.environ)
        env.update({"BACKUP_AUTO": "0", "DATABASE_PATH": os.path.join(tempfile.mkdtemp(), "x.db")})
        env.update(env_extra)
        code = ("import app; c=app.app.config; "
                "print(c['SESSION_COOKIE_SECURE'], c['SESSION_COOKIE_NAME'], c['SESSION_COOKIE_HTTPONLY'], "
                "c['SESSION_COOKIE_SAMESITE'], c.get('TRUSTED_HOSTS'), app.app.debug)")
        return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True,
                              encoding="utf-8", timeout=60)

    def test_https_mode(self):
        res = self.run_app({"COOKIE_SECURE": "1", "SECRET_KEY": "k" * 64, "ALLOWED_HOSTS": "zayavki.example.kz"})
        self.assertEqual(res.stdout.strip(), "True __Host-zayavki True Lax ['zayavki.example.kz'] False", res.stderr)

    def test_https_mode_refuses_short_secret(self):
        res = self.run_app({"COOKIE_SECURE": "1", "SECRET_KEY": "short"})
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("SECRET_KEY", res.stderr)

    def test_secure_cookie_and_hsts(self):
        zapp.app.config.update(SESSION_COOKIE_SECURE=True)
        try:
            common.reset_credentials()
            zapp.LOGIN_LIMITER.clear()
            zapp.IP_LIMITER.clear()
            client = zapp.app.test_client()
            html = client.get("/vhod", base_url="https://localhost").get_data(as_text=True)
            token = common.CSRF_RE.search(html).group(1)
            resp = client.post("/vhod", base_url="https://localhost",
                               data={"login": "manager1", "pin": PINS["manager1"], "csrf": token})
            self.assertEqual(resp.status_code, 302)
            cookie = resp.headers["Set-Cookie"]
            self.assertIn("Secure", cookie)
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=Lax", cookie)
            self.assertIn("Expires=", cookie)
        finally:
            zapp.app.config.update(SESSION_COOKIE_SECURE=False)


class AccessControlTests(AppTestCase):
    """Подстановка чужого номера заявки в адрес и вызов чужих действий."""

    def setUp(self):
        super().setUp()
        self.m = self.manager()
        self.b1, self.b2 = self.brigade(1), self.brigade(2)
        self.mine = self.create(self.m, address="Адрес бригады 1")
        self.post(self.b1, f"/brigada/zayavka/{self.mine}/vzyat")
        self.done = self.create(self.m, address="Выполненная бригадой 1")
        self.post(self.b1, f"/brigada/zayavka/{self.done}/vzyat")
        self.post(self.b1, f"/brigada/zayavka/{self.done}/otchet", {"report_work": "x", "report_price": "500",
                                                                     "report_payment": "cash"})
        self.cancelled = self.create(self.m)
        self.post(self.m, f"/zayavka/{self.cancelled}/otmenit")

    def test_brigade_cannot_open_foreign_requests_by_number(self):
        for rid in (self.mine, self.done, self.cancelled):
            resp = self.b2.get(f"/brigada/zayavka/{rid}")
            self.assertEqual(resp.headers["Location"], "/brigada", rid)
            self.assertNotIn("Адрес бригады 1", self.text(self.b2.get(f"/brigada/zayavka/{rid}", follow_redirects=True)))

    def test_brigade_cannot_act_on_foreign_requests(self):
        before = (tuple(get_row(self.mine)), tuple(get_row(self.done)))
        for url, data in ((f"/brigada/zayavka/{self.mine}/vernut", {}),
                          (f"/brigada/zayavka/{self.mine}/otchet", {"report_work": "x", "report_price": "0"}),
                          (f"/brigada/zayavka/{self.done}/otchet", {"report_work": "x", "report_price": "0"}),
                          (f"/brigada/zayavka/{self.mine}/vzyat", {}),
                          (f"/brigada/zayavka/{self.cancelled}/vzyat", {})):
            self.post(self.b2, url, data)
        self.assertEqual(before, (tuple(get_row(self.mine)), tuple(get_row(self.done))))
        self.assertEqual(get_row(self.cancelled)["status"], "cancelled")

    def test_brigade_cannot_use_office_pages(self):
        for url in ("/ofis", f"/zayavka/{self.mine}", f"/zayavka/{self.mine}/ispravit", "/ofis/statistika",
                    "/ofis/novaya", "/tech/", "/tech/zhurnal"):
            resp = self.b1.get(url)
            self.assertEqual(resp.status_code, 302, url)
        for url in (f"/zayavka/{self.mine}/otmenit", f"/zayavka/{self.mine}/garantiya", f"/zayavka/{self.mine}/ispravit"):
            self.assertIn(self.post(self.b1, url, valid_form()).headers["Location"], ("/",))
        self.assertEqual(get_row(self.mine)["status"], "in_work")

    def test_every_route_is_protected(self):
        """Все адреса сайта, кроме входа и служебных, требуют входа."""
        public = {"/vhod", "/favicon.ico", "/robots.txt", "/static/<path:filename>", "/tech/vhod"}
        anon = zapp.app.test_client()
        for rule in zapp.app.url_map.iter_rules():
            if rule.rule in public or "GET" not in rule.methods:
                continue
            url = re.sub(r"<int:\w+>", str(self.mine), rule.rule)
            resp = anon.get(url)
            self.assertIn(resp.status_code, (302, 404), url)
            if resp.status_code == 302:
                self.assertIn(resp.headers["Location"], ("/vhod", "/tech/vhod"), url)

    def test_every_post_route_needs_csrf(self):
        for rule in zapp.app.url_map.iter_rules():
            if "POST" not in rule.methods:
                continue
            url = re.sub(r"<int:\w+>", str(self.mine), rule.rule)
            self.assertEqual(self.m.post(url, data={"x": "1"}).status_code, 400, url)

    def test_stats_only_for_boss(self):
        self.assertEqual(self.boss().get("/ofis/statistika").status_code, 200)
        for client in (self.m, self.manager(2), self.b1):
            self.assertEqual(client.get("/ofis/statistika").status_code, 302)

    def test_missing_numbers_do_not_leak(self):
        self.assertEqual(self.m.get("/zayavka/99999").status_code, 404)
        self.assertEqual(self.b1.get("/brigada/zayavka/99999").status_code, 404)


class FileUploadTests(AppTestCase):
    """На сайте нет загрузки файлов: любые присланные файлы должны отбрасываться."""

    def snapshot(self):
        out = set()
        for folder in (os.path.join(ROOT, "static"), os.path.join(ROOT, "templates"), os.environ["UPLOADS_DIR"]):
            for root, _d, files in os.walk(folder):
                out.update(os.path.join(root, f) for f in files)
        return out

    def test_malicious_files_are_ignored(self):
        before = self.snapshot()
        m = self.manager()
        evil_files = {"shell.php": b"<?php system($_GET['c']); ?>", "page.html": b"<script>alert(1)</script>",
                      "photo.jpg.exe": b"MZ\x90\x00", "../../app.py": b"print('pwned')"}
        for name, content in evil_files.items():
            data = valid_form()
            data["csrf"] = self.token(m)
            data["photo"] = (io.BytesIO(content), name)
            resp = m.post("/ofis/novaya", data=data, content_type="multipart/form-data")
            self.assertEqual(resp.status_code, 302)
        b = self.brigade()
        rid = common.last_id()
        self.post(b, f"/brigada/zayavka/{rid}/vzyat")
        data = {"report_work": "x", "report_price": "0", "csrf": self.token(b),
                "file": (io.BytesIO(b"<?php ?>"), "x.php")}
        b.post(f"/brigada/zayavka/{rid}/otchet", data=data, content_type="multipart/form-data")
        self.assertEqual(before, self.snapshot())
        with open(os.path.join(ROOT, "app.py"), encoding="utf-8") as f:
            self.assertNotIn("pwned", f.read())

    def test_big_file_rejected(self):
        m = self.manager()
        data = valid_form()
        data["csrf"] = self.token(m)
        data["photo"] = (io.BytesIO(b"\x00" * (300 * 1024)), "big.jpg")
        resp = m.post("/ofis/novaya", data=data, content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 413)
        self.assertIsNone(common.last_id())

    def test_no_path_traversal_through_static(self):
        client = zapp.app.test_client()
        for url in ("/static/../app.py", "/static/..%2fapp.py", "/static/%2e%2e/.env", "/static/../data/zayavki.db"):
            resp = client.get(url)
            self.assertIn(resp.status_code, (404, 400), url)
            self.assertNotIn(b"SECRET_KEY", resp.data)
            resp.close()

    def test_data_folder_not_public(self):
        client = zapp.app.test_client()
        for url in ("/data/zayavki.db", "/.env", "/data/backups/", "/backup.py", "/app.py"):
            self.assertEqual(client.get(url).status_code, 404, url)


class BackupTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp()
        m = self.manager()
        for i in range(3):
            self.create(m, client_name=f"Клиент для копии {i}")

    def test_backup_created_and_valid(self):
        path = backup.make_backup(os.environ["DATABASE_PATH"], date(2026, 11, 9), self.dir)
        self.assertTrue(path.endswith("zayavki-2026-11-09.db"))
        ok, info = backup.verify(path)
        self.assertTrue(ok)
        self.assertIn("заявок 3", info)

    def test_daily_only_once(self):
        db_path = os.environ["DATABASE_PATH"]
        self.assertIsNotNone(backup.run_daily(db_path, date(2026, 11, 9), self.dir))
        self.assertIsNone(backup.run_daily(db_path, date(2026, 11, 9), self.dir))
        self.assertIsNotNone(backup.run_daily(db_path, date(2026, 11, 10), self.dir))

    def test_old_copies_removed(self):
        db_path = os.environ["DATABASE_PATH"]
        start = date(2026, 1, 1)
        for i in range(20):
            backup.make_backup(db_path, start + timedelta(days=i), self.dir)
        files = sorted(f for f in os.listdir(self.dir) if f.endswith(".db"))
        self.assertEqual(len(files), backup.KEEP_DAYS)
        self.assertEqual(files[-1], "zayavki-2026-01-20.db")

    def test_restore_brings_data_back(self):
        src = os.environ["DATABASE_PATH"]
        copy_path = backup.make_backup(src, date(2026, 11, 9), self.dir)
        work = os.path.join(self.dir, "work.db")
        import shutil
        shutil.copy2(src, work)
        conn = sqlite3.connect(work)
        conn.execute("DELETE FROM requests")
        conn.commit()
        conn.close()
        ok, info = backup.restore(copy_path, work)
        self.assertTrue(ok)
        self.assertIn("заявок 3", info)
        self.assertTrue(any(n.startswith("work.db.before-restore-") for n in os.listdir(self.dir)))

    def test_restore_refuses_broken_copy(self):
        broken = os.path.join(self.dir, "broken.db")
        with open(broken, "wb") as f:
            f.write(b"not a database")
        target = os.path.join(self.dir, "target.db")
        with open(target, "wb") as f:
            f.write(b"keep me")
        with self.assertRaises(RuntimeError):
            backup.restore(broken, target)
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"keep me")

    def test_photos_backed_up_and_restored(self):
        photos = os.environ["UPLOADS_DIR"]
        os.makedirs(os.path.join(photos, "2026"), exist_ok=True)
        with open(os.path.join(photos, "2026", "a.jpg"), "wb") as f:
            f.write(b"\xff\xd8photo")
        try:
            backup.make_backup(os.environ["DATABASE_PATH"], date(2026, 11, 9), self.dir)
            archive = os.path.join(self.dir, "zayavki-2026-11-09-photos.zip")
            self.assertTrue(os.path.exists(archive))
            os.remove(os.path.join(photos, "2026", "a.jpg"))
            work = os.path.join(self.dir, "w.db")
            backup.restore(os.path.join(self.dir, "zayavki-2026-11-09.db"), work, archive)
            with open(os.path.join(photos, "2026", "a.jpg"), "rb") as f:
                self.assertEqual(f.read(), b"\xff\xd8photo")
        finally:
            import shutil
            shutil.rmtree(photos, ignore_errors=True)

    def test_evil_photo_archive_rejected(self):
        evil = os.path.join(self.dir, "evil.zip")
        with zipfile.ZipFile(evil, "w") as z:
            z.writestr("../../escape.txt", "x")
        good = backup.make_backup(os.environ["DATABASE_PATH"], date(2026, 11, 9), self.dir)
        with self.assertRaises(RuntimeError):
            backup.restore(good, os.path.join(self.dir, "t.db"), evil)

    def test_backups_not_in_git(self):
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as f:
            self.assertIn("data/", f.read().split())


class SecretsTests(unittest.TestCase):
    def tracked_files(self):
        res = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, capture_output=True,
                             text=True, encoding="utf-8")
        return [f for f in res.stdout.splitlines() if f and not f.startswith(".venv")]

    def test_no_secrets_in_files_that_go_to_git(self):
        patterns = [r"SECRET_KEY=[0-9a-f]{32,}", r"\$argon2id\$v=19\$m=\d+,t=\d+,p=\d+\$[A-Za-z0-9+/]{16,}",
                    r"(?m)^PIN_[A-Z0-9]+=\S+", r"(?m)^SUPERADMIN_PASSWORD=\S+", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"]
        files = self.tracked_files()
        self.assertNotIn(".env", files)
        for name in files:
            path = os.path.join(ROOT, name)
            if not os.path.isfile(path) or name.endswith((".png", ".db")):
                continue
            with open(path, encoding="utf-8", errors="ignore") as f:
                text = f.read()
            for pattern in patterns:
                self.assertIsNone(re.search(pattern, text), f"{name}: {pattern}")

    def test_gitignore_covers_secrets(self):
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as f:
            items = f.read().split()
        for item in (".env", ".env.*", "!.env.example", "data/", "*.db", "*.pem", "*.key", "*.log"):
            self.assertIn(item, items)

    def test_git_history_has_no_env(self):
        res = subprocess.run(["git", "log", "--all", "--name-only", "--pretty=format:"], cwd=ROOT,
                             capture_output=True, text=True, encoding="utf-8")
        names = set(res.stdout.split())
        self.assertNotIn(".env", names)
        self.assertFalse(any(n.endswith(".db") for n in names))


if __name__ == "__main__":
    unittest.main()
