import unittest
from datetime import timedelta

import common
from common import AppTestCase, get_events, get_row, insert_request, now, valid_form, zapp


class FieldsTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.c = self.manager()

    def test_type_and_equipment_saved(self):
        rid = self.create(self.c, request_type="montazh", equipment="vorota")
        row = get_row(rid)
        self.assertEqual((row["request_type"], row["equipment"]), ("montazh", "vorota"))
        html = self.text(self.c.get(f"/zayavka/{rid}"))
        self.assertIn("Монтаж (новый объект)", html)
        self.assertIn("Автоматика ворот / шлагбаум", html)

    def test_all_types_and_equipment_accepted(self):
        for t in zapp.REQUEST_TYPES:
            for e in zapp.EQUIPMENT:
                rid = self.create(self.c, request_type=t, equipment=e)
                self.assertEqual((get_row(rid)["request_type"], get_row(rid)["equipment"]), (t, e))
            break

    def test_unknown_type_rejected(self):
        resp = self.post(self.c, "/ofis/novaya", valid_form(request_type="hack"))
        self.assertIn("Выберите тип заявки.", self.text(resp))
        resp = self.post(self.c, "/ofis/novaya", valid_form(equipment="hack"))
        self.assertIn("Выберите оборудование.", self.text(resp))

    def test_type_line_in_lists(self):
        rid = self.create(self.c, request_type="remont", equipment="domofon")
        self.assertIn("Ремонт / неисправность · Домофон", self.text(self.c.get("/ofis")))
        self.assertIn("Ремонт / неисправность · Домофон", self.text(self.brigade().get("/brigada")))

    def test_legacy_request_displays(self):
        rid = insert_request(request_type=None, equipment=None, work_type="Камеры и ворота")
        common.insert_event(rid, "created", "manager1")
        html = self.text(self.c.get(f"/zayavka/{rid}"))
        self.assertIn("Что монтируем", html)
        self.assertIn("Камеры и ворота", html)
        self.assertIn("не указан", html)
        self.assertIn("не указано", html)
        self.assertIn("Камеры и ворота", self.text(self.c.get("/ofis")))
        edit = self.text(self.c.get(f"/zayavka/{rid}/ispravit"))
        self.assertIn("Раньше в заявке было записано «Что монтируем»: Камеры и ворота", edit)

    def test_legacy_type_line_without_anything(self):
        rid = insert_request(request_type=None, equipment=None, work_type="")
        self.assertIn("тип не указан · оборудование не указано", self.text(self.c.get("/ofis")))

    def test_agreed_date_and_time(self):
        n = now()
        rid = self.create(self.c, agreed_day="15", agreed_month=f"{n.year}-{n.month:02d}", agreed_time="14:30")
        row = get_row(rid)
        self.assertEqual(row["agreed_date"], f"{n.year}-{n.month:02d}-15")
        self.assertEqual(row["agreed_time"], "14:30")
        self.assertIn("14:30", self.text(self.c.get(f"/zayavka/{rid}")))
        self.assertIn(", 14:30", self.text(self.brigade().get("/brigada")))

    def test_no_date_means_none(self):
        rid = self.create(self.c)
        self.assertEqual((get_row(rid)["agreed_date"], get_row(rid)["agreed_time"]), (None, None))
        self.assertIn("день не назначен", self.text(self.brigade().get("/brigada")))
        self.assertIn("не указано", self.text(self.c.get(f"/zayavka/{rid}")))

    def test_date_errors(self):
        n = now()
        month = f"{n.year}-02"
        resp = self.post(self.c, "/ofis/novaya", valid_form(agreed_day="30", agreed_month=month))
        self.assertIn("В месяце «февраль» нет 30-го числа.", self.text(resp))
        resp = self.post(self.c, "/ofis/novaya", valid_form(agreed_day="abc"))
        self.assertIn("Выберите день и месяц.", self.text(resp))
        resp = self.post(self.c, "/ofis/novaya", valid_form(agreed_day="5", agreed_month="мусор"))
        self.assertIn("Выберите день и месяц.", self.text(resp))
        resp = self.post(self.c, "/ofis/novaya", valid_form(agreed_time="25:00"))
        self.assertIn("Выберите время из списка.", self.text(resp))
        resp = self.post(self.c, "/ofis/novaya", valid_form(agreed_time="10:00"))
        self.assertIn("Время указано, а день — нет. Выберите и день.", self.text(resp))
        self.assertIsNone(common.last_id())

    def test_time_options_every_half_hour(self):
        self.assertEqual(zapp.AGREED_TIMES[0], "07:00")
        self.assertEqual(zapp.AGREED_TIMES[1], "07:30")
        self.assertEqual(zapp.AGREED_TIMES[-1], "22:00")
        self.assertEqual(len(zapp.AGREED_TIMES), 31)

    def test_month_options(self):
        options = zapp.month_options()
        self.assertEqual(len(options), 12)
        n = now()
        self.assertEqual(options[0][0], f"{n.year}-{n.month:02d}")
        self.assertEqual(options[0][1], zapp.MONTHS_NOM[n.month - 1])
        self.assertTrue(any(label.endswith(str(n.year + 1)) for _, label in options) or n.month == 1)

    def test_old_month_added_when_editing(self):
        past = (now() - timedelta(days=100)).date()
        rid = insert_request(agreed_date=past.isoformat())
        common.insert_event(rid, "created", "manager1")
        html = self.text(self.c.get(f"/zayavka/{rid}/ispravit"))
        self.assertIn(f'value="{past.year}-{past.month:02d}" selected', html)

    def test_warranty_toggle(self):
        rid = self.create(self.c)
        html = self.text(self.c.get(f"/zayavka/{rid}"))
        self.assertIn("Заявка <strong>платная</strong>.", html)
        self.assertIn("Сделать гарантийной", html)
        resp = self.post(self.c, f"/zayavka/{rid}/garantiya", follow_redirects=True)
        html = self.text(resp)
        self.assertIn("Заявка отмечена как гарантийная.", html)
        self.assertIn("Сделать платной", html)
        self.assertEqual(get_row(rid)["warranty"], 1)
        self.assertEqual(get_events(rid)[-1]["details"], "гарантийная")
        self.assertIn("Гарантия", self.text(self.c.get("/ofis")))
        resp = self.post(self.c, f"/zayavka/{rid}/garantiya", follow_redirects=True)
        self.assertIn("Заявка отмечена как платная.", self.text(resp))
        self.assertEqual(get_events(rid)[-1]["details"], "платная")

    def test_warranty_in_edit_form(self):
        rid = self.create(self.c)
        self.post(self.c, f"/zayavka/{rid}/ispravit", valid_form(warranty="1"))
        self.assertEqual(get_row(rid)["warranty"], 1)
        self.assertIn("гарантия", get_events(rid)[-1]["details"])

    def test_warranty_not_for_cancelled(self):
        rid = self.create(self.c)
        self.post(self.c, f"/zayavka/{rid}/otmenit")
        resp = self.post(self.c, f"/zayavka/{rid}/garantiya", follow_redirects=True)
        self.assertIn("У отменённой заявки гарантию не меняют.", self.text(resp))
        self.assertEqual(get_row(rid)["warranty"], 0)

    def test_warranty_shown_to_brigade(self):
        rid = self.create(self.c)
        self.post(self.c, f"/zayavka/{rid}/garantiya")
        b = self.brigade()
        self.assertIn("да — бесплатно для клиента", self.text(b.get(f"/brigada/zayavka/{rid}")))
        self.assertIn("Гарантия", self.text(b.get("/brigada")))

    def test_payable_card_text(self):
        rid = self.create(self.c)
        self.assertIn("нет, платная", self.text(self.brigade().get(f"/brigada/zayavka/{rid}")))

    def test_description_multiline_kept(self):
        rid = self.create(self.c, description="Первая строка\nВторая строка")
        self.assertIn("Первая строка\nВторая строка", get_row(rid)["description"])
        self.assertIn('class="multiline"', self.text(self.c.get(f"/zayavka/{rid}")))


class FilterHelperTests(unittest.TestCase):
    def test_filters(self):
        zapp_ctx = zapp
        with zapp_ctx.app.app_context():
            self.assertEqual(zapp_ctx.money_filter(15000), "15 000")
            self.assertEqual(zapp_ctx.money_filter(0), "0")
            self.assertEqual(zapp_ctx.money_filter(1234567), "1 234 567")
            self.assertEqual(zapp_ctx.tel_filter("+7 (701) 123-45-67"), "+77011234567")
            self.assertEqual(zapp_ctx.tel_filter("8 701 123"), "8701123")
            self.assertEqual(zapp_ctx.month_name_filter("2026-11"), "Ноябрь 2026")
            self.assertEqual(zapp_ctx.dt_filter("2026-11-09T14:05:09"), "09.11.2026 14:05")
            year = now().year
            self.assertEqual(zapp_ctx.d_filter(f"{year}-11-12"), "12 ноября")
            self.assertEqual(zapp_ctx.d_filter(f"{year + 1}-01-03"), f"3 января {year + 1}")


if __name__ == "__main__":
    unittest.main()
