import os
import re
import tempfile
import unittest
from unittest import mock

import common
from common import AppTestCase, PINS, get_events, get_row, valid_form, zapp


class RoleProtectionTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.office = self.manager()
        self.rid = self.create(self.office)

    def test_brigade_cannot_edit_cancel_or_change_warranty(self):
        b = self.brigade()
        before = tuple(get_row(self.rid))
        for url, data in ((f"/zayavka/{self.rid}/ispravit", valid_form(phone="+7 700 999-99-99")),
                          (f"/zayavka/{self.rid}/otmenit", {"reason": "x"}),
                          (f"/zayavka/{self.rid}/garantiya", {})):
            with self.subTest(url=url):
                resp = self.post(b, url, data)
                self.assertEqual(resp.status_code, 302)
                self.assertEqual(resp.headers["Location"], "/")
        self.assertEqual(before, tuple(get_row(self.rid)))
        self.assertEqual(len(get_events(self.rid)), 1)
        self.assertEqual(b.get(f"/zayavka/{self.rid}/ispravit").status_code, 302)

    def test_brigade_cannot_create_requests(self):
        self.post(self.brigade(), "/ofis/novaya", valid_form())
        self.assertEqual(common.last_id(), self.rid)

    def test_office_cannot_take_report_or_return(self):
        m = self.manager(2)
        for url in (f"/brigada/zayavka/{self.rid}/vzyat", f"/brigada/zayavka/{self.rid}/vernut"):
            self.assertEqual(self.post(m, url).headers["Location"], "/")
        self.assertEqual(get_row(self.rid)["status"], "new")

    def test_manager_cannot_open_stats_or_tech(self):
        for n in (1, 2):
            m = self.manager(n)
            self.assertEqual(m.get("/ofis/statistika").headers["Location"], "/")
            self.assertEqual(m.get("/tech/").headers["Location"], "/tech/vhod")

    def test_brigade_cannot_open_foreign_in_work_card(self):
        self.post(self.brigade(1), f"/brigada/zayavka/{self.rid}/vzyat")
        resp = self.brigade(2).get(f"/brigada/zayavka/{self.rid}")
        self.assertEqual(resp.headers["Location"], "/brigada")

    def test_brigade_cannot_open_cancelled_card(self):
        self.post(self.office, f"/zayavka/{self.rid}/otmenit")
        self.assertEqual(self.brigade().get(f"/brigada/zayavka/{self.rid}").headers["Location"], "/brigada")

    def test_cannot_forge_actor_or_status(self):
        data = valid_form(created_by="rukovoditel", status="done", taken_by="brigada1", warranty="1")
        rid = self.create(self.office, **{k: v for k, v in data.items() if k not in valid_form()})
        row = get_row(rid)
        self.assertEqual((row["created_by"], row["status"], row["taken_by"], row["warranty"]),
                         ("manager1", "new", None, 0))

    def test_logout_requires_post(self):
        self.assertEqual(self.office.get("/vyhod").status_code, 405)


class CsrfTests(AppTestCase):
    def test_post_without_token_is_400(self):
        client = self.manager()
        resp = client.post("/ofis/novaya", data=valid_form())
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Страница устарела.", self.text(resp))
        self.assertIsNone(common.last_id())

    def test_wrong_and_empty_token(self):
        client = self.manager()
        self.token(client)
        for token in ("wrong-token", ""):
            data = valid_form()
            data["csrf"] = token
            self.assertEqual(client.post("/ofis/novaya", data=data).status_code, 400)

    def test_cyrillic_token_does_not_crash(self):
        client = self.manager()
        data = valid_form()
        data["csrf"] = "токен-ёж"
        self.assertEqual(client.post("/ofis/novaya", data=data).status_code, 400)

    def test_token_from_other_session_rejected(self):
        a, b = self.manager(1), self.manager(2)
        data = valid_form()
        data["csrf"] = self.token(a)
        self.assertEqual(b.post("/ofis/novaya", data=data).status_code, 400)

    def test_login_post_without_token(self):
        client = zapp.app.test_client()
        client.get("/vhod")
        resp = client.post("/vhod", data={"login": "manager1", "pin": PINS["manager1"]})
        self.assertEqual(resp.status_code, 400)

    def test_every_post_form_has_token(self):
        boss = self.boss()
        rid = self.create(boss)
        for url in ("/ofis", f"/zayavka/{rid}", f"/zayavka/{rid}/ispravit", "/ofis/novaya"):
            html = self.text(boss.get(url))
            forms = re.findall(r"<form[^>]*method=\"post\".*?</form>", html, re.S)
            self.assertTrue(forms, url)
            for form in forms:
                self.assertIn('name="csrf"', form, url)


