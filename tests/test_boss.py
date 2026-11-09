import unittest

import common
from common import AppTestCase, get_events, get_row, valid_form, zapp


class EditTests(AppTestCase):
    def test_edit_by_boss_and_each_manager(self):
        for who_login in ("rukovoditel", "manager1", "manager2"):
            with self.subTest(who=who_login):
                creator = self.manager(1)
                rid = self.create(creator)
                editor = self.login(who_login)
                resp = self.post(editor, f"/zayavka/{rid}/ispravit",
                                 valid_form(phone="+7 777 000-11-22", description="Новое описание"),
                                 follow_redirects=True)
                self.assertIn("Заявка исправлена.", self.text(resp))
                row = get_row(rid)
                self.assertEqual(row["phone"], "+7 777 000-11-22")
                self.assertIsNotNone(row["updated_at"])
                ev = get_events(rid)[-1]
                self.assertEqual((ev["action"], ev["user_id"]), ("edited", who_login))
                self.assertEqual(ev["details"], "исправлено: телефон, описание")

    def test_edit_nothing_changed(self):
        client = self.manager()
        rid = self.create(client)
        resp = self.post(client, f"/zayavka/{rid}/ispravit", valid_form(), follow_redirects=True)
        self.assertIn("Ничего не изменилось.", self.text(resp))
        self.assertEqual(len(get_events(rid)), 1)

    def test_edit_form_prefilled(self):
        client = self.manager()
        rid = self.create(client, client_name="Сергей К.")
        html = self.text(client.get(f"/zayavka/{rid}/ispravit"))
        self.assertIn(f"Исправить заявку №{rid}", html)
        self.assertIn("Сергей К.", html)
        self.assertIn("Сохранить исправления", html)
        self.assertIn('name="warranty"', html)

    def test_edit_validation(self):
        client = self.manager()
        rid = self.create(client)
        resp = self.post(client, f"/zayavka/{rid}/ispravit", valid_form(client_name=""))
        self.assertIn("Укажите имя клиента.", self.text(resp))
        self.assertEqual(get_row(rid)["client_name"], "Иван Петров")

    def test_cannot_edit_cancelled(self):
        client = self.manager()
        rid = self.create(client)
        self.post(client, f"/zayavka/{rid}/otmenit")
        resp = self.post(client, f"/zayavka/{rid}/ispravit", valid_form(phone="+7 700 999-99-99"), follow_redirects=True)
        self.assertIn("Отменённую заявку исправить нельзя.", self.text(resp))
        self.assertNotEqual(get_row(rid)["phone"], "+7 700 999-99-99")

    def test_edit_done_request_with_report(self):
        rid = self.done_request(price=1000, payment="cash")
        client = self.manager(2)
        html = self.text(client.get(f"/zayavka/{rid}/ispravit"))
        self.assertIn("Отчёт бригады (Бригада 1)", html)
        data = valid_form(report_work="Сделали иначе", report_price="2000", report_payment="kaspi_pay",
                          report_comment="коммент")
        self.post(client, f"/zayavka/{rid}/ispravit", data)
        row = get_row(rid)
        self.assertEqual((row["report_work"], row["report_price"], row["report_payment"]),
                         ("Сделали иначе", 2000, "kaspi_pay"))
        self.assertEqual(row["status"], "done")
        self.assertEqual(get_events(rid)[-1]["details"],
                         "исправлено: что сделано, цена, оплата, комментарий")

    def test_edit_done_report_validation(self):
        rid = self.done_request()
        client = self.boss()
        resp = self.post(client, f"/zayavka/{rid}/ispravit", valid_form(report_work="", report_price="500"))
        html = self.text(resp)
        self.assertIn("Напишите, что сделали.", html)
        self.assertIn("Выберите, как заплатил клиент.", html)

    def test_edit_in_work_keeps_brigade(self):
        client = self.manager()
        rid = self.create(client)
        self.post(self.brigade(1), f"/brigada/zayavka/{rid}/vzyat")
        self.post(client, f"/zayavka/{rid}/ispravit", valid_form(address="Другой адрес"))
        row = get_row(rid)
        self.assertEqual((row["status"], row["taken_by"], row["address"]), ("in_work", "brigada1", "Другой адрес"))


