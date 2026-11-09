import os
import unittest

import common
from common import AppTestCase, TECH_LOGIN, TECH_PASSWORD, zapp


class TechIsolationTests(AppTestCase):
    def test_disabled_without_settings(self):
        variants = (("", ""), ("tech-test", ""), ("tech-test", "not-a-hash"), ("", common.TECH_HASH),
                    ("tech-test", "plain-password-123"))
        for login, password_hash in variants:
            with self.subTest(login=login, password_hash=password_hash[:12]):
                os.environ["SUPERADMIN_LOGIN"] = login
                os.environ["SUPERADMIN_PASSWORD_HASH"] = password_hash
                client = zapp.app.test_client()
                for url in ("/tech/", "/tech/vhod", "/tech/log", "/tech/zhurnal", "/tech/zayavka/1"):
                    self.assertEqual(client.get(url).status_code, 404, url)
                self.assertEqual(client.post("/tech/vhod", data={"login": "x", "password": "y"}).status_code, 404)

    def test_anonymous_redirected_to_tech_login(self):
        client = zapp.app.test_client()
        for url in ("/tech/", "/tech/log", "/tech/zhurnal"):
            resp = client.get(url)
            self.assertEqual(resp.status_code, 302)
            self.assertEqual(resp.headers["Location"], "/tech/vhod")

    def test_login_page_texts(self):
        html = self.text(zapp.app.test_client().get("/tech/vhod"))
        self.assertIn("Технический вход", html)
        self.assertIn("Только для разработчика и системного аудита.", html)

    def test_wrong_password(self):
        client = zapp.app.test_client()
        resp = client.post("/tech/vhod", data={"login": TECH_LOGIN, "password": "wrong-password-1", "csrf": self.token(client)})
        self.assertIn("Неверный логин или пароль.", self.text(resp))

    def test_cyrillic_password_does_not_crash(self):
        client = zapp.app.test_client()
        resp = client.post("/tech/vhod", data={"login": "админ", "password": "пароль-пароль-1", "csrf": self.token(client)})
        self.assertEqual(resp.status_code, 200)

    def test_lockout_after_five_failures(self):
        client = zapp.app.test_client()
        for _ in range(5):
            client.post("/tech/vhod", data={"login": TECH_LOGIN, "password": "bad-password-12", "csrf": self.token(client)})
        resp = client.post("/tech/vhod", data={"login": TECH_LOGIN, "password": TECH_PASSWORD, "csrf": self.token(client)})
        self.assertIn("Слишком много попыток. Подождите 15 минут.", self.text(resp))

    def test_successful_login_and_pages(self):
        client, resp = self.tech_login()
        self.assertEqual(resp.headers["Location"], "/tech/")
        html = self.text(client.get("/tech/"))
        self.assertIn("Сквозная статистика", html)
        self.assertIn("Технический раздел · только чтение", html)
        self.assertIn("Сайт запущен", html)
        self.assertIn("заявок 0, записей журнала 0", html)
        for url in ("/tech/zhurnal", "/tech/log"):
            self.assertEqual(client.get(url).status_code, 200)

    def test_headers_no_store_and_noindex(self):
        client, _ = self.tech_login()
        for url in ("/tech/", "/tech/vhod", "/tech/log"):
            resp = client.get(url)
            self.assertEqual(resp.headers["Cache-Control"], "no-store")
            self.assertEqual(resp.headers["X-Robots-Tag"], "noindex, nofollow")

    def test_tech_session_does_not_open_staff_pages(self):
        client, _ = self.tech_login()
        for url in ("/ofis", "/ofis/statistika", "/brigada", "/zayavka/1"):
            self.assertEqual(client.get(url).headers["Location"], "/vhod", url)

    def test_staff_session_does_not_open_tech(self):
        for who_login in ("rukovoditel", "manager1", "brigada1"):
            client = self.login(who_login)
            resp = client.get("/tech/")
            self.assertEqual(resp.headers["Location"], "/tech/vhod")
            self.assertEqual(client.get("/ofis" if who_login != "brigada1" else "/brigada").status_code, 200)

    def test_tech_login_closes_staff_session(self):
        client = self.login("manager1")
        client.post("/tech/vhod", data={"login": TECH_LOGIN, "password": TECH_PASSWORD, "csrf": self.token(client)})
        self.assertEqual(client.get("/ofis").headers["Location"], "/vhod")

    def test_staff_login_closes_tech_session(self):
        client, _ = self.tech_login()
        client.post("/vhod", data={"login": "manager1", "pin": common.PINS["manager1"], "csrf": self.token(client)})
        self.assertEqual(client.get("/tech/").headers["Location"], "/tech/vhod")

    def test_logout(self):
        client, _ = self.tech_login()
        resp = self.post(client, "/tech/vyhod")
        self.assertEqual(resp.headers["Location"], "/tech/vhod")
        self.assertEqual(client.get("/tech/").status_code, 302)

    def test_session_expires_after_eight_hours(self):
        client, _ = self.tech_login()
        with client.session_transaction() as sess:
            sess["tech_since"] = "2000-01-01T00:00:00"
        self.assertEqual(client.get("/tech/").headers["Location"], "/tech/vhod")

    def test_tech_header_has_no_staff_name(self):
        client, _ = self.tech_login()
        html = self.text(client.get("/tech/"))
        self.assertNotIn('class="who"', html)
        self.assertNotIn('class="tabs"', html)


