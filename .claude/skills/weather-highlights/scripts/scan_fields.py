#!/usr/bin/env python3
"""Objective scan of ECMWF fields for unusual or severe weather, worldwide.

All inputs are ECMWF Open Data (no MARS), so the same scan could run anywhere.

Signals (each is evidence for Claude to weigh, not a verdict):
  * ensemble - IFS ENS probability regions over land and coasts: chance of >50 mm precipitation
               and of gusts >25 m/s in daily 24 h windows, days 1-10, >= threshold.
  * low      - deep surface lows in IFS HRES (Open Data), tracked crudely across 12-hourly steps.
               Tropical lows (|lat| < 30) are flagged as possible tropical cyclones.
  * rain24   - 24 h precipitation totals >= 100 mm in IFS HRES.
  * gust     - 10 m gusts >= 35 m/s in IFS HRES (3-hourly maxima, days 0-5).

Usage (from the backend directory):
  uv run python .claude/skills/weather-highlights/scripts/scan_fields.py --out data/highlights/scan.json
Takes about a minute the first time (downloads are cached for the data service).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

BACKEND = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(BACKEND))
import fields  # noqa: E402  (the backend's data service: downloads, caching, decoding)

H = timedelta(hours=1)
ENS_PARAMS = {"tpg50": "chance of >50 mm precipitation in 24 h", "10fgg25": "chance of gusts >25 m/s in 24 h"}


def label(mask: np.ndarray, wrap_lon: bool = True) -> list[np.ndarray]:
    """Connected regions of True cells (8-connectivity, periodic in longitude). Own code: no scipy."""
    seen = np.zeros_like(mask, bool)
    ny, nx = mask.shape
    regions = []
    for y0, x0 in zip(*np.nonzero(mask)):
        if seen[y0, x0]:
            continue
        q, cells = deque([(y0, x0)]), []
        seen[y0, x0] = True
        while q:
            y, x = q.popleft()
            cells.append((y, x))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    yy, xx = y + dy, x + dx
                    if wrap_lon:
                        xx %= nx
                    if 0 <= yy < ny and 0 <= xx < nx and mask[yy, xx] and not seen[yy, xx]:
                        seen[yy, xx] = True
                        q.append((yy, xx))
        regions.append(np.array(cells))
    return regions


def describe(cells: np.ndarray, vals: np.ndarray, grid: fields.Grid, pick=np.argmax) -> dict:
    lats, lons = grid.lats[cells[:, 0]], grid.lons[cells[:, 1]]
    v = vals[cells[:, 0], cells[:, 1]]
    i = pick(v)
    # Longitude extent that may cross the dateline: unwrap around the extreme point.
    rel = ((lons - lons[i] + 180) % 360) - 180
    west, east = lons[i] + rel.min(), lons[i] + rel.max()
    return {"extreme": round(float(v[i]), 2), "at": [round(float(lats[i]), 2), round(float(lons[i]), 2)],
            "bbox": [round(float(west), 1), round(float(lats.min()), 1), round(float(east), 1), round(float(lats.max()), 1)],
            "area_cells": int(len(cells))}


def area_km2(cells: np.ndarray, grid: fields.Grid) -> float:
    dlat = abs(grid.lats[1] - grid.lats[0])
    dlon = abs(grid.lons[1] - grid.lons[0])
    lat = np.radians(grid.lats[cells[:, 0]])
    return float(np.sum((111.2 * dlat) * (111.2 * dlon) * np.cos(lat)))


def scan_ensemble(threshold: float, min_area_km2: float) -> tuple[list[dict], dict]:
    run = fields.latest_run("ens")
    wins = [(d * 24, d * 24 + 24) for d in range(10)]
    jobs = [(p, s, e) for p in ENS_PARAMS for s, e in wins]
    with ThreadPoolExecutor(8) as ex:
        paths = list(ex.map(lambda j: fields.ens_field(run, "0p25", *j), jobs))
    out, coast = [], None
    for (param, s, e), path in zip(jobs, paths):
        if not path:
            continue
        vals, grid = fields.read_one(path)
        if np.nanmax(vals) <= 1.0:
            vals = vals * 100
        vals = vals[::2, ::2]
        grid = fields.Grid(grid.lats[::2], grid.lons[::2])   # 0.5 deg is plenty for finding regions
        if coast is None:
            coast = land_and_coast(grid)
        for cells in label((vals >= threshold) & coast):
            a = area_km2(cells, grid)
            if a < min_area_km2:
                continue
            d = describe(cells, vals, grid)
            out.append({"signal": "ensemble", "param": param, "what": ENS_PARAMS[param], "units": "%",
                        "window_start": fields.iso(run + s * H), "window_end": fields.iso(run + e * H),
                        "lead_day": e // 24, "area_km2": round(a), **d, "run": fields.iso(run)})
    return merge_days(out), {"ensemble": f"IFS ENS probabilities {fields.iso(run)} from ECMWF Open Data, threshold {threshold:.0f}%"}


def land_and_coast(grid: fields.Grid) -> np.ndarray:
    """Land plus a two-cell fringe, from the IFS land-sea mask (Open Data step 0) on this grid.
    Impact-focused: storms over open ocean are found by the deep-low scan instead."""
    run = fields.latest_run()
    lsm, lgrid = fields.read_one(fields.opendata_file(run, "lsm", 0, None))
    land = fields.resample(lsm, lgrid, grid.lons, grid.lats) > 0.5
    out = land.copy()
    for dy in (-2, -1, 0, 1, 2):
        for dx in (-2, -1, 0, 1, 2):
            out |= np.roll(np.roll(land, dy, axis=0), dx, axis=1)
    out[grid.lats < -60, :] = False   # Antarctica: no population
    return out


def merge_days(items: list[dict]) -> list[dict]:
    """Chain the same region across consecutive days into one entry with a day list.

    A region joins a chain only if it overlaps that chain's region on the previous day, so
    separate same-day regions stay separate and chains don't snowball into continents.
    """
    items.sort(key=lambda d: (d["param"], d["what"], d["lead_day"]))
    merged: list[dict] = []
    for it in items:
        for m in merged:
            if m["param"] == it["param"] and m["what"] == it["what"] and it["lead_day"] == m["days"][-1] + 1 \
                    and overlaps(m["_last_bbox"], it["bbox"]):
                m["days"].append(it["lead_day"])
                m["window_end"] = it["window_end"]
                m["_last_bbox"] = it["bbox"]
                m["bbox"] = union(m["bbox"], it["bbox"])
                if abs(it["extreme"]) > abs(m["extreme"]):
                    m.update(extreme=it["extreme"], at=it["at"], peak_window_end=it["window_end"])
                m["area_km2"] = max(m["area_km2"], it["area_km2"])
                break
        else:
            merged.append({**it, "days": [it["lead_day"]], "peak_window_end": it["window_end"], "_last_bbox": it["bbox"]})
    for m in merged:
        for k in ("lead_day", "area_cells", "_last_bbox"):
            m.pop(k, None)
    return merged


def overlaps(a, b) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def union(a, b):
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def opendata(run, param, steps):
    with ThreadPoolExecutor(8) as ex:
        paths = list(ex.map(lambda s: fields.opendata_file(run, param, s, None), steps))
    return {s: fields.read_one(p) for s, p in zip(steps, paths) if p}


def local_minima(vals: np.ndarray, radius: int) -> np.ndarray:
    """Cells that are the minimum within +-radius cells (own code: no scipy minimum_filter)."""
    pad = np.pad(vals, ((radius, radius), (0, 0)), mode="edge")
    pad = np.concatenate([pad[:, -radius:], pad, pad[:, :radius]], axis=1)
    m = np.full_like(vals, np.inf)
    ny, nx = vals.shape
    for dy in range(2 * radius + 1):
        for dx in range(2 * radius + 1):
            m = np.minimum(m, pad[dy:dy + ny, dx:dx + nx])
    return vals <= m


def scan_lows(run: datetime) -> list[dict]:
    steps = list(range(0, 121, 12))
    data = opendata(run, "msl", steps)
    tracks: list[dict] = []
    for step, (vals, grid) in sorted(data.items()):
        v, g = vals[::4, ::4] / 100.0, fields.Grid(grid.lats[::4], grid.lons[::4])   # 1 deg is enough
        lat2 = np.repeat(g.lats[:, None], len(g.lons), axis=1)
        deep = np.where(np.abs(lat2) < 30, v < 1000, v < 975)
        deep &= lat2 > -70          # pressure reduced to sea level over Antarctica is unreliable
        for y, x in zip(*np.nonzero(local_minima(v, 4) & deep)):
            pt = (float(g.lats[y]), float(g.lons[x]), round(float(v[y, x]), 1))
            t = run + step * H
            for tr in tracks:
                la, lo, _ = tr["points"][-1][1:]
                if tr["last_step"] == step - 12 and abs(la - pt[0]) < 10 and abs(((lo - pt[1] + 180) % 360) - 180) < 15:
                    tr["points"].append((fields.iso(t), *pt))
                    tr["last_step"] = step
                    break
            else:
                tracks.append({"points": [(fields.iso(t), *pt)], "last_step": step})
    out = []
    for tr in tracks:
        pts = tr["points"]
        deepest = min(pts, key=lambda p: p[3])
        tropical = abs(deepest[1]) < 30
        lats = [p[1] for p in pts]
        lons = np.unwrap(np.radians([p[2] for p in pts]))
        lons = np.degrees(lons)
        pressures = [p[3] for p in pts]
        falls = [pressures[i] - pressures[i + 2] for i in range(len(pressures) - 2)]  # 24 h falls
        out.append({"signal": "low", "what": "possible tropical cyclone (model only)" if tropical else "deep extratropical low",
                    "min_mslp_hPa": deepest[3], "at": [deepest[1], deepest[2]], "time_of_min": deepest[0],
                    "max_24h_fall_hPa": round(max(falls), 1) if falls else None,
                    "track": [{"t": p[0], "lat": p[1], "lon": p[2], "mslp": p[3]} for p in pts],
                    "bbox": [round(min(lons) - 12, 1), round(min(lats) - 10, 1), round(max(lons) + 12, 1), round(max(lats) + 10, 1)],
                    "run": fields.iso(run)})
    for d in out:
        # Bergeron-style deepening rate normalised to 60 deg latitude (>= 1 is a "bomb")
        f = d["max_24h_fall_hPa"]
        d["bergeron"] = round(f * np.sin(np.radians(60)) / max(np.sin(np.radians(abs(d["at"][0]))), 0.2) / 24, 2) if f else None
    out.sort(key=lambda d: (d["what"].startswith("deep"), d["min_mslp_hPa"] if not d["what"].startswith("deep")
                            else -(d["bergeron"] or 0)))
    return out[:20]


def scan_rain(run: datetime, threshold: float) -> list[dict]:
    steps = list(range(0, 121, 24))
    data = opendata(run, "tp", steps)
    out = []
    for s0, s1 in zip(steps, steps[1:]):
        if s0 not in data or s1 not in data:
            continue
        (a, grid), (b, _) = data[s0], data[s1]
        tot = ((b - a) * 1000)[::2, ::2]
        g = fields.Grid(grid.lats[::2], grid.lons[::2])
        for cells in label(tot >= threshold):
            d = describe(cells, tot, g)
            out.append({"signal": "rain24", "what": "24 h precipitation", "units": "mm",
                        "window_start": fields.iso(run + s0 * H), "window_end": fields.iso(run + s1 * H),
                        "area_km2": round(area_km2(cells, g)), **d, "run": fields.iso(run)})
    out.sort(key=lambda d: -d["extreme"])
    return out[:25]


def scan_gusts(run: datetime, threshold: float) -> list[dict]:
    steps = list(range(3, 121, 3))
    data = opendata(run, "10fg", steps)
    if not data:
        return []
    grid = next(iter(data.values()))[1]
    stack = np.stack([v for v, _ in data.values()])
    mx = stack.max(axis=0)[::2, ::2]
    when = np.array(list(data.keys()))[stack.argmax(axis=0)][::2, ::2]
    g = fields.Grid(grid.lats[::2], grid.lons[::2])
    out = []
    for cells in label(mx >= threshold):
        d = describe(cells, mx, g)
        st = int(when[cells[:, 0], cells[:, 1]][np.argmax(mx[cells[:, 0], cells[:, 1]])])
        out.append({"signal": "gust", "what": "10 m wind gust", "units": "m/s",
                    "time_of_max": fields.iso(run + st * H), "area_km2": round(area_km2(cells, g)), **d,
                    "run": fields.iso(run)})
    out.sort(key=lambda d: -d["extreme"])
    return out[:25]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ens-threshold", type=float, default=50, help="probability %% (default 50)")
    ap.add_argument("--ens-min-area-km2", type=float, default=20000)
    ap.add_argument("--rain-mm", type=float, default=100)
    ap.add_argument("--gust-ms", type=float, default=35)
    ap.add_argument("--out")
    a = ap.parse_args()

    latest = fields.latest_run()
    run = latest if latest.hour in (0, 12) else latest - 6 * H   # 00/12 runs, for the longest steps
    sources, errors = {}, {}
    result = {"generated_utc": fields.iso(datetime.now(timezone.utc)), "hres_run": fields.iso(run)}
    for name, fn in [("ensemble", lambda: scan_ensemble(a.ens_threshold, a.ens_min_area_km2)),
                     ("lows", lambda: (scan_lows(run), {})),
                     ("rain24", lambda: (scan_rain(run, a.rain_mm), {})),
                     ("gusts", lambda: (scan_gusts(run, a.gust_ms), {}))]:
        try:
            items, src = fn()
            result[name] = items
            sources.update(src)
        except Exception as e:  # report and carry on with the other signals
            errors[name] = f"{type(e).__name__}: {e}"
            result[name] = []
    result["ensemble"] = sorted(result["ensemble"], key=lambda d: -d["extreme"] * d["area_km2"] ** 0.5)[:40]
    result["sources"] = {**sources, "hres": f"IFS HRES {fields.iso(run)} from ECMWF Open Data (CC BY 4.0)"}
    result["errors"] = errors
    text = json.dumps(result, indent=1)
    if a.out:
        Path(a.out).write_text(text)
        counts = {k: len(result[k]) for k in ("ensemble", "lows", "rain24", "gusts")}
        print(f"wrote {a.out}: {counts}; errors: {errors or 'none'}")
    else:
        print(text)


if __name__ == "__main__":
    main()
