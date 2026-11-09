import os
import re
import unittest
from datetime import timedelta

import common
import stats
from common import AppTestCase, insert_event, insert_request, now, zapp


def tiles(html):
    return dict(re.findall(r'<span class="stat-label">(.*?)</span><span class="stat-value">(.*?)</span>', html))


def at(days_ago, hour=12):
    d = now() - timedelta(days=days_ago)
    return d.replace(hour=hour, minute=0, second=0).isoformat()


class PeriodResolveTests(unittest.TestCase):
    def test_periods(self):
        from datetime import date
        today = date(2026, 11, 9)
        self.assertEqual(stats.resolve_period("today", "", today)[2:], (today, today))
        self.assertEqual(stats.resolve_period("yesterday", "", today)[2], date(2026, 11, 8))
        self.assertEqual(stats.resolve_period("day_before", "", today)[2], date(2026, 11, 7))
        self.assertEqual(stats.resolve_period("week", "", today)[2:], (date(2026, 11, 3), today))
        self.assertEqual(stats.resolve_period("month", "", today)[2], date(2026, 11, 1))
        self.assertEqual(stats.resolve_period("all", "", today)[2:], (None, None))

    def test_day(self):
        from datetime import date
        today = date(2026, 11, 9)
        key, caption, start, end = stats.resolve_period("day", "2026-10-05", today)
        self.assertEqual((key, caption, start, end), ("day", "05.10.2026", date(2026, 10, 5), date(2026, 10, 5)))
        self.assertEqual(stats.resolve_period("day", "2027-01-01", today)[0], "today")
        self.assertEqual(stats.resolve_period("day", "мусор", today)[0], "today")
        self.assertEqual(stats.resolve_period("bogus", "", today)[0], "today")

    def test_hours_format(self):
        self.assertEqual(stats.fmt_hours(None), "—")
        self.assertEqual(stats.fmt_hours(5.34), "5.3 ч")
        self.assertEqual(stats.fmt_hours(47.9), "47.9 ч")
        self.assertEqual(stats.fmt_hours(50.4), "2.1 дн")


class PeriodPagesTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.boss_client = self.boss()
        # заявка, созданная и выполненная сегодня
        a = insert_request(status="done", created_at=at(0, 8), taken_by="brigada1", taken_at=at(0, 9),
                           done_at=at(0, 11), report_price=1000, report_payment="cash")
        insert_event(a, "created", "manager1", at(0, 8))
        insert_event(a, "taken", "brigada1", at(0, 9))
        insert_event(a, "done", "brigada1", at(0, 11))
        # вчера создана, вчера выполнена
        b = insert_request(status="done", created_at=at(1, 8), taken_by="brigada2", taken_at=at(1, 10),
                           done_at=at(1, 13), report_price=2000, report_payment="kaspi_pay")
        insert_event(b, "created", "manager2", at(1, 8))
        insert_event(b, "taken", "brigada2", at(1, 10))
        insert_event(b, "done", "brigada2", at(1, 13))
        # три дня назад — отменена
        c = insert_request(status="cancelled", created_at=at(3, 8), cancelled_by="manager1", cancelled_at=at(3, 9))
        insert_event(c, "created", "manager1", at(3, 8))
        insert_event(c, "cancelled", "manager1", at(3, 9))
        # 40 дней назад — выполнена
        d = insert_request(status="done", created_at=at(40, 8), taken_by="brigada1", taken_at=at(40, 9),
                           done_at=at(40, 12), report_price=5000, report_payment="cash", warranty=1)
        insert_event(d, "created", "manager1", at(40, 8))
        insert_event(d, "taken", "brigada1", at(40, 9))
        insert_event(d, "done", "brigada1", at(40, 12))

    def stats_page(self, **params):
        return self.text(self.boss_client.get("/ofis/statistika", query_string=params))

    def test_today(self):
        t = tiles(self.stats_page(p="today"))
        self.assertEqual((t["Создано заявок"], t["Выполнено"], t["Сумма выполненных"]), ("1", "1", "1 000"))
        self.assertEqual(t["Ожидание до взятия, в среднем"], "1.0 ч")

    def test_default_is_today(self):
        self.assertIn("Показано: <strong>сегодня</strong>", self.stats_page())

    def test_yesterday(self):
        t = tiles(self.stats_page(p="yesterday"))
        self.assertEqual((t["Создано заявок"], t["Выполнено"], t["Сумма выполненных"]), ("1", "1", "2 000"))
        self.assertEqual(t["Ожидание до взятия, в среднем"], "2.0 ч")

    def test_day_before_is_empty(self):
        t = tiles(self.stats_page(p="day_before"))
        self.assertEqual((t["Создано заявок"], t["Выполнено"]), ("0", "0"))
        self.assertEqual(t["Ожидание до взятия, в среднем"], "—")
        self.assertIn("За этот период выполненных заявок нет.", self.stats_page(p="day_before"))
        self.assertIn("За этот период заявок нет.", self.stats_page(p="day_before"))

    def test_week_includes_cancelled(self):
        t = tiles(self.stats_page(p="week"))
        self.assertEqual((t["Создано заявок"], t["Выполнено"], t["Отменено"]), ("3", "2", "1"))
        self.assertEqual(t["Сумма выполненных"], "3 000")

    def test_all_time(self):
        t = tiles(self.stats_page(p="all"))
        self.assertEqual((t["Создано заявок"], t["Выполнено"], t["Сумма выполненных"]), ("4", "3", "8 000"))
        self.assertIn("за всё время", self.stats_page(p="all"))

    def test_specific_day(self):
        day = (now() - timedelta(days=1)).date().isoformat()
        html = self.stats_page(p="day", d=day)
        self.assertIn((now() - timedelta(days=1)).strftime("%d.%m.%Y"), html)
        self.assertEqual(tiles(html)["Выполнено"], "1")

    def test_future_day_falls_back(self):
        day = (now() + timedelta(days=5)).date().isoformat()
        self.assertIn("Показано: <strong>сегодня</strong>", self.stats_page(p="day", d=day))

    def test_by_days_table_for_week(self):
        html = self.stats_page(p="week")
        self.assertIn("<h2>По дням</h2>", html)
        self.assertEqual(len(re.findall(r'p=day&amp;d=\d{4}-\d{2}-\d{2}">\d{2}\.\d{2}</a>', html)), 7)
        self.assertIn('class="muted-row"', html)
        self.assertRegex(html, r'<span class="muted">(пн|вт|ср|чт|пт|сб|вс)</span>')

    def test_no_days_table_for_single_day_or_all(self):
        self.assertNotIn("<h2>По дням</h2>", self.stats_page(p="today"))
        self.assertNotIn("<h2>По дням</h2>", self.stats_page(p="all"))

    def test_months_table_for_all(self):
        html = self.stats_page(p="all")
        self.assertIn("<h2>По месяцам</h2>", html)
        self.assertIn(zapp.month_name_filter(f"{now().year}-{now().month:02d}"), html)

    def test_range_caption(self):
        html = self.stats_page(p="week")
        self.assertRegex(html, r"\(\d{2}\.\d{2} — \d{2}\.\d{2}\.\d{4}\)")
        self.assertNotRegex(self.stats_page(p="today"), r"— \d{2}\.\d{2}\.\d{4}\)")

    def test_brigade_table_per_period(self):
        html = self.stats_page(p="all")
        m = re.search(r"<td>Бригада 1</td><td>(\d+)</td><td>(\d+)</td><td>([\d ]+)</td><td>(.*?)</td>", html)
        self.assertEqual(m.groups()[:3], ("2", "2", "6 000"))
        self.assertEqual(m.group(4), "2.5 ч")

    def test_warranty_table_values(self):
        html = self.stats_page(p="all")
        self.assertRegex(html, r"<td>Гарантийные</td><td>1</td><td>1</td><td>5 000</td>")

    def test_legacy_requests_label(self):
        insert_request(request_type=None, equipment=None, work_type="Камеры")
        self.assertIn("не указано (старые заявки)", self.stats_page(p="today"))

    def test_names_from_env_in_stats(self):
        os.environ["NAME_MANAGER1"] = "Айгерим"
        os.environ["NAME_BRIGADA2"] = "Команда Юг"
        html = self.stats_page(p="all")
        self.assertIn("<td>Айгерим</td>", html)
        self.assertIn("<td>Команда Юг</td>", html)

    def test_current_counts_always_current(self):
        insert_request(status="new")
        insert_request(status="in_work", taken_by="brigada1")
        t = tiles(self.stats_page(p="day_before"))
        self.assertEqual((t["Сейчас новых"], t["Сейчас в работе"]), ("1", "1"))

    def test_return_counted(self):
        r = insert_request(status="new")
        insert_event(r, "returned", "brigada1")
        html = self.stats_page(p="today")
        m = re.search(r"<td>Бригада 1</td><td>\d+</td><td>\d+</td><td>[\d ]+</td><td>.*?</td><td>(\d+)</td>", html)
        self.assertEqual(m.group(1), "1")


if __name__ == "__main__":
    unittest.main()
