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
import math
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
MAX_GROW = 2.0  # most a box's pixel limits grow, to keep detail where the projection widens it


def _tag(feed: dict) -> str:
    raw = feed.get("generated_utc") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    return re.sub(r"[^0-9A-Za-z]", "", raw)


# The web viewer draws maps in a Lambert azimuthal equal-area projection centred on the scene
# (web/site/app.js). Towards the poles its meridians converge, so a margin in degrees covers less
# of the screen there; the margin is set in that projection instead (plane units: Earth radii).
D = math.pi / 180


def _laea(lon0: float, lat0: float, lon: float, lat: float) -> tuple[float, float]:
    l, sp, cp = (lon - lon0) * D, math.sin(lat0 * D), math.cos(lat0 * D)
    sl, cl, cd = math.sin(lat * D), math.cos(lat * D), math.cos(l)
    k = math.sqrt(2 / max(1e-9, 1 + sp * sl + cp * cl * cd))
    return k * cl * math.sin(l), k * (cp * sl - sp * cl * cd)


def _laea_inv(lon0: float, lat0: float, x: float, y: float) -> tuple[float, float]:
    r, sp, cp = math.hypot(x, y), math.sin(lat0 * D), math.cos(lat0 * D)
    if r < 1e-12:
        return lon0, lat0
    c = 2 * math.asin(min(1.0, r / 2))
    sc, cc = math.sin(c), math.cos(c)
    return (lon0 + math.atan2(x * sc, r * cp * cc - y * sp * sc) / D,
            math.asin(max(-1.0, min(1.0, cc * sp + y * sc * cp / r))) / D)


def _outline(w: float, s: float, e: float, n: float, k: int = 16):
    for i in range(k + 1):
        t = i / k
        yield w + (e - w) * t, s
        yield w + (e - w) * t, n
        yield w, s + (n - s) * t
        yield e, s + (n - s) * t


def _extent(lon0: float, lat0: float, pts) -> tuple[float, float, float, float]:
    xy = [_laea(lon0, lat0, lon, lat) for lon, lat in pts]
    return min(p[0] for p in xy), max(p[0] for p in xy), min(p[1] for p in xy), max(p[1] for p in xy)


def _box_and_grow(bbox: list[float]) -> tuple[str, float]:
    """The data box for a scene box, and how much wider it is than a plain margin in degrees.

    The scene's outline, in the projection centred on it as the viewer centres it, is widened by
    MARGIN each side, and the box is the longitudes and latitudes that rectangle covers.
    """
    w, s, e, n = bbox
    if e <= w:
        e += 360
    lon0, lat0 = (w + e) / 2, (s + n) / 2
    for _ in range(3):                      # centre on the middle of the outline, as the viewer does
        x0, x1, y0, y1 = _extent(lon0, lat0, _outline(w, s, e, n))
        lon0, lat0 = _laea_inv(lon0, lat0, (x0 + x1) / 2, (y0 + y1) / 2)
    x0, x1, y0, y1 = _extent(lon0, lat0, _outline(w, s, e, n))
    dx, dy = (x1 - x0) * MARGIN, (y1 - y0) * MARGIN
    X0, X1, Y0, Y1 = x0 - dx, x1 + dx, y0 - dy, y1 + dy
    edge = [(X0 + (X1 - X0) * i / 32, Y) for i in range(33) for Y in (Y0, Y1)]
    edge += [(X, Y0 + (Y1 - Y0) * i / 32) for i in range(33) for X in (X0, X1)]
    ll = [_laea_inv(lon0, lat0, x, y) for x, y in edge]
    lons = [((lon - lon0 + 540) % 360) - 180 + lon0 for lon, _ in ll]
    bw, be = min(lons), max(lons)
    if be - bw > 360:
        bw, be = lon0 - 180, lon0 + 180
    bs, bn = max(-85.0, min(lat for _, lat in ll)), min(85.0, max(lat for _, lat in ll))
    # Never smaller than the margin in degrees (the app (build 24) still draws in Mercator).
    bw, be = min(bw, w - (e - w) * MARGIN), max(be, e + (e - w) * MARGIN)
    bs, bn = min(bs, max(-85, s - (n - s) * MARGIN)), max(bn, min(85, n + (n - s) * MARGIN))
    grow = min(MAX_GROW, max(1.0, (be - bw) / ((e - w) * (1 + 2 * MARGIN))))
    return f"{bw:.2f},{bs:.2f},{be:.2f},{bn:.2f}", grow


def _box(bbox: list[float]) -> str:
    return _box_and_grow(bbox)[0]


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
            box, grow = _box_and_grow(scene["bbox"])
            for role, layer in _roles(scene).items():
                if layer not in fields.LAYERS:
                    continue
                name = f"{item['id']}-{n}-{role}.json.gz"
                path = out_dir / name
                if not path.exists():
                    try:
                        data = fields.get_frames(layer, box, scene.get("start"), scene.get("end"),
                                                 round(PIXELS[role] * grow), _frames(scene.get("start", ""), scene.get("end", "")))
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
