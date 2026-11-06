"""Frame times, caption alignment and the publisher's text checks (no downloads)."""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / ".claude/skills/weather-highlights/scripts"))

import fields  # noqa: E402
import publish_highlights as P  # noqa: E402
import scenes  # noqa: E402

H = timedelta(hours=1)


class FrameTimes(unittest.TestCase):
    latest = datetime(2026, 11, 3, 18, tzinfo=timezone.utc)

    def test_long_window_is_even_on_synoptic_hours_and_reaches_the_end(self):
        start, end = self.latest - 6 * H, self.latest + 234 * H          # 03T12 .. 13T12
        times = fields.frame_times(start, end, self.latest, fields.scene_frame_count(fields.iso(start), fields.iso(end)))
        self.assertLessEqual(len(times), 48)
        self.assertEqual(times[-1], end)
        self.assertTrue(all(t.hour % 6 == 0 for t in times))
        self.assertIn(datetime(2026, 11, 11, 12, tzinfo=timezone.utc), times)

    def test_short_window_keeps_three_hourly(self):
        times = fields.frame_times(self.latest, self.latest + 48 * H, self.latest, 17)
        self.assertEqual([(b - a) / H for a, b in zip(times, times[1:])], [3.0] * 16)


class RainTotals(unittest.TestCase):
    latest = datetime(2026, 11, 4, 6, tzinfo=timezone.utc)

    def test_window_comes_from_one_run_and_spans_24_hours(self):
        for t in [self.latest - 18 * H, self.latest + 3 * H, self.latest + 51 * H, self.latest + 162 * H]:
            run, step, prev = fields.pick_window(t, self.latest, 24)
            self.assertEqual(run + step * H, t)
            self.assertEqual(step - prev, 24)
            self.assertLessEqual(run, self.latest)
            self.assertIn(step, fields.native_steps(run.hour))
            self.assertIn(prev, fields.native_steps(run.hour))

    def test_beyond_the_short_run_uses_the_long_one(self):
        run, step, _ = fields.pick_window(self.latest + 162 * H, self.latest, 24)
        self.assertEqual(run.hour, 0)


class AlignCaptions(unittest.TestCase):
    times = ["2026-11-11T06:00Z", "2026-11-11T18:00Z", "2026-11-12T06:00Z"]

    def test_moves_to_nearest_and_never_past_the_last_frame(self):
        sc = {"captions": [{"at": "2026-11-11T12:00Z", "text": "a"}, {"at": "2026-11-11T19:00Z", "text": "b"},
                           {"at": "2026-11-13T12:00Z", "text": "c", "until": "2026-11-14T00:00Z"}]}
        scenes.align_captions(sc, self.times)
        self.assertEqual([c["at"] for c in sc["captions"]], ["2026-11-11T06:00Z", "2026-11-11T18:00Z", "2026-11-12T06:00Z"])
        self.assertNotIn("until", sc["captions"][2])


