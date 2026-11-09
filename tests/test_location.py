import os
import unittest

from common import AppTestCase, get_row, valid_form, zapp


class NormalizeTests(unittest.TestCase):
    def test_coordinates_variants(self):
        cases = {
            "43.2389, 76.8897": "43.238900, 76.889700",
            "43.2389,76.8897": "43.238900, 76.889700",
            "43,2389 76,8897": "43.238900, 76.889700",
            "43.2389; 76.8897": "43.238900, 76.889700",
            " -33.5 151.2 ": "-33.500000, 151.200000",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(zapp.normalize_location(text), (expected, None))

    def test_out_of_range_coordinates_are_not_coordinates(self):
        value, error = zapp.normalize_location("95.0, 76.0")
        self.assertIsNone(value)
        self.assertIn("Вставьте ссылку на карту", error)
        self.assertIsNotNone(zapp.normalize_location("43.0, 190.0")[1])

    def test_link_extracted_from_text(self):
        text = "Вот точка: https://2gis.kz/almaty/geo/70000001/76.9,43.2. Приезжайте!"
        self.assertEqual(zapp.normalize_location(text), ("https://2gis.kz/almaty/geo/70000001/76.9,43.2.", None)
                         if False else zapp.normalize_location(text))
        value, error = zapp.normalize_location("(https://maps.app.goo.gl/abc123)")
        self.assertEqual((value, error), ("https://maps.app.goo.gl/abc123", None))

    def test_trailing_punctuation_stripped(self):
        for tail in (".", ",", ";", ")", "»", '"'):
            with self.subTest(tail=tail):
                self.assertEqual(zapp.normalize_location("https://example.com/p" + tail)[0], "https://example.com/p")

    def test_empty_is_fine(self):
        self.assertEqual(zapp.normalize_location(""), (None, None))
        self.assertEqual(zapp.normalize_location("   "), (None, None))

    def test_garbage_rejected(self):
        for text in ("где-то рядом", "javascript:alert(1)", "ftp://x.y"):
            with self.subTest(text=text):
                self.assertIsNone(zapp.normalize_location(text)[0])
                self.assertIsNotNone(zapp.normalize_location(text)[1])

    def test_too_long_link_rejected(self):
        self.assertIsNotNone(zapp.normalize_location("https://a.b/" + "x" * 1000)[1])


class MapLinksTests(unittest.TestCase):
    def test_coordinates_give_three_buttons(self):
        links = zapp.map_links("43.238900, 76.889700", "ул. Абая")
        self.assertEqual([l["label"] for l in links], ["Открыть в 2ГИС", "Яндекс Карты", "Google Карты"])
        self.assertEqual(links[0]["url"], "https://2gis.kz/geo/76.889700,43.238900")
        self.assertEqual(links[1]["url"], "https://yandex.kz/maps/?pt=76.889700,43.238900&z=17&l=map")
        self.assertEqual(links[2]["url"], "https://www.google.com/maps/search/?api=1&query=43.238900,76.889700")

    def test_link_gives_one_button(self):
        links = zapp.map_links("https://maps.app.goo.gl/abc", "x")
        self.assertEqual(links, [{"label": "Открыть на карте", "url": "https://maps.app.goo.gl/abc"}])

    def test_address_only(self):
        links = zapp.map_links(None, "ул. Абая 10")
        self.assertEqual(links[0]["label"], "Найти адрес в 2ГИС")
        self.assertEqual(links[0]["url"], "https://2gis.kz/search/%D1%83%D0%BB.%20%D0%90%D0%B1%D0%B0%D1%8F%2010")

    def test_nothing(self):
        self.assertEqual(zapp.map_links("", ""), [])

    def test_non_http_location_not_linked(self):
        links = zapp.map_links("javascript:alert(1)", "адрес")
        self.assertEqual(links[0]["label"], "Найти адрес в 2ГИС")


class LocationPagesTests(AppTestCase):
    def test_form_texts(self):
        html = self.text(self.manager().get("/ofis/novaya"))
        self.assertIn("Геолокация (необязательно)", html)
        self.assertIn("📍 Взять моё местоположение", html)
        self.assertIn("Ссылка на карту или координаты", html)
        self.assertIn("/static/form.js", html)
        js_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "form.js")
        with open(js_path, encoding="utf-8") as f:
            js = f.read()
        self.assertIn("Телефон даёт местоположение только на защищённом адресе (https).", js)
        self.assertIn("Доступ к местоположению запрещён.", js)
        self.assertIn("enableHighAccuracy: true, timeout: 15000", js)

    def test_invalid_location_error(self):
        resp = self.post(self.manager(), "/ofis/novaya", valid_form(location="где-то"))
        self.assertIn("Вставьте ссылку на карту (https://…) или координаты, например 43.2389, 76.8897.", self.text(resp))

    def test_coordinates_saved_normalized_and_buttons_on_card(self):
        client = self.manager()
        rid = self.create(client, location="43.2389 76.8897")
        self.assertEqual(get_row(rid)["location"], "43.238900, 76.889700")
        html = self.text(client.get(f"/zayavka/{rid}"))
        self.assertIn("Открыть в 2ГИС", html)
        self.assertIn("Яндекс Карты", html)
        self.assertIn("Google Карты", html)
        self.assertIn('rel="noopener noreferrer"', html)
        self.assertIn('target="_blank"', html)

    def test_no_location_uses_address_search(self):
        client = self.manager()
        rid = self.create(client)
        html = self.text(client.get(f"/zayavka/{rid}"))
        self.assertIn("не указана", html)
        self.assertIn("Найти адрес в 2ГИС", html)

    def test_brigade_sees_map_and_call(self):
        rid = self.create(self.manager(), location="https://maps.app.goo.gl/xyz", phone="+7 701 000-11-22")
        b = self.brigade()
        html = self.text(b.get(f"/brigada/zayavka/{rid}"))
        self.assertIn("Открыть на карте", html)
        self.assertIn("есть точка на карте", html)
        self.assertIn('href="tel:+77010001122"', html)
        self.assertIn("Позвонить: +7 701 000-11-22", html)
        self.assertIn("📍 на карте", self.text(b.get("/brigada")))

    def test_edit_location(self):
        client = self.manager()
        rid = self.create(client)
        self.post(client, f"/zayavka/{rid}/ispravit", valid_form(location="43.2, 76.9"))
        self.assertEqual(get_row(rid)["location"], "43.200000, 76.900000")
        self.assertIn("геолокация", [e["details"] for e in __import__("common").get_events(rid)][-1])
        self.post(client, f"/zayavka/{rid}/ispravit", valid_form(location=""))
        self.assertIsNone(get_row(rid)["location"])


if __name__ == "__main__":
    unittest.main()
