"""Historical events: build a past event's scenes from ERA5 (see docs/historical-events-proposal.md).

  uv run --extra history history.py <item.json> [--out data/history]

The item is a highlight in the feed format (references/feed-schema.md) with kind "historical",
written and reviewed before it gets here. Its scenes are built from the ERA5 reanalysis (era5.py)
with the same layers, boxes and frame counts as live scenes, into

  <out>/<item id>/item.json                       the item, scene "data" naming the files below, and
                                                  scene "track": the main low's track (tracks.py)
  <out>/<item id>/<item id>-<n>-<role>.json.gz    role: fill, contours or vectors

The web repository's history.py turns that folder into the viewer's packages. This never runs on
the Replit server (§2 of AGENTS.md): it is a one-off batch job per event.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import era5
import scenes
import tracks


def build(item_path: Path, out: Path) -> Path:
    item = json.loads(item_path.read_text())
    if item.get("kind") != "historical":
        raise SystemExit(f"{item_path}: kind must be 'historical'")
    folder = out / item["id"]
    folder.mkdir(parents=True, exist_ok=True)
    written = scenes.build_items([item], "history", source="era5", out_dir=folder)
    points = tracks.add_to_scenes(item, lambda t: (era5.field("msl", t), era5.GRID))
    print(f"[history] {item['id']}: track of {points} hourly points", flush=True)
    for scene in item.get("scenes", []):
        if not scene.get("data") and not scene.get("web_data"):
            print(f"[history] {item['id']}: scene '{scene.get('title')}' has no data", file=sys.stderr)
    (folder / "item.json").write_text(json.dumps(item, ensure_ascii=False, indent=1))
    print(f"[history] {item['id']}: {written} files built in {folder}", flush=True)
    return folder


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("item", type=Path)
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "data" / "history")
    a = ap.parse_args()
    build(a.item, a.out)


if __name__ == "__main__":
    main()