class PublicText(unittest.TestCase):
    def test_process_language_from_the_review_is_caught(self):
        for text in ["clears the skill's 935 hPa threshold", "The scan's −5 °C flag", "the app's interval-mean rain",
                     "rather than a duplicate explainer", "a newer confirming analysis was not obtained",
                     "the public warning page could not be fetched", "the older public PDF", "CAP severity",
                     "evidence/field-checks.json"]:
            self.assertTrue(P.PROCESS.search(text), text)

    def test_weather_text_passes(self):
        for text in ["Heavy rain over Cape Town", "CAPE above 3000 J/kg", "The polar cap warms 8 °C",
                     "The sweep of cold air reaches the Alps", "A gas pipeline was damaged"]:
            self.assertFalse(P.PROCESS.search(text), text)

    def test_disclaimers_and_rate_versus_total(self):
        for text in ["no new flooding is asserted", "no mainland landfall is implied", "no sea-ice change is claimed",
                     "Offshore wind maxima should not be read as town forecasts"]:
            self.assertTrue(P.HEDGE.search(text), text)
        self.assertFalse(P.HEDGE.search("Forecasters claimed a record"))
        self.assertTrue(P.MODEL_VS_WARNING.search("Regional model rain is lower than the warning's local totals"))
        self.assertFalse(P.MODEL_VS_WARNING.search("The model gives 60 mm in 24 h near Burgas, close to the red warning"))

    def test_caption_time_stamps(self):
        for text in ["At 21 UTC 4 Nov: winds reach 38 m/s", "By 12 UTC 5 Nov: 69 kg/m²", "By 9 Nov, the jet weakens",
                     "Early 6 Nov: rain spreads east", "On Thursday the low deepens"]:
            self.assertTrue(P.CAPTION_TIME.search(text), text)
        for text in ["Winds reach 38 m/s west of Chichijima", "The 24 h total since 12 UTC reaches 90 mm",
                     "60°N winds turn easterly", "850 hPa air reaches 28 °C"]:
            self.assertFalse(P.CAPTION_TIME.search(text), text)

    def test_feed_language_confidence_and_grid_points(self):
        for text in ["up from 48 in the previous feed", "High confidence in the original warning", "since the last sweep"]:
            self.assertTrue(P.PROCESS.search(text), text)
        self.assertFalse(P.PROCESS.search("The last feed of moist air reaches the coast before the front"))
        self.assertTrue(P.SOURCE_CONFIDENCE.search("High confidence in the agency warning; moderate in totals"))
        self.assertFalse(P.SOURCE_CONFIDENCE.search("High confidence in the track; moderate in peak gusts"))
        self.assertTrue(P.GRID_COORD.search("−2.1 °C near 85.75°N, 55.25°E"))
        self.assertFalse(P.GRID_COORD.search("a 930 hPa low near 69°S, 90°W"))

    def test_flat_captions_and_variety(self):
        sc = {"fill": "tcwv", "captions": [{"at": "a", "text": "Quibdó has 63 kg/m²"}, {"at": "b", "text": "65 kg/m² at Quibdó"},
                                           {"at": "c", "text": "Quibdó still has 64 kg/m²"}]}
        w = []
        P.check_caption_values(sc, "s", w)
        self.assertEqual(len(w), 1)
        heat = {"fill": "t2m", "captions": [{"at": "a", "text": "45 °C"}, {"at": "b", "text": "44 °C"}, {"at": "c", "text": "45 °C"}]}
        w = []
        P.check_caption_values(heat, "s", w)
        self.assertEqual(w, [])
        self.assertTrue(P.near([-82, -5, -77, 2], [-79, 2, -75, 8]))
        self.assertTrue(P.near([170, 40, 190, 50], [-175, 45, -160, 55]))      # across the date line
        self.assertFalse(P.near([6, 38, 12, 44], [97, 5, 103, 12]))

    def test_vortex_name(self):
        for text in ["The Arctic vortex weakens", "the antarctic vortex splits"]:
            self.assertTrue(P.VORTEX_NAME.search(text), text)
        for text in ["The Arctic polar vortex weakens", "a displaced Atlantic vortex lobe", "the vortex splits"]:
            self.assertFalse(P.VORTEX_NAME.search(text), text)

    def test_new_zealand_item(self):
        self.assertTrue(P.about_nz({"where": {"country": "New Zealand"}, "title": "Rain on the Southern Alps"}))
        self.assertTrue(P.about_nz({"where": {"country": "Tasman Sea"}, "title": "Low heads for New Zealand"}))
        self.assertFalse(P.about_nz({"where": {"country": "Australia"}, "title": "Heat in Perth"}))

    def test_short_until_warns(self):
        sc = {"fill": "mslp", "start": "2026-11-04T00:00Z", "end": "2026-11-05T00:00Z",
              "captions": [{"at": "2026-11-04T00:00Z", "text": "a", "until": "2026-11-04T03:00Z"},
                           {"at": "2026-11-04T12:00Z", "text": "b", "until": "2026-11-05T00:00Z"}]}
        w = []
        orig = P.fields.scene_times
        P.fields.scene_times = lambda *a: [P.fields.parse_time("2026-11-04T00:00Z") + k * H for k in range(0, 25, 3)]
        try:
            P.check_captions(sc, "s", w)
        finally:
            P.fields.scene_times = orig
        self.assertEqual(len([x for x in w if "under 12 h" in x]), 1)

    def test_vacuous_evidence(self):
        self.assertTrue(P.VACUOUS.search("NASA Ozone Watch and NOAA CPC provide observational context"))
        self.assertFalse(P.VACUOUS.search("NASA Ozone Watch: 9 m/s at 60°N on 1 Nov, lowest since 1979"))


if __name__ == "__main__":
    unittest.main()
