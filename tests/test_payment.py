import unittest

from common import AppTestCase, get_row, zapp


class PaymentTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.office = self.manager()
        self.b = self.brigade(1)

    def taken(self, **over):
        rid = self.create(self.office, **over)
        self.post(self.b, f"/brigada/zayavka/{rid}/vzyat")
        return rid, f"/brigada/zayavka/{rid}/otchet"

    def test_each_payment_method_saved(self):
        for key in ("kaspi_transfer", "kaspi_pay", "cash"):
            with self.subTest(key=key):
                rid, url = self.taken()
                self.post(self.b, url, {"report_work": "x", "report_price": "500", "report_payment": key})
                self.assertEqual(get_row(rid)["report_payment"], key)

    def test_payment_required_when_price_positive(self):
        rid, url = self.taken()
        resp = self.post(self.b, url, {"report_work": "x", "report_price": "500"})
        self.assertIn("Выберите, как заплатил клиент.", self.text(resp))
        self.assertEqual(get_row(rid)["status"], "in_work")

    def test_unknown_payment_rejected(self):
        rid, url = self.taken()
        resp = self.post(self.b, url, {"report_work": "x", "report_price": "500", "report_payment": "bitcoin"})
        self.assertIn("Выберите, как заплатил клиент.", self.text(resp))

    def test_zero_price_needs_no_payment_and_stores_null(self):
        rid, url = self.taken()
        self.post(self.b, url, {"report_work": "x", "report_price": "0", "report_payment": "cash"})
        row = get_row(rid)
        self.assertEqual((row["status"], row["report_price"], row["report_payment"]), ("done", 0, None))

    def test_price_takes_only_digits(self):
        rid, url = self.taken()
        self.post(self.b, url, {"report_work": "x", "report_price": "85 000 тг", "report_payment": "cash"})
        self.assertEqual(get_row(rid)["report_price"], 85000)

    def test_price_without_digits(self):
        rid, url = self.taken()
        resp = self.post(self.b, url, {"report_work": "x", "report_price": "много", "report_payment": "cash"})
        self.assertIn("Укажите цену цифрами.", self.text(resp))

    def test_payment_choice_remembered_on_error(self):
        rid, url = self.taken()
        resp = self.post(self.b, url, {"report_work": "", "report_price": "10", "report_payment": "kaspi_pay"})
        self.assertRegex(self.text(resp), r'value="kaspi_pay" checked')

    def test_report_shows_payment_or_not_specified(self):
        rid = self.done_request(price=700, payment="kaspi_transfer")
        self.assertIn("Перевод на Kaspi", self.text(self.office.get(f"/zayavka/{rid}")))
        free = self.done_request(price=0)
        self.assertIn("не указана", self.text(self.office.get(f"/zayavka/{free}")))

    def test_comment_optional_and_shown(self):
        rid = self.done_request()
        html = self.text(self.office.get(f"/zayavka/{rid}"))
        self.assertNotIn("Комментарий", html)

    def test_urgent_on_top_of_brigade_list(self):
        normal = self.create(self.office, address="Обычный адрес")
        urgent = self.create(self.office, address="Срочный адрес", urgent="1")
        html = self.text(self.b.get("/brigada"))
        self.assertLess(html.index("Срочный адрес"), html.index("Обычный адрес"))
        self.assertIn("Срочно", html)

    def test_urgent_on_top_of_office_list(self):
        self.create(self.office, address="Обычный адрес")
        self.create(self.office, address="Срочный адрес", urgent="1")
        html = self.text(self.office.get("/ofis"))
        self.assertLess(html.index("Срочный адрес"), html.index("Обычный адрес"))

    def test_brigade_order_by_agreed_date(self):
        n = zapp.now_local()
        self.create(self.office, address="Без даты")
        self.create(self.office, address="Поздно", agreed_day="28", agreed_month=f"{n.year}-{n.month:02d}")
        self.create(self.office, address="Рано", agreed_day="2", agreed_month=f"{n.year}-{n.month:02d}")
        html = self.text(self.b.get("/brigada"))
        self.assertLess(html.index("Рано"), html.index("Поздно"))
        self.assertLess(html.index("Поздно"), html.index("Без даты"))

    def test_office_summary_with_payments(self):
        self.done_request(price=1000, payment="cash")
        self.done_request(price=2500, payment="kaspi_pay")
        self.done_request(price=0)
        html = self.text(self.office.get("/ofis", query_string={"status": "done"}))
        self.assertIn("Выполнено: <strong>3</strong> на сумму <strong>3 500</strong>.", html)
        self.assertIn("Наличными: <strong>1 000</strong>", html)
        self.assertIn("Kaspi Pay: <strong>2 500</strong>", html)
        self.assertIn("не указана: <strong>0</strong>", html)

    def test_filter_by_payment(self):
        a = self.done_request(price=1000, payment="cash")
        b = self.done_request(price=2000, payment="kaspi_pay")
        html = self.text(self.office.get("/ofis", query_string={"oplata": "cash"}))
        self.assertIn(f"№{a}</span>", html)
        self.assertNotIn(f"№{b}</span>", html)


if __name__ == "__main__":
    unittest.main()
