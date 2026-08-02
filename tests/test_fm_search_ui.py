import unittest

from src.commands import (
    FM_PAGE_SIZE,
    _FM_INDEX_EMOJIS,
    _fm_page_count,
    _fm_station_meta_line,
    _truncate_ui,
    build_fm_results_embed,
)


class FmSearchUiTests(unittest.TestCase):
    def test_truncate_ui(self) -> None:
        self.assertEqual(_truncate_ui("  hello   world  ", 50), "hello world")
        self.assertEqual(_truncate_ui("abcdefghij", 5), "abcd…")
        self.assertEqual(_truncate_ui("short", 10), "short")

    def test_station_meta_line(self) -> None:
        line = _fm_station_meta_line(
            {
                "country": "Venezuela",
                "state": "Caracas",
                "codec": "MP3",
                "bitrate": 128,
            }
        )
        self.assertIn("Venezuela", line)
        self.assertIn("Caracas", line)
        self.assertIn("MP3 128kbps", line)

    def test_page_count(self) -> None:
        self.assertEqual(_fm_page_count(0), 1)
        self.assertEqual(_fm_page_count(1), 1)
        self.assertEqual(_fm_page_count(10), 1)
        self.assertEqual(_fm_page_count(11), 2)
        self.assertEqual(_fm_page_count(50, 10), 5)

    def test_build_embed_numbers_and_pagination(self) -> None:
        stations = [
            {
                "name": f"Station {i}",
                "country": "Venezuela",
                "state": "Caracas" if i == 0 else "",
                "codec": "MP3",
                "bitrate": 128,
                "favicon": "",
            }
            for i in range(15)
        ]
        embed = build_fm_results_embed(
            stations,
            query_label="mega",
            filter_suffix=" (countrycode:VE)",
            page=0,
        )
        self.assertEqual(embed.title, "📻 Resultados FM · pág. 1/2")
        desc = embed.description or ""
        self.assertIn(_FM_INDEX_EMOJIS[0], desc)
        self.assertIn(_FM_INDEX_EMOJIS[9], desc)
        self.assertIn("**Station 0**", desc)
        self.assertIn("**Station 9**", desc)
        self.assertNotIn("**Station 10**", desc)
        self.assertIsNotNone(embed.footer)
        self.assertIn("15 emisoras", embed.footer.text or "")
        self.assertIn("Anterior / Siguiente", embed.footer.text or "")

        page2 = build_fm_results_embed(stations, query_label="mega", page=1)
        self.assertEqual(page2.title, "📻 Resultados FM · pág. 2/2")
        desc2 = page2.description or ""
        self.assertIn("**Station 10**", desc2)
        self.assertIn(_FM_INDEX_EMOJIS[0], desc2)  # page-local numbering
        self.assertEqual(FM_PAGE_SIZE, 10)


if __name__ == "__main__":
    unittest.main()
