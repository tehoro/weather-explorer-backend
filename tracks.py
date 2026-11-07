"""The track of an item's main low: its centre and central pressure through a scene's frames.

The storm is the one the item is about, found from the item itself: the deepest low inside
where.bbox at when.peak (falling back to when.start). From there it is followed forwards and
backwards, at each step taking the lowest pressure within reach of where the centre was heading,
and refined between grid points so it moves smoothly. Its name comes from the title when it has
one ("Cyclone Gabrielle", "Hurricane Katrina", "Storm Ciarán").

Each scene gets

  "track": {"name": "Gabrielle" | null,
            "points": [{"t": "2023-02-13T00:00Z", "lat": -35.12, "lon": 175.03, "msl": 968.4}, ...]}

for the scene's window. The viewer draws the line, glides a marker along it and blends the
frames round the centre so the low moves rather than fades.

Built once per scene (no per-request work). Any source works: msl_at(t) returns a global msl
field (Pa) on a fields.Grid, or None where there is none. Historical items use ERA5 (hourly). Live
highlights about a storm (wanted()) use Open Data at the frames' 3-hourly (later 6-hourly) times,
from the same runs as the frames (opendata_msl, opendata_times); an item can opt out with
"track": false when the deepest low in its box at its peak is not its storm.
"""
from __future__ import annotations

import functools
import math
import re
from datetime import datetime
from typing import Callable

import numpy as np

import fields

MAX_SPEED_KMH = 160      # fastest a low's centre is followed (the 1987 storm reached about 130 km/h)
FIND_KM = 250            # search radius round the predicted position, beyond the expected move
DEEP_HPA = 1000          # a centre shallower than this ends the track
MIN_POINTS = 4           # a shorter track is more likely a passing wobble than the storm
NAME = re.compile(r"\b(?:Super Typhoon|Typhoon|Hurricane|Cyclone|Tropical Storm|Storm|Medicane)\s+([A-Z][a-zà-ÿ]+)")

FieldAt = Callable[[datetime], "tuple[np.ndarray, fields.Grid] | None"]
STORMS = {"tropical-cyclone", "wind", "rain", "flood", "coastal", "snow"}   # hazards a live item's low may be the story of


def storm_name(title: str) -> str | None:
    m = NAME.search(title or "")
    return m.group(1) if m else None


def _km(lat1, lon1, lat2, lon2):
    dlon = (lon2 - lon1 + 540) % 360 - 180
    x = math.radians(dlon) * math.cos(math.radians((lat1 + lat2) / 2))
    return 6371.0 * math.hypot(x, math.radians(lat2 - lat1))


def _window(grid: "fields.Grid", lat: float, lon: float, km: float):
    """Row and column indexes of the grid within about km of (lat, lon)."""
    dlat = km / 111.0
    dlon = km / (111.0 * max(0.2, math.cos(math.radians(lat))))
    rows = np.where(np.abs(grid.lats - lat) <= dlat)[0]
    east = (grid.lons - lon + 540) % 360 - 180          # degrees east of lon, across the date line
    cols = np.where(np.abs(east) <= dlon)[0]
    return rows, cols[np.argsort(east[cols])]            # west to east, so neighbours stay neighbours


def _centre(vals: np.ndarray, grid: "fields.Grid", lat: float, lon: float, km: float) -> dict | None:
    """The lowest pressure within km of (lat, lon), refined between grid points; None if the lowest
    point is on the edge of the search (no closed low there)."""
    rows, cols = _window(grid, lat, lon, km)
    if len(rows) < 3 or len(cols) < 3:
        return None
    sub = vals[np.ix_(rows, cols)]
    lats, lons = np.meshgrid(grid.lats[rows], grid.lons[cols], indexing="ij")
    dist = np.vectorize(lambda a, b: _km(lat, lon, a, b))(lats, lons)
    sub = np.where(dist <= km, sub, np.inf)
    i, j = np.unravel_index(np.argmin(sub), sub.shape)
    if i in (0, sub.shape[0] - 1) or j in (0, sub.shape[1] - 1) or dist[i, j] > km - 40:
        return None
    if not all(np.isfinite(sub[i + di, j + dj]) for di in (-1, 0, 1) for dj in (-1, 0, 1)):
        return None
    f0 = sub[i, j]

    def offset(fm, fp):  # vertex of a parabola through three points, in grid steps
        d = fm - 2 * f0 + fp
        return max(-0.5, min(0.5, 0.5 * (fm - fp) / d)) if d > 0 else 0.0

    oy, ox = offset(sub[i - 1, j], sub[i + 1, j]), offset(sub[i, j - 1], sub[i, j + 1])
    dlat = grid.lats[1] - grid.lats[0]
    dlon = grid.lons[1] - grid.lons[0]
    p = f0 - 0.25 * ((sub[i - 1, j] - sub[i + 1, j]) * oy + (sub[i, j - 1] - sub[i, j + 1]) * ox)
    return {"lat": float(lats[i, j] + oy * dlat), "lon": float(lons[i, j] + ox * dlon), "msl": float(p) / 100.0}