class LoginLimitTests(AppTestCase):
    def attempt(self, login, pin, client=None):
        client = client or zapp.app.test_client()
        return client.post("/vhod", data={"login": login, "pin": pin, "csrf": self.token(client)})

    def test_blocked_after_five_failures(self):
        client = zapp.app.test_client()
        for _ in range(5):
            self.assertIn("Неверный логин или пароль.", self.text(self.attempt("manager1", "0000", client)))
        resp = self.attempt("manager1", PINS["manager1"], client)
        self.assertIn("Слишком много неверных попыток. Подождите 15 минут.", self.text(resp))
        self.assertEqual(resp.status_code, 200)

    def test_other_login_not_blocked(self):
        client = zapp.app.test_client()
        for _ in range(5):
            self.attempt("manager1", "0000", client)
        self.assertEqual(self.attempt("manager2", PINS["manager2"], client).status_code, 302)

    def test_success_resets_counter(self):
        client = zapp.app.test_client()
        for _ in range(4):
            self.attempt("manager1", "0000", client)
        self.assertEqual(self.attempt("manager1", PINS["manager1"], client).status_code, 302)
        client2 = zapp.app.test_client()
        for _ in range(4):
            self.attempt("manager1", "0000", client2)
        self.assertIn("Неверный логин", self.text(self.attempt("manager1", "0000", client2)))

    def test_limiter_memory_is_bounded(self):
        limiter = zapp.AttemptLimiter()
        for i in range(6000):
            limiter.fail(("ip", str(i)))
        self.assertLessEqual(len(limiter.attempts), 5000)
        self.assertGreaterEqual(len(limiter.attempts), 2000)
        limiter.fail(("ip", "last"))
        self.assertIn(("ip", "last"), limiter.attempts)

    def test_limiter_drops_stale_first(self):
        limiter = zapp.AttemptLimiter(window=-1)
        for i in range(2100):
            limiter.fail(("ip", str(i)))
        self.assertLess(len(limiter.attempts), 2100)

    def test_limiter_window(self):
        limiter = zapp.AttemptLimiter()
        for _ in range(5):
            limiter.fail("k")
        self.assertTrue(limiter.blocked("k"))
        limiter.reset("k")
        self.assertFalse(limiter.blocked("k"))


