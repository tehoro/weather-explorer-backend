"""Ready-made field data for every highlight scene, built once per sweep.

The app plays a highlight's scenes from these files, so viewing highlights costs the server only a
static file download however many devices ask. (Browsing anywhere else, the app reads ECMWF Open
Data itself.) Files are gzipped JSON in the same format the old /api/fields returned, named by the
feed's generation time so they can be cached for good:

  data/scenes/<tag>/<item id>-<scene>-<role>.json.gz      role: fill, contours or vectors

`build()` adds each scene's file names to the feed as scene["data"] = {"fill": ..., ...}.
Scenes reaching past POLAR_LAT are built on a polar grid (fields.PolarBox), which only the web
viewer can draw; their files go in scene["web_data"] instead, so the iPhone app (build 25) builds
those scenes itself from Open Data, in Mercator, as it does for any scene without files.
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
import tracks

DIR = store.FEED.parent.parent / "scenes"      # data/scenes
KEEP = 2                                       # feeds whose scene files are kept

# Same sizes the app asks for when it loads a view itself.
PIXELS = {"fill": 400, "contours": 200, "vectors": 160}
MARGIN = 0.35   # extra area around the scene box, for wide screens and a little panning
MAX_GROW = 2.0  # most a box's pixel limits grow, to keep detail where the projection widens it
POLAR_LAT = fields.SCENE_POLAR_LAT  # scenes reaching further towards a pole use a polar grid


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


def build_items(items: list[dict], tag: str, source: str = "opendata", out_dir: Path | None = None) -> int:
    """Build scene files for these items into data/scenes/<tag>/, adding scene["data"]; saves them to storage.

    With out_dir (historical events, source "era5") the files go there instead and are not saved."""
    keep = out_dir is None
    out_dir = out_dir or DIR / tag
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for item in items:
        for n, scene in enumerate(item.get("scenes", [])):
            files = {}
            polar = fields.scene_polar_box(scene["bbox"], MARGIN)
            box, grow = (polar, 1.0) if polar else _box_and_grow(scene["bbox"])
            for role, layer in _roles(scene).items():
                if layer not in fields.LAYERS:
                    continue
                name = f"{item['id']}-{n}{'p' if polar else ''}-{role}.json.gz"
                path = out_dir / name
                if not path.exists():
                    try:
                        data = fields.get_frames(layer, box, scene.get("start"), scene.get("end"),
                                                 round(PIXELS[role] * grow),
                                                 fields.scene_frame_count(scene.get("start", ""), scene.get("end", "")),
                                                 source)
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
                scene["web_data" if polar else "data"] = files
            if "fill" in files:
                align_captions(scene, _valid_times(out_dir / files["fill"].split("/", 1)[1]), item["id"], n)
    if source == "opendata":
        _add_tracks(items)
    if keep:
        store.save_scenes(out_dir)
    return written


def _add_tracks(items: list[dict]) -> None:
    """The main low's track for items about a storm (tracks.py), from the runs the frames use."""
    for item in items:                            # an item carried over from an earlier feed brings its
        for scene in item.get("scenes", []):      # old track; only a track built now is shown
            scene.pop("tracks", None)
            scene.pop("track", None)
    try:
        latest = fields.latest_run()
    except Exception as e:
        print(f"[scenes] tracks skipped: {e}", file=sys.stderr, flush=True)
        return
    msl_at = tracks.opendata_msl(latest)
    for item in items:
        if not tracks.wanted(item):
            continue
        try:
            n = tracks.add_to_scenes(item, msl_at, times=tracks.opendata_times(item, latest))
            print(f"[scenes] {item['id']}: track of {n} points", flush=True)
        except Exception as e:
            print(f"[scenes] {item['id']}: no track ({type(e).__name__}: {e})", file=sys.stderr, flush=True)


def _valid_times(path: Path) -> list[str]:
    try:
        return [f["valid_time"] for f in json.loads(gzip.decompress(path.read_bytes()))["frames"]]
    except Exception:
        return []


def align_captions(scene: dict, times: list[str], item_id: str = "?", n: int = 0) -> None:
    """Put every caption on a frame the scene really has. The viewer shows a caption from its time
    until the next one, so a caption between frames would first appear on a later frame, and one
    after the last frame would never appear. Each moves to the nearest frame (the last one at the
    latest); a caption's `until` likewise. The publisher warns about these, so this is a safety net.
    """
    if not times or not scene.get("captions"):
        return
    ts = [fields.parse_time(t) for t in times]

    def nearest(at: str) -> str:
        t = fields.parse_time(at)
        return times[min(range(len(ts)), key=lambda i: (abs((ts[i] - t).total_seconds()), ts[i]))]

    for c in scene["captions"]:
        try:
            at = nearest(c["at"])
        except Exception:
            continue
        if at != c["at"]:
            print(f"[scenes] {item_id} scene {n}: caption {c['at']} moved to frame {at}", file=sys.stderr, flush=True)
            c["at"] = at
        if c.get("until"):
            try:
                c["until"] = nearest(c["until"]) if fields.parse_time(c["until"]) <= ts[-1] else None
            except Exception:
                c["until"] = None
            if not c["until"] or c["until"] <= c["at"]:
                c.pop("until")


def remove(tag: str) -> None:
    shutil.rmtree(DIR / tag, ignore_errors=True)


def missing(feed_path: Path = store.FEED) -> bool:
    """True if the feed has scenes without ready-made data (e.g. an older feed)."""
    try:
        feed = json.loads(feed_path.read_text())
    except Exception:
        return False
    return any("data" not in s and "web_data" not in s for i in feed.get("items", []) for s in i.get("scenes", []))


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
