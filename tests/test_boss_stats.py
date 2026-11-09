import re
import unittest

from common import AppTestCase, zapp


def tiles(html):
    out = {}
    for label, value in re.findall(r'<span class="stat-label">(.*?)</span><span class="stat-value">(.*?)</span>', html):
        out[label] = value
    return out


class BossStatsTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.office = self.manager()
        self.done_request(price=3000, payment="cash", brigade=1)
        self.done_request(price=2000, payment="kaspi_pay", brigade=2)
        self.create(self.office)

    def test_only_boss_sees_statistics(self):
        self.assertEqual(self.boss().get("/ofis/statistika").status_code, 200)
        self.assertEqual(self.manager(1).get("/ofis/statistika").status_code, 302)
        self.assertEqual(self.manager(2).get("/ofis/statistika").status_code, 302)
        self.assertEqual(self.brigade().get("/ofis/statistika").status_code, 302)

    def test_texts(self):
        html = self.text(self.boss().get("/ofis/statistika"))
        self.assertIn("Выберите период. Цифры обновляются при каждом открытии страницы.", html)
        for label in ("Сегодня", "Вчера", "Позавчера", "7 дней", "Месяц", "Всё время", "Конкретный день:", "Показать"):
            self.assertIn(label, html)
        for title in ("Бригады", "Офис", "Оплата выполненных", "По типу заявки и оборудованию",
                      "Гарантийные и платные", "Создано заявок", "Ожидание до взятия, в среднем"):
            self.assertIn(title, html)

    def test_numbers(self):
        t = tiles(self.text(self.boss().get("/ofis/statistika", query_string={"p": "today"})))
        self.assertEqual(t["Создано заявок"], "3")
        self.assertEqual(t["Взято в работу"], "2")
        self.assertEqual(t["Выполнено"], "2")
        self.assertEqual(t["Сумма выполненных"], "5 000")
        self.assertEqual(t["Сейчас новых"], "1")
        self.assertEqual(t["Сейчас в работе"], "0")

    def test_matches_tech_section(self):
        boss_html = self.text(self.boss().get("/ofis/statistika", query_string={"p": "all"}))
        client, _ = self.tech_login()
        tech_html = self.text(client.get("/tech/", query_string={"p": "all"}))
        self.assertEqual(tiles(boss_html), tiles(tech_html))
        body = lambda h: h[h.index('<nav class="status-tabs period-tabs"'):].split("<h2>Система</h2>")[0].replace("/tech/", "/ofis/statistika")
        for title in ("<h2>Бригады</h2>", "<h2>Оплата выполненных</h2>", "<h2>По месяцам</h2>"):
            self.assertIn(title, boss_html)
            self.assertIn(title, tech_html)

    def test_brigade_rows_use_names(self):
        import os
        os.environ["NAME_BRIGADA1"] = "Команда Север"
        html = self.text(self.boss().get("/ofis/statistika"))
        self.assertIn("Команда Север", html)

    def test_payment_rows(self):
        html = self.text(self.boss().get("/ofis/statistika"))
        self.assertIn("<td>Наличными</td><td>3 000</td>", html)
        self.assertIn("<td>Kaspi Pay</td><td>2 000</td>", html)
        self.assertLess(html.index("<td>Наличными</td>"), html.index("<td>Kaspi Pay</td>"))

    def test_warranty_table(self):
        html = self.text(self.boss().get("/ofis/statistika"))
        self.assertIn("<td>Платные</td>", html)
        self.assertIn("<td>Гарантийные</td>", html)

    def test_office_table_counts_creations(self):
        html = self.text(self.boss().get("/ofis/statistika"))
        m = re.search(r"<td>Менеджер 1</td><td>(\d+)</td><td>(\d+)</td><td>(\d+)</td>", html)
        self.assertEqual(m.groups(), ("3", "0", "0"))

    def test_type_equipment_table(self):
        html = self.text(self.boss().get("/ofis/statistika"))
        self.assertIn("Ремонт / неисправность · Камеры / видеонаблюдение", html)

    def test_all_time_has_months(self):
        html = self.text(self.boss().get("/ofis/statistika", query_string={"p": "all"}))
        self.assertIn("По месяцам", html)
        self.assertNotIn("<h2>По дням</h2>", html)


if __name__ == "__main__":
    unittest.main()