def follow(msl_at: FieldAt, times: list[datetime], start: datetime, bbox: list[float]) -> list[dict]:
    """The deepest low in bbox ([w, s, e, n]) at `start`, followed through `times` (sorted)."""
    w, s, e, n = bbox
    if (got := msl_at(start)) is None:
        return []
    vals, grid = got
    rows = np.where((grid.lats >= s) & (grid.lats <= n))[0]
    cols = np.where(((grid.lons - w) % 360) <= ((e - w) % 360 or 360))[0]
    sub = vals[np.ix_(rows, cols)]
    i, j = np.unravel_index(np.argmin(sub), sub.shape)
    first = _centre(vals, grid, float(grid.lats[rows[i]]), float(grid.lons[cols[j]]), 150)
    if not first or first["msl"] > DEEP_HPA:
        return []
    first["t"] = start
    track = [first]
    for direction in (1, -1):
        seq = [t for t in times if (t - start).total_seconds() * direction > 0][::direction]
        prev, vel = first, (0.0, 0.0)
        for t in seq:
            hours = abs((t - prev["t"]).total_seconds()) / 3600
            guess_lat, guess_lon = prev["lat"] + vel[0] * hours, prev["lon"] + vel[1] * hours
            field = msl_at(t)
            if field is None:
                break
            c = _centre(field[0], grid, guess_lat, guess_lon, FIND_KM + MAX_SPEED_KMH * hours * 0.5)
            if not c or c["msl"] > DEEP_HPA or _km(prev["lat"], prev["lon"], c["lat"], c["lon"]) > MAX_SPEED_KMH * hours:
                break
            c["t"] = t
            vel = ((c["lat"] - prev["lat"]) / hours, ((c["lon"] - prev["lon"] + 540) % 360 - 180) / hours)
            prev = c
            track.append(c)
    track.sort(key=lambda p: p["t"])
    return _smooth(track)


def _smooth(track: list[dict]) -> list[dict]:
    """A light 1-2-1 smoothing of positions (not pressure), to take out grid-scale jitter."""
    out = []
    for k, p in enumerate(track):
        q = dict(p)
        if 0 < k < len(track) - 1:
            a, b = track[k - 1], track[k + 1]
            q["lat"] = (a["lat"] + 2 * p["lat"] + b["lat"]) / 4
            q["lon"] = p["lon"] + ((a["lon"] - p["lon"] + 540) % 360 - 180 + (b["lon"] - p["lon"] + 540) % 360 - 180) / 4
        out.append(q)
    return out


def add_to_scenes(item: dict, msl_at: FieldAt, step_hours: int = 1, times: list[datetime] | None = None) -> int:
    """Track the item's main low over all its scenes' windows and give each scene its part, hourly
    (step_hours) or at the given times. Returns the number of track points (0: no deep low found,
    and no scene gets a track)."""
    when, where = item.get("when") or {}, item.get("where") or {}
    windows = [(fields.parse_time(s["start"]), fields.parse_time(s["end"]))
               for s in item.get("scenes", []) if s.get("start") and s.get("end")]
    if not windows or not where.get("bbox") or not (when.get("peak") or when.get("start")):
        return 0
    t0, t1 = min(a for a, _ in windows), max(b for _, b in windows)
    start = fields.parse_time(when.get("peak") or when["start"])
    start = min(max(start.replace(minute=0, second=0, microsecond=0), t0), t1)
    if times is not None:
        if not times:
            return 0
        start = min(times, key=lambda t: abs((t - start).total_seconds()))
    else:
        times = [t0 + k * step_hours * fields.H for k in range(int((t1 - t0) / fields.H) // step_hours + 1)]
        if start not in times:
            times = sorted(set(times) | {start})
    points = follow(msl_at, times, start, where["bbox"])
    if len(points) < MIN_POINTS:
        points = []
    name = storm_name(item.get("title", ""))
    for scene, (a, b) in zip([s for s in item["scenes"] if s.get("start") and s.get("end")], windows):
        pts = [{"t": fields.iso(p["t"]), "lat": round(p["lat"], 3), "lon": round(p["lon"], 3), "msl": round(p["msl"], 1)}
               for p in points if a <= p["t"] <= b]
        if len(pts) >= 2:
            scene["track"] = {"name": name, "points": pts}
        else:
            scene.pop("track", None)
    return len(points)


# --------------------------------------------------------------------------------------------
# Live highlights (Open Data)
# --------------------------------------------------------------------------------------------

def wanted(item: dict) -> bool:
    """A live item about a storm, shown with isobars: not an explainer, and not opted out."""
    if item.get("track") is False or item.get("kind") == "explainer":
        return False
    return bool(set(item.get("hazards") or []) & STORMS) and any(
        "mslp" in (s.get("fill"), s.get("contours")) for s in item.get("scenes", []))


def opendata_times(item: dict, latest: datetime) -> list[datetime]:
    """The frame times (3-hourly, 6-hourly beyond +144 h) over the item's scenes, within what Open
    Data still holds."""
    windows = [(fields.parse_time(s["start"]), fields.parse_time(s["end"]))
               for s in item.get("scenes", []) if s.get("start") and s.get("end")]
    if not windows:
        return []
    t0 = max(min(a for a, _ in windows), latest - 72 * fields.H)
    t1 = min(max(b for _, b in windows), latest + 360 * fields.H)
    return fields.frame_times(t0, t1, latest, 10_000) if t0 <= t1 else []


def opendata_msl(latest: datetime) -> FieldAt:
    """msl at valid time t from the run the frames use (fields.pick_source), 0.25 deg; a few
    decoded fields are kept, since a track is followed forwards and backwards from its peak."""
    @functools.lru_cache(maxsize=8)
    def at(t: datetime):
        src = fields.pick_source(t, latest, False)
        path = fields.oper_field(src[0], "0p25", "msl", src[1], None) if src else None
        return fields.read_one(path) if path else None
    return at
