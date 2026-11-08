"""Storm tracks (tracks.py) on synthetic pressure fields: no downloads."""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fields  # noqa: E402
import tracks  # noqa: E402

T0 = datetime(2026, 11, 8, tzinfo=timezone.utc)
TIMES = [T0 + timedelta(hours=3 * k) for k in range(9)]
GRID = fields.Grid(lats=np.arange(30, -0.01, -0.25), lons=np.arange(-180, -140, 0.25))


def low_at(depth_hpa: float, base_hpa: float = 1010.0):
    """A round low drifting west 1 degree every 3 h from 12N 155W, depth_hpa below base_hpa."""
    lat2, lon2 = np.meshgrid(GRID.lats, GRID.lons, indexing="ij")

    def at(t):
        k = TIMES.index(t) if t in TIMES else None
        if k is None:
            return None
        r2 = (lat2 - 12) ** 2 + (lon2 - (-155 - k)) ** 2
        return (base_hpa - depth_hpa * np.exp(-r2 / 8)) * 100, GRID
    return at


def item(hazards, **extra):
    return {"hazards": hazards, "where": {"bbox": [-175, 5, -142, 25]},
            "when": {"peak": fields.iso(TIMES[4])},
            "scenes": [{"start": fields.iso(TIMES[0]), "end": fields.iso(TIMES[-1]), "fill": "wind10",
                        "contours": "mslp", "track": {"name": "old", "points": []}}], **extra}


class Tracks(unittest.TestCase):
    def test_weak_tropical_low_is_followed(self):
        it = item(["tropical-cyclone"])
        self.assertEqual(tracks.add_to_scenes(it, low_at(5), times=TIMES), len(TIMES))
        self.assertEqual(len(it["scenes"][0]["track"]["points"]), len(TIMES))

    def test_weak_low_not_followed_for_other_storms(self):
        it = item(["rain"])
        self.assertEqual(tracks.add_to_scenes(it, low_at(5), times=TIMES), 0)
        self.assertNotIn("track", it["scenes"][0])          # the old track is gone too

    def test_storms_near_names_it(self):
        it = item(["rain"], storms=[{"name": "Pena", "at": fields.iso(TIMES[2]), "near": [12, -157]},
                                     {"name": "Again", "at": fields.iso(TIMES[4]), "near": [12, -159]}])
        tracks.add_to_scenes(it, low_at(5), times=TIMES)
        self.assertEqual([t["name"] for t in it["scenes"][0]["tracks"]], ["Pena"])   # one low, drawn once

    def test_flat_field_has_no_track(self):
        it = item(["tropical-cyclone"])
        self.assertEqual(tracks.add_to_scenes(it, low_at(0.3), times=TIMES), 0)


if __name__ == "__main__":
    unittest.main()