class TechReadOnlyTests(AppTestCase):
    def test_post_only_on_login_and_logout(self):
        post_routes = []
        for rule in zapp.app.url_map.iter_rules():
            if rule.endpoint.startswith("tech.") and "POST" in rule.methods:
                post_routes.append(rule.rule)
        self.assertEqual(sorted(post_routes), ["/tech/vhod", "/tech/vyhod"])

    def test_pages_cannot_change_anything(self):
        rid = self.create(self.manager())
        client, _ = self.tech_login()
        before = common.get_row(rid)
        for url in ("/tech/", f"/tech/zayavka/{rid}", "/tech/zhurnal", "/tech/log"):
            client.get(url)
        self.assertEqual(tuple(before), tuple(common.get_row(rid)))
        resp = self.post(client, f"/tech/zayavka/{rid}")
        self.assertEqual(resp.status_code, 405)

    def test_journal_lists_and_filters(self):
        c = self.manager()
        rid = self.create(c, client_name="Журнальный Клиент")
        self.post(self.brigade(1), f"/brigada/zayavka/{rid}/vzyat")
        client, _ = self.tech_login()
        html = self.text(client.get("/tech/zhurnal"))
        self.assertIn("Журнальный Клиент", html)
        self.assertIn("Создал(а) заявку", html)
        self.assertIn("Показано: <strong>2</strong> (не больше 500, свежие сверху).", html)
        html = self.text(client.get("/tech/zhurnal", query_string={"kto": "brigada1"}))
        self.assertIn("Показано: <strong>1</strong>", html)
        html = self.text(client.get("/tech/zhurnal", query_string={"deystvie": "taken", "zayavka": str(rid)}))
        self.assertIn("Показано: <strong>1</strong>", html)

    def test_request_detail(self):
        rid = self.create(self.manager(), address="Адрес для техраздела")
        client, _ = self.tech_login()
        html = self.text(client.get(f"/tech/zayavka/{rid}"))
        self.assertIn("Адрес для техраздела", html)
        self.assertIn("← Журнал этой заявки", html)
        self.assertIn("—", html)
        self.assertEqual(client.get("/tech/zayavka/9999").status_code, 404)

    def test_log_viewer(self):
        client, _ = self.tech_login()
        zapp.app.test_client().get("/nonexistent-page-for-log")
        html = self.text(client.get("/tech/log"))
        self.assertIn("ТЕХ-ВХОД успешно", html)
        self.assertIn("/nonexistent-page-for-log", html)
        html = self.text(client.get("/tech/log", query_string={"chto": "vhod"}))
        self.assertNotIn("/nonexistent-page-for-log", html)
        html = self.text(client.get("/tech/log", query_string={"chto": "warn", "soderzhit": "тех-вход"}))
        self.assertIn("ТЕХ-ВХОД", html)
        html = self.text(client.get("/tech/log", query_string={"strok": "abc"}))
        self.assertIn("Лог приложения", html)


if __name__ == "__main__":
    unittest.main()
