"""Ready-made field data for every highlight scene, built once per sweep.

The app plays a highlight's scenes from these files, so viewing highlights costs the server only a
static file download however many devices ask. (Browsing anywhere else, the app reads ECMWF Open
Data itself.) Files are gzipped JSON in the same format the old /api/fields returned, named by the
feed's generation time so they can be cached for good:

  data/scenes/<tag>/<item id>-<scene>-<role>.json.gz      role: fill, contours or vectors

`build()` adds each scene's file names to the feed as scene["data"] = {"fill": ..., ...}.
"""
from __future__ import annotations

import gzip
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import fields
import store

DIR = store.FEED.parent.parent / "scenes"      # data/scenes
KEEP = 2                                       # feeds whose scene files are kept

# Same sizes the app asks for when it loads a view itself.
PIXELS = {"fill": 400, "contours": 200, "vectors": 160}
MARGIN = 0.35   # extra area around the scene box, for wide screens and a little panning


def _tag(feed: dict) -> str:
    raw = feed.get("generated_utc") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    return re.sub(r"[^0-9A-Za-z]", "", raw)


def _box(bbox: list[float]) -> str:
    w, s, e, n = bbox
    if e <= w:
        e += 360
    dx, dy = (e - w) * MARGIN, (n - s) * MARGIN
    return f"{w - dx:.2f},{max(-85, s - dy):.2f},{e + dx:.2f},{min(85, n + dy):.2f}"


def _frames(start: str, end: str) -> int:
    try:
        hours = (fields.parse_time(end) - fields.parse_time(start)).total_seconds() / 3600
    except Exception:
        hours = 72
    return int(min(48, max(4, hours // 3 + 1)))


def _roles(scene: dict) -> dict[str, str]:
    """Which layer each role needs, skipping ones the fill layer already provides."""
    fill, contours, vectors = scene["fill"], scene.get("contours"), scene.get("vectors")
    roles = {"fill": fill}
    if contours and contours != fill:
        roles["contours"] = contours
    if vectors and vectors not in (fill, contours):
        roles["vectors"] = vectors
    return roles


def build(feed_path: Path = store.FEED) -> int:
    """Build the files for every scene in the feed and record them in it. Returns files written."""
    feed = json.loads(feed_path.read_text())
    tag = _tag(feed)
    written = build_items(feed.get("items", []), tag)
    tmp = feed_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(feed, ensure_ascii=False, indent=1))
    tmp.replace(feed_path)
    _prune()
    print(f"[scenes] {tag}: {written} files built", flush=True)
    return written


def build_items(items: list[dict], tag: str) -> int:
    """Build scene files for these items into data/scenes/<tag>/, adding scene["data"]; saves them to storage."""
    out_dir = DIR / tag
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for item in items:
        for n, scene in enumerate(item.get("scenes", [])):
            files = {}
            for role, layer in _roles(scene).items():
                if layer not in fields.LAYERS:
                    continue
                name = f"{item['id']}-{n}-{role}.json.gz"
                path = out_dir / name
                if not path.exists():
                    try:
                        data = fields.get_frames(layer, _box(scene["bbox"]), scene.get("start"), scene.get("end"),
                                                 PIXELS[role], _frames(scene.get("start", ""), scene.get("end", "")))
                    except Exception as e:
                        print(f"[scenes] {item['id']} scene {n} {layer}: {e}", file=sys.stderr, flush=True)
                        continue
                    if not data.get("frames"):
                        continue
                    tmp = path.with_suffix(".part")
                    tmp.write_bytes(gzip.compress(json.dumps(data, separators=(",", ":")).encode(), 6))
                    tmp.rename(path)
                    written += 1
                files[role] = f"{tag}/{name}"
            if files:
                scene["data"] = files
    store.save_scenes(out_dir)
    return written


def remove(tag: str) -> None:
    shutil.rmtree(DIR / tag, ignore_errors=True)


def missing(feed_path: Path = store.FEED) -> bool:
    """True if the feed has scenes without ready-made data (e.g. an older feed)."""
    try:
        feed = json.loads(feed_path.read_text())
    except Exception:
        return False
    return any("data" not in s for i in feed.get("items", []) for s in i.get("scenes", []))


def path_for(rel: str) -> Path | None:
    """Local file for a scene path from the feed, fetched back from storage if the disk was reset."""
    if not re.fullmatch(r"[0-9A-Za-z]+/[0-9A-Za-z_.\-]+\.json\.gz", rel):
        return None
    p = DIR / rel
    if p.exists() or store.restore_scene(rel, p):
        return p
    return None


def _prune() -> None:
    """Keep the scene files of the last few feeds (requested highlights are removed when they expire)."""
    dirs = sorted((d for d in DIR.iterdir() if d.is_dir() and d.name[:1].isdigit()), key=lambda d: d.name, reverse=True)
    for d in dirs[KEEP:]:
        shutil.rmtree(d, ignore_errors=True)
