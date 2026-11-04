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

    def test_vacuous_evidence(self):
        self.assertTrue(P.VACUOUS.search("NASA Ozone Watch and NOAA CPC provide observational context"))
        self.assertFalse(P.VACUOUS.search("NASA Ozone Watch: 9 m/s at 60°N on 1 Nov, lowest since 1979"))


if __name__ == "__main__":
    unittest.main()