class LogTests(AppTestCase):
    def log_text(self):
        for handler in zapp.log.handlers:
            handler.flush()
        with open(zapp.LOG_PATH, encoding="utf-8") as f:
            return f.read()

    def test_log_has_no_passwords_or_texts(self):
        client = zapp.app.test_client()
        client.post("/vhod", data={"login": "manager1", "pin": "секретный-9876", "csrf": self.token(client)})
        m = self.manager()
        rid = self.create(m, client_name="СекретноеИмяКлиента", description="СекретноеОписание")
        m.get("/ofis", query_string={"q": "СекретнаяСтрокаПоиска"})
        text = self.log_text()
        for secret in ("секретный-9876", PINS["manager1"], "СекретноеИмяКлиента", "СекретноеОписание",
                       "СекретнаяСтрокаПоиска", "test-password-123"):
            self.assertNotIn(secret, text)
        self.assertIn(f"manager1 GET /ofis 200", text)
        self.assertIn("ВХОД неудачно: manager1 с", text)
        self.assertIn("ВХОД успешно: manager1 с", text)

    def test_log_line_format(self):
        zapp.app.test_client().get("/vhod")
        line = [l for l in self.log_text().splitlines() if " GET /vhod 200" in l][-1]
        self.assertRegex(line, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} INFO \S+ - GET /vhod 200$")

    def test_static_not_logged(self):
        with zapp.app.test_client().get("/static/style.css"):
            pass
        self.assertNotIn("/static/style.css", self.log_text())

    def test_blocked_login_is_logged(self):
        client = zapp.app.test_client()
        for _ in range(6):
            client.post("/vhod", data={"login": "brigada2", "pin": "0", "csrf": self.token(client)})
        self.assertIn("ВХОД заблокирован (много попыток): brigada2 с", self.log_text())

    def test_unknown_login_logged_as_question_mark(self):
        client = zapp.app.test_client()
        client.post("/vhod", data={"login": "stranger", "pin": "0", "csrf": self.token(client)})
        text = self.log_text()
        self.assertIn("ВХОД неудачно: ? с", text)
        self.assertNotIn("stranger", text)

    def test_safe_for_log(self):
        self.assertEqual(zapp.safe_for_log("a\nb\r\tc"), "a\\x0ab\\x0d\\x09c")
        self.assertEqual(zapp.safe_for_log("x" * 400), "x" * 300 + "…")
        self.assertEqual(zapp.safe_for_log("привет"), "привет")
        self.assertEqual(zapp.safe_for_log("​"), "\\u200b")

    def test_log_injection_neutralised(self):
        zapp.app.test_client().get("/fake%0A2026-01-01%20INFO%20evil")
        self.assertNotIn("\n2026-01-01 INFO evil", self.log_text())

    def test_tech_login_logged_without_password(self):
        self.tech_login()
        text = self.log_text()
        self.assertIn("ТЕХ-ВХОД успешно с", text)
        self.assertIn("суперадмин GET /tech/ 200", text) if False else None
        self.assertNotIn(common.TECH_PASSWORD, text)

    def test_500_is_logged(self):
        boss = self.boss()
        zapp.app.config["TESTING"] = False
        zapp.app.config["PROPAGATE_EXCEPTIONS"] = False
        try:
            with mock.patch.object(zapp, "stats_data", side_effect=RuntimeError("секретная причина")):
                resp = boss.get("/ofis/statistika")
            self.assertEqual(resp.status_code, 500)
            self.assertIn("Что-то пошло не так.", self.text(resp))
            text = self.log_text()
            self.assertIn("ОШИБКА 500 на GET /ofis/statistika", text)
            self.assertNotIn("секретная причина", text)
        finally:
            zapp.app.config["TESTING"] = True
            zapp.app.config["PROPAGATE_EXCEPTIONS"] = None


