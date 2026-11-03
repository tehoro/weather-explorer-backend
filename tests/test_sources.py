"""publish_highlights.py: source links go to pages people can read, and source text stays plain."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".claude/skills/weather-highlights/scripts"))
import publish_highlights as P  # noqa: E402


class Sources(unittest.TestCase):
    def test_data_feeds_are_recognised(self):
        for url in ("https://alerts.metservice.com/cap/rss",
                    "https://www.metservice.com/publicData/webdata/national",
                    "https://feeds.meteoalarm.org/api/v1/warnings/feeds-italy",
                    "https://www.jma.go.jp/bosai/typhoon/data/targetTc.json",
                    "https://www.data.jma.go.jp/developer/xml/data/x_VPTW61_010000.xml"):
            self.assertTrue(P.data_url(url), url)
        for url in ("https://www.metservice.com/warnings/home", "https://www.nhc.noaa.gov/xgtwo/two_pac_7d0.png",
                    "https://forecast.weather.gov/product.php?site=NWS&product=AFD",
                    "https://rsmcnewdelhi.imd.gov.in/uploads/archive/1/report.pdf"):
            self.assertFalse(P.data_url(url), url)

    def test_known_feeds_map_to_public_pages(self):
        self.assertEqual(P.public_page("https://www.metservice.com/publicData/webdata/national"),
                         "https://www.metservice.com/national")
        self.assertEqual(P.public_page("https://alerts.metservice.com/cap/rss"),
                         "https://www.metservice.com/warnings/home")
        self.assertEqual(P.public_page("https://feeds.meteoalarm.org/api/v1/warnings/feeds-croatia"),
                         "https://meteo.hr/upozorenja.php")
        self.assertIsNone(P.public_page("https://example.org/api/alerts.json"))

    def test_method_notes_are_caught(self):
        for text in ("scan.rain24[21]: 123 mm near 43S 171E", "Targeted fields.get_frames check",
                     "Values are saved in evidence/field-checks.json", "At the resampled grid cell nearest Napier"):
            self.assertTrue(P.JARGON.search(text), text)
        for text in ("ECMWF model (00 UTC 2 Nov run): about 120 mm on the Southern Alps by 00 UTC 3 Nov",
                     "MetService: orange Heavy Rain Warning for the Westland ranges, 130-180 mm. Expect slips."):
            self.assertIsNone(P.JARGON.search(text), text)


if __name__ == "__main__":
    unittest.main()