class CancelTests(AppTestCase):
    def test_cancel_by_boss_and_each_manager(self):
        for who_login in ("rukovoditel", "manager1", "manager2"):
            with self.subTest(who=who_login):
                rid = self.create(self.manager(1))
                client = self.login(who_login)
                resp = self.post(client, f"/zayavka/{rid}/otmenit", {"reason": "Клиент передумал"},
                                 follow_redirects=True)
                self.assertIn(f"Заявка №{rid} отменена.", self.text(resp))
                row = get_row(rid)
                self.assertEqual((row["status"], row["cancelled_by"], row["cancel_reason"]),
                                 ("cancelled", who_login, "Клиент передумал"))
                ev = get_events(rid)[-1]
                self.assertEqual((ev["action"], ev["user_id"], ev["details"]), ("cancelled", who_login, "Клиент передумал"))

    def test_cancel_in_work_removes_from_brigade(self):
        client = self.manager()
        rid = self.create(client)
        b = self.brigade(1)
        self.post(b, f"/brigada/zayavka/{rid}/vzyat")
        html = self.text(client.get(f"/zayavka/{rid}"))
        self.assertIn("Заявка сейчас у бригады «Бригада 1».", html)
        self.post(client, f"/zayavka/{rid}/otmenit")
        self.assertIn("У вас нет заявок в работе.", self.text(b.get("/brigada/moi")))

    def test_cannot_cancel_twice_or_done(self):
        client = self.manager()
        rid = self.create(client)
        self.post(client, f"/zayavka/{rid}/otmenit")
        resp = self.post(client, f"/zayavka/{rid}/otmenit", follow_redirects=True)
        self.assertIn("Эту заявку уже нельзя отменить.", self.text(resp))
        done = self.done_request()
        self.post(client, f"/zayavka/{done}/otmenit")
        self.assertEqual(get_row(done)["status"], "done")

    def test_cancelled_card(self):
        client = self.manager()
        rid = self.create(client)
        self.post(client, f"/zayavka/{rid}/otmenit", {"reason": "дубль"})
        html = self.text(client.get(f"/zayavka/{rid}"))
        self.assertIn("Заявка отменена", html)
        self.assertIn("Причина: дубль", html)
        self.assertNotIn("Отменить заявку</summary>", html)
        self.assertNotIn(">Исправить<", html)

    def test_cancel_without_reason(self):
        client = self.manager()
        rid = self.create(client)
        self.post(client, f"/zayavka/{rid}/otmenit")
        self.assertIsNone(get_row(rid)["cancel_reason"])
        self.assertNotIn("Причина:", self.text(client.get(f"/zayavka/{rid}")))

    def test_history_shown_on_card(self):
        client = self.manager()
        rid = self.create(client)
        self.post(self.brigade(1), f"/brigada/zayavka/{rid}/vzyat")
        html = self.text(client.get(f"/zayavka/{rid}"))
        self.assertIn("История", html)
        self.assertIn("создал(а) заявку", html)
        self.assertIn("взял(а) в работу", html)


class SearchTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.c = self.manager()
        self.a = self.create(self.c, client_name="Алексей Смирнов", phone="+7 (701) 555-12-34",
                             address="пр. Достык 5", description="Установить шлагбаум")
        self.b = self.create(self.c, client_name="Борис Иванов", phone="8 702 111-22-33",
                             address="ул. Сатпаева 7", description="Починить домофон",
                             request_type="to", equipment="domofon")

    def found(self, q):
        html = self.text(self.c.get("/ofis", query_string={"q": q}))
        return {i for i in (self.a, self.b) if f"№{i}</span>" in html}

    def test_by_name_case_insensitive_cyrillic(self):
        self.assertEqual(self.found("АЛЕКСЕЙ"), {self.a})
        self.assertEqual(self.found("иванов"), {self.b})

    def test_by_address_and_description(self):
        self.assertEqual(self.found("достык"), {self.a})
        self.assertEqual(self.found("домофон"), {self.b})

    def test_by_number(self):
        self.assertEqual(self.found(str(self.a)), {self.a})
        self.assertEqual(self.found(f"№{self.b}"), {self.b})

    def test_by_phone_digits(self):
        self.assertEqual(self.found("555-12"), {self.a})
        self.assertEqual(self.found("701 555 1234"), {self.a})
        self.assertEqual(self.found("702 111"), {self.b})

    def test_phone_without_leading_digit(self):
        self.assertEqual(self.found("87021112233"), {self.b})
        self.assertEqual(self.found("77015551234"), {self.a})

    def test_by_type_and_equipment_label(self):
        self.assertEqual(self.found("плановое то"), {self.b})
        self.assertEqual(self.found("Домофон"), {self.b})

    def test_percent_and_underscore_are_literal(self):
        self.assertEqual(self.found("%"), set())
        self.assertEqual(self.found("_"), set())

    def test_search_defaults_to_all_tab(self):
        self.post(self.c, f"/zayavka/{self.a}/otmenit")
        html = self.text(self.c.get("/ofis", query_string={"q": "Алексей"}))
        self.assertIn(f"№{self.a}</span>", html)

    def test_nothing_found(self):
        html = self.text(self.c.get("/ofis", query_string={"q": "zzzz"}))
        self.assertIn("Ничего не нашлось.", html)
        self.assertIn("Найдено: <strong>0</strong>.", html)


class WholePathTests(AppTestCase):
    def test_full_life_of_request(self):
        office = self.manager()
        rid = self.create(office, urgent="1")
        b1, b2 = self.brigade(1), self.brigade(2)
        self.post(b1, f"/brigada/zayavka/{rid}/vzyat")
        self.post(b1, f"/brigada/zayavka/{rid}/vernut")
        self.post(b2, f"/brigada/zayavka/{rid}/vzyat")
        self.post(b2, f"/brigada/zayavka/{rid}/otchet",
                  {"report_work": "ок", "report_price": "10000", "report_payment": "cash"})
        self.post(office, f"/zayavka/{rid}/garantiya")
        actions = [e["action"] for e in get_events(rid)]
        self.assertEqual(actions, ["created", "taken", "returned", "taken", "done", "warranty"])
        row = get_row(rid)
        self.assertEqual((row["status"], row["taken_by"], row["warranty"]), ("done", "brigada2", 1))
        html = self.text(office.get(f"/zayavka/{rid}"))
        self.assertIn("Отчёт", html)
        self.assertIn("10 000", html)
        self.assertIn("Наличными", html)
        self.assertIn("Заявка <strong>гарантийная</strong>.", html)

    def test_card_for_missing_request(self):
        self.assertEqual(self.manager().get("/zayavka/9999").status_code, 404)

    def test_card_after_status_back_link(self):
        client = self.manager()
        rid = self.create(client)
        self.assertIn("← Новые", self.text(client.get(f"/zayavka/{rid}")))


if __name__ == "__main__":
    unittest.main()