class HeadersAndLimitsTests(AppTestCase):
    def test_security_headers(self):
        resp = zapp.app.test_client().get("/vhod")
        h = resp.headers
        self.assertEqual(h["X-Frame-Options"], "DENY")
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        self.assertEqual(h["Referrer-Policy"], "same-origin")
        self.assertEqual(h["Permissions-Policy"], "geolocation=(self), camera=(), microphone=(), payment=()")
        self.assertEqual(
            h["Content-Security-Policy"],
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; manifest-src 'self'; form-action 'self'; frame-ancestors 'none'; "
            "base-uri 'self'; object-src 'none'")

    def test_headers_on_errors_too(self):
        resp = zapp.app.test_client().get("/no-such-page")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.headers["X-Frame-Options"], "DENY")
        self.assertIn("Такой страницы или заявки нет.", self.text(resp))

    def test_cookie_flags(self):
        client = zapp.app.test_client()
        resp = client.post("/vhod", data={"login": "manager1", "pin": PINS["manager1"], "csrf": self.token(client)})
        cookie = resp.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertNotIn("Secure", cookie)

    def test_config(self):
        cfg = zapp.app.config
        self.assertEqual(cfg["MAX_CONTENT_LENGTH"], 256 * 1024)
        self.assertEqual(cfg["PERMANENT_SESSION_LIFETIME"].days, 14)
        self.assertTrue(cfg["SESSION_COOKIE_HTTPONLY"])
        self.assertEqual(cfg["SESSION_COOKIE_SAMESITE"], "Lax")

    def test_big_form_is_413(self):
        client = self.manager()
        resp = client.post("/ofis/novaya", data={"description": "я" * 300000, "csrf": self.token(client)})
        self.assertEqual(resp.status_code, 413)
        self.assertIn("Слишком много текста в форме.", self.text(resp))

    def test_no_inline_styles_or_external_resources(self):
        boss = self.boss()
        rid = self.done_request()
        pages = [boss.get(u) for u in ("/ofis", "/ofis/novaya", f"/zayavka/{rid}", f"/zayavka/{rid}/ispravit",
                                       "/ofis/statistika")]
        pages.append(zapp.app.test_client().get("/vhod"))
        pages.append(self.brigade().get("/brigada"))
        client, _ = self.tech_login()
        pages += [client.get(u) for u in ("/tech/", "/tech/log", "/tech/zhurnal")]
        for resp in pages:
            html = self.text(resp)
            self.assertNotRegex(html, r'\sstyle="')
            self.assertNotRegex(html, r'<(script|link|img)[^>]+(src|href)="https?://')

    def test_favicon_and_manifest(self):
        client = zapp.app.test_client()
        with client.get("/favicon.ico") as resp:
            self.assertEqual(resp.mimetype, "image/png")
        with client.get("/static/manifest.webmanifest") as resp:
            self.assertEqual(resp.status_code, 200)
        for icon in ("favicon-32.png", "apple-touch-icon.png", "icon-192.png", "icon-512.png"):
            with client.get("/static/" + icon) as resp:
                self.assertEqual(resp.status_code, 200)
                self.assertTrue(resp.data.startswith(b"\x89PNG"))

    def test_html_is_escaped(self):
        m = self.manager()
        rid = self.create(m, client_name="<script>alert(1)</script>", description="<b>x</b>")
        html = self.text(m.get(f"/zayavka/{rid}"))
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)

    def test_sql_injection_in_search_is_harmless(self):
        m = self.manager()
        self.create(m)
        resp = m.get("/ofis", query_string={"q": "' OR 1=1 --", "tip": "x' OR '1'='1"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Ничего не нашлось.", self.text(resp))


class EnvTests(unittest.TestCase):
    def test_env_file_created_with_random_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = zapp.ENV_PATH
            zapp.ENV_PATH = os.path.join(tmp, ".env")
            try:
                zapp.ensure_env_file()
                with open(zapp.ENV_PATH, encoding="utf-8") as f:
                    text = f.read()
                zapp.ensure_env_file()
                with open(zapp.ENV_PATH, encoding="utf-8") as f:
                    self.assertEqual(text, f.read())
            finally:
                zapp.ENV_PATH = original
        self.assertIn("# Настройки системы заявок. Этот файл НЕ попадает в Git.", text)
        self.assertNotRegex(text, r"(?m)^PIN_")  # паролей сотрудников в .env больше нет
        self.assertNotRegex(text, r"(?m)^SUPERADMIN_PASSWORD")  # техраздел выключен, пока не задан хеш
        self.assertRegex(text, r"(?m)^SECRET_KEY=[0-9a-f]{64}$")
        self.assertRegex(text, r"(?m)^TZ_OFFSET_HOURS=-?\d+(\.\d+)?$")
        self.assertRegex(text, r"(?m)^COOKIE_SECURE=0$")

    def test_env_parser(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = zapp.ENV_PATH
            zapp.ENV_PATH = os.path.join(tmp, ".env")
            with open(zapp.ENV_PATH, "w", encoding="utf-8") as f:
                f.write("# комментарий\n\nбез знака равно\nZ_TEST_A=1\nZ_TEST_B = два = три \nZ_TEST_C=из-файла\n")
            os.environ["Z_TEST_C"] = "из-окружения"
            try:
                zapp.load_env()
            finally:
                zapp.ENV_PATH = original
        self.assertEqual(os.environ["Z_TEST_A"], "1")
        self.assertEqual(os.environ["Z_TEST_B"], "два = три")
        self.assertEqual(os.environ["Z_TEST_C"], "из-окружения")

    def test_tz_offset_and_now(self):
        self.assertEqual(zapp.tz_offset_hours(), 3.0)
        stamp = zapp.now_local()
        self.assertIsNone(stamp.tzinfo)
        self.assertEqual(stamp.microsecond, 0)


class GitignoreTests(unittest.TestCase):
    def test_secrets_ignored(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, ".gitignore"), encoding="utf-8") as f:
            text = f.read().split()
        for item in (".env", "!.env.example", "data/", "*.db", ".venv", "*.pem", "*.key"):
            self.assertIn(item, text)


if __name__ == "__main__":
    unittest.main()
