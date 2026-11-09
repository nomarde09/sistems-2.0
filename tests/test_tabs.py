import re
import unittest

from common import AppTestCase, get_row, zapp


class TabsTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.c = self.manager()
        self.new1 = self.create(self.c, address="Новая-1")
        self.new2 = self.create(self.c, address="Новая-2", request_type="to")
        self.work = self.create(self.c, address="Работа-1")
        self.post(self.brigade(1), f"/brigada/zayavka/{self.work}/vzyat")
        self.done = self.done_request(address="Готово-1", price=5000, payment="cash")
        self.canc = self.create(self.c, address="Отмена-1")
        self.post(self.c, f"/zayavka/{self.canc}/otmenit")

    def counts(self, **params):
        html = self.text(self.c.get("/ofis", query_string=params))
        out = {}
        for key, label in (("new", "Новые"), ("in_work", "В работе"), ("done", "Выполнены"),
                           ("cancelled", "Отменены"), ("all", "Все")):
            m = re.search(rf'{label} <span class="count">(\d+)</span>', html)
            out[key] = int(m.group(1))
        return out, html

    def test_counts(self):
        counts, _ = self.counts()
        self.assertEqual(counts, {"new": 2, "in_work": 1, "done": 1, "cancelled": 1, "all": 5})

    def test_default_tab_is_new(self):
        _, html = self.counts()
        self.assertIn("Новая-1", html)
        self.assertNotIn("Работа-1", html)
        self.assertRegex(html, r'class="status-tab tab-new active"')

    def test_each_tab_shows_only_its_status(self):
        for status, shown, hidden in (("in_work", "Работа-1", "Новая-1"), ("done", "Готово-1", "Работа-1"),
                                      ("cancelled", "Отмена-1", "Готово-1")):
            with self.subTest(status=status):
                _, html = self.counts(status=status)
                self.assertIn(shown, html)
                self.assertNotIn(hidden, html)

    def test_all_tab(self):
        _, html = self.counts(status="all")
        for name in ("Новая-1", "Работа-1", "Готово-1", "Отмена-1"):
            self.assertIn(name, html)

    def test_counts_follow_filters(self):
        counts, _ = self.counts(tip="to")
        self.assertEqual(counts["new"], 1)
        self.assertEqual(counts["all"], 1)

    def test_counts_ignore_current_tab(self):
        counts, _ = self.counts(status="done")
        self.assertEqual(counts["new"], 2)

    def test_tab_links_keep_filters(self):
        _, html = self.counts(tip="remont", q="Новая")
        self.assertRegex(html, r'href="/ofis\?status=done&amp;q=%D0%9D%D0%BE%D0%B2%D0%B0%D1%8F&amp;tip=remont"')

    def test_default_with_search_is_all(self):
        _, html = self.counts(q="Отмена")
        self.assertRegex(html, r'class="status-tab tab-all active"')

    def test_default_with_period_or_payment_is_done(self):
        _, html = self.counts(period="today")
        self.assertRegex(html, r'class="status-tab tab-done active"')
        _, html = self.counts(oplata="cash")
        self.assertRegex(html, r'class="status-tab tab-done active"')

    def test_empty_tab_points_to_others(self):
        conn_row = self.done
        self.post(self.c, f"/zayavka/{self.new1}/otmenit")
        self.post(self.c, f"/zayavka/{self.new2}/otmenit")
        html = self.text(self.c.get("/ofis"))
        self.assertIn("В этой вкладке заявок нет.", html)
        self.assertIn("Есть в других:", html)
        self.assertIn("Выполнены (1)", html)

    def test_filtered_empty_tab(self):
        html = self.text(self.c.get("/ofis", query_string={"q": "Готово", "status": "new"}))
        self.assertIn("Здесь ничего не нашлось.", html)
        self.assertIn("Выполнены (1)", html)

    def test_filter_form_state(self):
        _, html = self.counts(tip="to", garantiya="net")
        self.assertIn("Фильтры (2)", html)
        self.assertIn("<details class=\"more-filters\" open>", html)
        self.assertIn("Сбросить", html)
        _, html = self.counts()
        self.assertNotIn("Сбросить", html)
        self.assertNotIn("Фильтры (", html)

    def test_hidden_status_field_only_when_not_new(self):
        _, html = self.counts()
        self.assertNotIn('type="hidden" name="status"', html)
        _, html = self.counts(status="done")
        self.assertIn('type="hidden" name="status" value="done"', html)

    def test_filter_by_brigade_and_author(self):
        _, html = self.counts(brigada="brigada1", status="all")
        self.assertIn("Работа-1", html)
        self.assertNotIn("Новая-1", html)
        _, html = self.counts(avtor="manager2", status="all")
        self.assertNotIn("Новая-1", html)

    def test_filter_by_warranty(self):
        self.post(self.c, f"/zayavka/{self.new1}/garantiya")
        _, html = self.counts(garantiya="da")
        self.assertIn("Новая-1", html)
        self.assertNotIn("Новая-2", html)
        _, html = self.counts(garantiya="net")
        self.assertNotIn("Новая-1", html)
        self.assertIn("Новая-2", html)

    def test_filter_by_period_today(self):
        _, html = self.counts(period="today")
        self.assertIn("Готово-1", html)
        _, html = self.counts(period="yesterday")
        self.assertNotIn("Готово-1", html)

    def test_filter_by_type_and_equipment(self):
        _, html = self.counts(tip="to", status="all")
        self.assertIn("Новая-2", html)
        self.assertNotIn("Новая-1", html)
        _, html = self.counts(oborud="domofon", status="all")
        self.assertIn("Ничего не нашлось.", html)

    def test_invalid_filter_values_ignored(self):
        counts, _ = self.counts(tip="bogus", brigada="hacker", period="x")
        self.assertEqual(counts["new"], 2)

    def test_summary_found(self):
        _, html = self.counts(q="Новая")
        self.assertIn("Найдено: <strong>2</strong>.", html)

    def test_boss_tabs_in_header(self):
        boss = self.boss()
        html = self.text(boss.get("/ofis"))
        self.assertIn(">Заявки</a>", html)
        self.assertIn(">Статистика</a>", html)
        self.assertNotIn(">Статистика</a>", self.text(self.c.get("/ofis")))


if __name__ == "__main__":
    unittest.main()
