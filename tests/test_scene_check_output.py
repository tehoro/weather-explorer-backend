"""Check the Sol terminal summary against the full report, without fetching weather data."""
import base64
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "scene_checker", BACKEND / ".claude/skills/weather-highlights/scripts/check_scenes.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class SceneCheckOutput(unittest.TestCase):
    def test_reports_survive_targeted_rerun(self):
        stamp = "2026-11-20T00:00Z"
        scene = {"title": "Rain", "bbox": [0, 0, 1, 1], "fill": "rain24",
                 "start": stamp, "end": stamp, "focus": [{"label": "Town", "lat": 0.5, "lon": 0.5}],
                 "captions": [{"at": stamp, "text": "Town gets 100 mm"}]}
        pkg = {"nx": 1, "ny": 1, "lo": 5, "hi": 5, "units": "mm",
               "box": dict(west=0, south=0, east=1, north=1),
               "frames": [{"valid_time": stamp, "run": stamp, "step": 0,
                           "data": base64.b64encode(bytes(2)).decode()}]}

        def fetch(*args):
            print("download detail", file=sys.stderr)
            return pkg

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            draft = root / "draft.json"
            draft.write_text(json.dumps({"items": [{"id": "rain", "scenes": [scene, dict(scene, fill="unknown")]}]}))
            args = ["check_scenes.py", str(draft), "--report-dir", str(root)]
            with patch.object(checker.fields, "latest_run", return_value=checker.fields.parse_time(stamp)), \
                 patch.object(checker.fields, "get_frames", side_effect=fetch), patch.object(checker.os, "nice"):
                for extra in ([], ["--item", "rain", "--scene", "0"]):
                    out = io.StringIO()
                    with patch.object(sys, "argv", args + extra), redirect_stdout(out):
                        checker.main()
                    brief = out.getvalue()
                    self.assertIn("Town 5.0", brief)
                    self.assertIn("says 100 mm", brief)
                    self.assertEqual("unknown fill" in brief, not extra)
                    self.assertNotIn("download detail", brief)
                    self.assertNotIn("on screen until", brief)
                    self.assertIn("NOTE rain scene 0", brief)
            reports = list(root.glob("scene-check-*.log"))
            self.assertEqual(len(reports), 2)
            for report in reports:
                self.assertIn("on screen until", report.read_text())
                self.assertIn("download detail", report.read_text())
