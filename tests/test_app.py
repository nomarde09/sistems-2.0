import os
import threading
import unittest

import common
from common import AppTestCase, PINS, get_events, get_row, valid_form, zapp


class LoginTests(AppTestCase):
    def test_login_page_has_no_staff_list(self):
        html = self.text(zapp.app.test_client().get("/vhod"))
        self.assertIn("Введите свой логин и пароль.", html)
        for name in ("Менеджер 1", "Бригада 2", "Руководитель"):
            self.assertNotIn(name, html)

    def test_anonymous_goes_to_login(self):
        resp = zapp.app.test_client().get("/")
        self.assertEqual(resp.headers["Location"], "/vhod")

    def test_each_role_lands_on_own_page(self):
        expected = {"rukovoditel": "/ofis", "manager1": "/ofis", "manager2": "/ofis",
                    "brigada1": "/brigada", "brigada2": "/brigada"}
        for uid, target in expected.items():
            with self.subTest(uid=uid):
                client = self.login(uid)
                self.assertEqual(client.get("/").headers["Location"], target)
                self.assertEqual(client.get(target).status_code, 200)

    def test_wrong_password(self):
        client = zapp.app.test_client()
        resp = client.post("/vhod", data={"login": "manager1", "pin": "0000", "csrf": self.token(client)})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Неверный логин или пароль.", self.text(resp))

    def test_unknown_login(self):
        client = zapp.app.test_client()
        resp = client.post("/vhod", data={"login": "nobody", "pin": "1234", "csrf": self.token(client)})
        self.assertIn("Неверный логин или пароль.", self.text(resp))

    def test_empty_fields(self):
        client = zapp.app.test_client()
        resp = client.post("/vhod", data={"login": "", "pin": "", "csrf": self.token(client)})
        self.assertIn("Введите логин и пароль.", self.text(resp))

    def test_login_keeps_typed_login_after_error(self):
        client = zapp.app.test_client()
        resp = client.post("/vhod", data={"login": "manager1", "pin": "0", "csrf": self.token(client)})
        self.assertIn('value="manager1"', self.text(resp))

    def test_login_is_case_insensitive_and_trimmed(self):
        client = zapp.app.test_client()
        resp = client.post("/vhod", data={"login": "  Manager1 ", "pin": PINS["manager1"],
                                          "csrf": self.token(client)})
        self.assertEqual(resp.status_code, 302)

    def test_custom_russian_login(self):
        os.environ["LOGIN_MANAGER1"] = "Айгерим"
        client = zapp.app.test_client()
        resp = client.post("/vhod", data={"login": "айгерим", "pin": PINS["manager1"],
                                          "csrf": self.token(client)})
        self.assertEqual(resp.status_code, 302)
        client2 = zapp.app.test_client()
        resp = client2.post("/vhod", data={"login": "manager1", "pin": PINS["manager1"],
                                           "csrf": self.token(client2)})
        self.assertEqual(resp.status_code, 200)

    def test_cyrillic_password_does_not_crash(self):
        client = zapp.app.test_client()
        resp = client.post("/vhod", data={"login": "manager1", "pin": "пароль", "csrf": self.token(client)})
        self.assertEqual(resp.status_code, 200)

    def test_session_is_permanent_14_days(self):
        client = zapp.app.test_client()
        resp = client.post("/vhod", data={"login": "manager1", "pin": PINS["manager1"], "csrf": self.token(client)})
        self.assertIn("Expires=", resp.headers.get("Set-Cookie", ""))
        self.assertEqual(zapp.app.config["PERMANENT_SESSION_LIFETIME"].days, 14)

    def test_logout(self):
        client = self.login("manager1")
        resp = self.post(client, "/vyhod")
        self.assertEqual(resp.headers["Location"], "/vhod")
        self.assertEqual(client.get("/ofis").headers["Location"], "/vhod")

    def test_name_from_env_is_shown_everywhere(self):
        os.environ["NAME_MANAGER1"] = "Айгерим"
        client = self.login("manager1")
        html = self.text(client.get("/ofis"))
        self.assertIn("Айгерим", html)
        self.assertIn('title="Менеджер 1"', html)


class RoleAccessTests(AppTestCase):
    def test_brigade_cannot_open_office(self):
        client = self.brigade()
        for url in ("/ofis", "/ofis/novaya", "/ofis/statistika", "/zayavka/1"):
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 302)

    def test_office_cannot_open_brigade(self):
        client = self.manager()
        for url in ("/brigada", "/brigada/moi", "/brigada/zayavka/1"):
            with self.subTest(url=url):
                self.assertEqual(client.get(url).headers["Location"], "/")

    def test_anonymous_redirected_from_everything(self):
        client = zapp.app.test_client()
        for url in ("/ofis", "/brigada", "/ofis/novaya", "/zayavka/1"):
            with self.subTest(url=url):
                self.assertEqual(client.get(url).headers["Location"], "/vhod")


class CreateRequestTests(AppTestCase):
    def test_create_and_open_card(self):
        client = self.manager()
        resp = self.post(client, "/ofis/novaya", valid_form())
        self.assertEqual(resp.status_code, 302)
        self.assertIn("created=1", resp.headers["Location"])
        html = self.text(client.get(resp.headers["Location"]))
        self.assertIn("Заявка создана. Бригады увидят её в свободных.", html)
        row = get_row(common.last_id())
        self.assertEqual(row["status"], "new")
        self.assertEqual(row["work_type"], "")
        self.assertEqual(row["warranty"], 0)
        self.assertEqual(row["created_by"], "manager1")
        self.assertEqual([e["action"] for e in get_events(row["id"])], ["created"])

    def test_required_fields(self):
        client = self.manager()
        resp = self.post(client, "/ofis/novaya", {"client_name": "", "phone": "", "address": ""})
        html = self.text(resp)
        for msg in ("Укажите имя клиента.", "Укажите телефон.", "Укажите адрес.",
                    "Выберите тип заявки.", "Выберите оборудование.", "Опишите, что нужно сделать.",
                    "Проверьте поля, отмеченные красным."):
            self.assertIn(msg, html)
        self.assertIsNone(common.last_id())

    def test_short_phone(self):
        resp = self.post(self.manager(), "/ofis/novaya", valid_form(phone="12-3"))
        self.assertIn("В телефоне слишком мало цифр.", self.text(resp))

    def test_urgent_flag(self):
        client = self.manager()
        rid = self.create(client, urgent="1")
        self.assertEqual(get_row(rid)["urgent"], 1)

    def test_warranty_always_zero_on_create(self):
        client = self.manager()
        rid = self.create(client, warranty="1")
        self.assertEqual(get_row(rid)["warranty"], 0)

    def test_boss_can_create(self):
        rid = self.create(self.boss())
        self.assertEqual(get_row(rid)["created_by"], "rukovoditel")

    def test_new_request_shows_in_free_list(self):
        rid = self.create(self.manager(), address="ул. Тестовая 5")
        html = self.text(self.brigade().get("/brigada"))
        self.assertIn("ул. Тестовая 5", html)
        self.assertIn(f"№{rid}", html)
        self.assertIn("Взять", html)

    def test_form_has_all_options(self):
        html = self.text(self.manager().get("/ofis/novaya"))
        self.assertIn("— выберите —", html)
        self.assertIn("Автоматика ворот / шлагбаум", html)
        self.assertIn("Диагностика / выезд на осмотр", html)
        self.assertIn("07:00", html)
        self.assertIn("22:00", html)
        self.assertNotIn("22:30", html)
        self.assertIn("Не знаем", html)
        self.assertNotIn('name="warranty"', html)


class BrigadeFlowTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.office = self.manager()
        self.rid = self.create(self.office)

    def test_take(self):
        b = self.brigade(1)
        resp = self.post(b, f"/brigada/zayavka/{self.rid}/vzyat")
        self.assertEqual(resp.status_code, 302)
        row = get_row(self.rid)
        self.assertEqual((row["status"], row["taken_by"]), ("in_work", "brigada1"))
        self.assertIsNotNone(row["taken_at"])
        html = self.text(b.get("/brigada/moi"))
        self.assertIn(f"№{self.rid}", html)
        self.assertNotIn(f"№{self.rid}", self.text(b.get("/brigada")))

    def test_take_message(self):
        b = self.brigade(1)
        resp = self.post(b, f"/brigada/zayavka/{self.rid}/vzyat", follow_redirects=True)
        self.assertIn(f"Заявка №{self.rid} ваша. Она теперь в «Мои заявки».", self.text(resp))

    def test_second_brigade_cannot_take(self):
        self.post(self.brigade(1), f"/brigada/zayavka/{self.rid}/vzyat")
        b2 = self.brigade(2)
        resp = self.post(b2, f"/brigada/zayavka/{self.rid}/vzyat", follow_redirects=True)
        self.assertIn("Не получилось: эту заявку только что взяла другая бригада.", self.text(resp))
        self.assertEqual(get_row(self.rid)["taken_by"], "brigada1")

    def test_other_brigade_cannot_open_taken(self):
        self.post(self.brigade(1), f"/brigada/zayavka/{self.rid}/vzyat")
        resp = self.brigade(2).get(f"/brigada/zayavka/{self.rid}", follow_redirects=True)
        self.assertIn("Эту заявку уже взяла другая бригада или её отменили.", self.text(resp))

    def test_simultaneous_take_gives_only_one(self):
        c1, c2 = self.brigade(1), self.brigade(2)
        t1, t2 = self.token(c1), self.token(c2)
        gate = threading.Barrier(2)
        results = {}

        def run(name, client, token):
            gate.wait()
            results[name] = client.post(f"/brigada/zayavka/{self.rid}/vzyat", data={"csrf": token}).status_code

        threads = [threading.Thread(target=run, args=("a", c1, t1)), threading.Thread(target=run, args=("b", c2, t2))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        taken = [e for e in get_events(self.rid) if e["action"] == "taken"]
        self.assertEqual(len(taken), 1)
        self.assertIn(get_row(self.rid)["taken_by"], ("brigada1", "brigada2"))
        self.assertEqual(get_row(self.rid)["taken_by"], taken[0]["user_id"])

    def test_return_to_free(self):
        b = self.brigade(1)
        self.post(b, f"/brigada/zayavka/{self.rid}/vzyat")
        resp = self.post(b, f"/brigada/zayavka/{self.rid}/vernut", follow_redirects=True)
        self.assertIn(f"Заявка №{self.rid} возвращена в свободные.", self.text(resp))
        row = get_row(self.rid)
        self.assertEqual(row["status"], "new")
        self.assertIsNone(row["taken_by"])
        self.assertIsNone(row["taken_at"])
        self.assertEqual([e["action"] for e in get_events(self.rid)], ["created", "taken", "returned"])
        self.assertIn(f"№{self.rid}", self.text(self.brigade(2).get("/brigada")))

    def test_cannot_return_foreign_or_new(self):
        self.post(self.brigade(1), f"/brigada/zayavka/{self.rid}/vzyat")
        resp = self.post(self.brigade(2), f"/brigada/zayavka/{self.rid}/vernut", follow_redirects=True)
        self.assertIn("Эту заявку нельзя вернуть: она не у вас в работе.", self.text(resp))
        self.assertEqual(get_row(self.rid)["taken_by"], "brigada1")

    def test_tab_counts(self):
        rid2 = self.create(self.office)
        b = self.brigade(1)
        html = self.text(b.get("/brigada"))
        self.assertIn('Свободные <span class="count">2</span>', html)
        self.post(b, f"/brigada/zayavka/{rid2}/vzyat")
        html = self.text(b.get("/brigada"))
        self.assertIn('Свободные <span class="count">1</span>', html)
        self.assertIn('Мои заявки <span class="count">1</span>', html)

    def test_empty_states(self):
        conn = common.db()
        conn.execute("DELETE FROM events")
        conn.execute("DELETE FROM requests")
        conn.commit()
        conn.close()
        b = self.brigade(1)
        self.assertIn("Свободных заявок нет.", self.text(b.get("/brigada")))
        self.assertIn("У вас нет заявок в работе.", self.text(b.get("/brigada/moi")))

    def test_card_buttons_for_taken(self):
        b = self.brigade(1)
        self.post(b, f"/brigada/zayavka/{self.rid}/vzyat")
        html = self.text(b.get(f"/brigada/zayavka/{self.rid}"))
        self.assertIn("Работа сделана — написать отчёт", html)
        self.assertIn("Вернуть в свободные", html)
        self.assertIn("Позвонить:", html)
        self.assertIn("Вы взяли эту заявку", html)


class ReportTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.rid = self.create(self.manager())
        self.b = self.brigade(1)
        self.post(self.b, f"/brigada/zayavka/{self.rid}/vzyat")
        self.url = f"/brigada/zayavka/{self.rid}/otchet"

    def test_report_saves_and_finishes(self):
        resp = self.post(self.b, self.url, {"report_work": "Поставили 4 камеры", "report_price": "85 000 тг",
                                            "report_payment": "kaspi_pay", "report_comment": "Всё ок"},
                         follow_redirects=True)
        self.assertIn(f"Отчёт сохранён. Заявка №{self.rid} выполнена.", self.text(resp))
        row = get_row(self.rid)
        self.assertEqual((row["status"], row["report_price"], row["report_payment"]), ("done", 85000, "kaspi_pay"))
        self.assertIsNotNone(row["done_at"])
        self.assertEqual(get_events(self.rid)[-1]["action"], "done")

    def test_report_required_fields(self):
        resp = self.post(self.b, self.url, {"report_work": "", "report_price": ""})
        html = self.text(resp)
        self.assertIn("Напишите, что сделали.", html)
        self.assertIn("Укажите цену цифрами.", html)
        self.assertEqual(get_row(self.rid)["status"], "in_work")

    def test_huge_price_rejected(self):
        resp = self.post(self.b, self.url, {"report_work": "x", "report_price": "100000001", "report_payment": "cash"})
        self.assertIn("Слишком большая сумма, проверьте.", self.text(resp))

    def test_cannot_report_foreign_request(self):
        b2 = self.brigade(2)
        resp = self.post(b2, self.url, {"report_work": "x", "report_price": "0"}, follow_redirects=True)
        self.assertIn("Отчёт можно написать только по своей заявке в работе.", self.text(resp))
        self.assertEqual(get_row(self.rid)["status"], "in_work")

    def test_cannot_rewrite_or_return_after_done(self):
        self.post(self.b, self.url, {"report_work": "ok", "report_price": "0"})
        resp = self.post(self.b, self.url, {"report_work": "переписали", "report_price": "5", "report_payment": "cash"},
                         follow_redirects=True)
        self.assertIn("Отчёт можно написать только по своей заявке в работе.", self.text(resp))
        self.assertEqual(get_row(self.rid)["report_work"], "ok")
        self.post(self.b, f"/brigada/zayavka/{self.rid}/vernut")
        self.assertEqual(get_row(self.rid)["status"], "done")

    def test_done_visible_in_recent(self):
        self.post(self.b, self.url, {"report_work": "ok", "report_price": "1500", "report_payment": "cash"})
        html = self.text(self.b.get("/brigada/moi"))
        self.assertIn("Выполненные недавно", html)
        self.assertIn("1 500", html)

    def test_report_page_texts(self):
        html = self.text(self.b.get(self.url))
        self.assertIn(f"Отчёт по заявке №{self.rid}", html)
        self.assertIn("Заявка выполнена", html)
        self.assertIn("Как заплатил клиент", html)
        self.assertIn("Например: установили 4 камеры, настроили запись", html)

    def test_warranty_hint_on_report(self):
        self.post(self.manager(), f"/zayavka/{self.rid}/garantiya")
        html = self.text(self.b.get(self.url))
        self.assertIn("Гарантийная заявка: клиент не платит.", html)


if __name__ == "__main__":
    unittest.main()
