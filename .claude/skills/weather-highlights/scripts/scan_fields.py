#!/usr/bin/env python3
"""Objective scan of ECMWF fields for unusual or severe weather, worldwide.

All inputs are ECMWF Open Data (no MARS), so the same scan could run anywhere.

Signals (each is evidence for Claude to weigh, not a verdict):
  * ensemble - IFS ENS probability regions over land and coasts: chance of >50 mm precipitation
               and of gusts >25 m/s in daily 24 h windows, days 1-10, >= threshold.
  * low      - deep surface lows in IFS HRES (Open Data), tracked crudely across 12-hourly steps.
               Tropical lows (|lat| < 30) are flagged as possible tropical cyclones. Lows north of
               75N (970 hPa or below; 985 in May-September) and Southern Ocean lows of 935 hPa or
               below get a `polar` label.
  * rain24   - 24 h precipitation totals >= 100 mm in IFS HRES.
  * gust     - 10 m gusts >= 35 m/s in IFS HRES (3-hourly maxima, days 0-5).
  * explainers - textbook local winds at known places, for "explainer" highlights: Greenland tip
               jets, fall and gap winds (bora, mistral, piteraq, Tehuantepecer, Papagayo, Santa
               Ana, Antarctic katabatics) and low-level jets (Great Plains, Somali, South American,
               Caribbean), each when strong enough in IFS HRES, days 0-5.
               Those shown as explainers in the last two sweeps are marked `recently_shown`,
               and `recent_explainers` lists them.
  * convection - organised deep convection anywhere: CAPE >= 2500 J/kg with heavy rain falling.
  * freezing - freezing rain or freezing drizzle falling over a sizeable area.
  * stratosphere - the polar vortices: zonal-mean wind at 10 hPa and 60 degrees (westerly round the
               vortex; easterly = reversed, the test for a major sudden stratospheric warming),
               polar-cap temperature and pole-minus-60 temperature at 10 hPa, 12-hourly and as daily
               means to +360 h (with the first of two easterly days in a row), NASA Ozone Watch's
               analysis and 1979-onwards climatology for each day, the vortex's centres daily to
               +240 h, plus the last three days' analyses.
               Each hemisphere gets a `status` for the sweep (start, continue, end or none) from
               these thresholds and whether the previous feed had a polar-vortex item, and, when
               flagged, the share of the 50 ENS members with reversed winds.
  * polar    - polar lows (small, intense maritime lows in cold air over open sea: sea surface
               at least 43 K warmer than 500 hPa, gale-force wind, 6-hourly to +72 h) and warm,
               moist intrusions into the polar regions (Arctic Ocean near or above freezing in
               the cold season, melt at Greenland's summit, a warm spell on the Antarctic plateau).

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
import explainers  # noqa: E402  (local winds, and which explainers recent sweeps showed)
from explainers import LOCAL_WINDS  # noqa: E402

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


ARCTIC_SUMMER = {5, 6, 7, 8, 9}


def scan_lows(run: datetime) -> list[dict]:
    steps = list(range(0, 121, 12))
    data = opendata(run, "msl", steps)
    tracks: list[dict] = []
    arctic = 985 if run.month in ARCTIC_SUMMER else 975   # summer Arctic cyclones are weaker but matter for sea ice
    for step, (vals, grid) in sorted(data.items()):
        v, g = vals[::4, ::4] / 100.0, fields.Grid(grid.lats[::4], grid.lons[::4])   # 1 deg is enough
        lat2 = np.repeat(g.lats[:, None], len(g.lons), axis=1)
        deep = np.where(np.abs(lat2) < 30, v < 1000, np.where(lat2 >= 70, v < arctic, v < 975))
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
        lat, p = d["at"][0], d["min_mslp_hPa"]
        if lat >= 75 and p <= (985 if run.month in ARCTIC_SUMMER else 970):
            d["polar"] = "Arctic cyclone (deep low over the Arctic Ocean side; can break up and move sea ice)"
        elif lat <= -50 and p <= 935:
            d["polar"] = "extreme Southern Ocean low (935 hPa or below: among the deepest lows on Earth)"
    out.sort(key=lambda d: (d["what"].startswith("deep"), d["min_mslp_hPa"] if not d["what"].startswith("deep")
                            else -(d["bergeron"] or 0)))
    return out[:20] + [d for d in out[20:] if d.get("polar")]


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


# ---- Explainers: local winds at known places -------------------------------------------------
# (name, kind, box [w, s, e, n], wind-from sector [deg from, deg to], threshold m/s, level, note)
# level "10m" uses 10 m wind; "850" uses 850 hPa wind with the 10 m wind much weaker (a jet aloft).
def _from_dir(u: np.ndarray, v: np.ndarray) -> np.ndarray:
    return (np.degrees(np.arctan2(-u, -v)) + 360) % 360       # meteorological: where it blows from


def _in_sector(d: np.ndarray, sector: tuple[float, float]) -> np.ndarray:
    a, b = sector
    return (d >= a) & (d <= b) if a <= b else (d >= a) | (d <= b)


def _box_cells(grid: fields.Grid, box) -> tuple[np.ndarray, np.ndarray]:
    w, s_, e, n = box
    rows = np.nonzero((grid.lats >= s_) & (grid.lats <= n))[0]
    lon = ((grid.lons - w) % 360)
    cols = np.nonzero(lon <= (e - w) % 360)[0]
    return rows, cols


def _fetch_all(run, jobs):
    """Download (param, step, levelist) fields in parallel; returns {job: path}."""
    with ThreadPoolExecutor(8) as ex:
        paths = list(ex.map(lambda j: fields.opendata_file(run, j[0], j[1], j[2]), jobs))
    return dict(zip(jobs, paths))


def scan_local_winds(run: datetime) -> list[dict]:
    """Textbook local winds at the places above, when strong enough: 10 m wind 6-hourly over days
    0-5; 850 hPa wind 12-hourly at 06 and 18 UTC (night over the Americas, when their jets peak)."""
    steps = list(range(0, 121, 6))
    steps850 = {s for s in steps if (run.hour + s) % 24 in (6, 18)}
    jobs = [(p, s, None) for s in steps for p in ("10u", "10v")] + [(p, s, 850) for s in sorted(steps850) for p in ("u", "v")]
    paths = _fetch_all(run, jobs)
    best: dict[str, dict] = {}
    for step in steps:
        try:
            u10, grid = fields.read_one(paths[("10u", step, None)])
            v10, _ = fields.read_one(paths[("10v", step, None)])
            u85 = v85 = None
            if step in steps850:
                u85, _ = fields.read_one(paths[("u", step, 850)])
                v85, _ = fields.read_one(paths[("v", step, 850)])
        except Exception:
            continue
        for name, kind, box, sector, thr, level, note in LOCAL_WINDS:
            if level == "850" and u85 is None:
                continue
            rows, cols = _box_cells(grid, box)
            sub = np.ix_(rows, cols)
            s10 = np.hypot(u10[sub], v10[sub])
            if level == "850":
                u, v = u85[sub], v85[sub]
                spd = np.hypot(u, v)
                ok = (spd >= thr) & (spd >= 1.8 * s10)        # strong aloft, much weaker at the surface
            else:
                u, v = u10[sub], v10[sub]
                spd = s10
                ok = spd >= thr
            ok &= _in_sector(_from_dir(u, v), sector)
            if not ok.any():
                continue
            i = np.argmax(np.where(ok, spd, -1))
            y, x = np.unravel_index(i, spd.shape)
            t = fields.iso(run + step * H)
            b = best.setdefault(name, {"signal": "explainer", "phenomenon": name, "kind": kind, "note": note,
                                       "level": "850 hPa wind (with weak surface wind)" if level == "850" else "10 m wind",
                                       "first": t, "steps": 0, "peak_ms": 0.0, "units": "m/s"})
            b["last"] = t
            b["steps"] += 1
            if spd[y, x] > b["peak_ms"]:
                b.update(peak_ms=round(float(spd[y, x]), 1), time_of_peak=t,
                         at=[round(float(grid.lats[rows[y]]), 2), round(float(grid.lons[cols[x]]), 2)],
                         from_deg=int(_from_dir(u[y, x], v[y, x])))
            w, s_, e, n = box
            b["scene_bbox"] = [w - 8, s_ - 6, e + 8, n + 6]
            b["run"] = fields.iso(run)
    return sorted(best.values(), key=lambda d: -d["peak_ms"])


def _dilate(mask: np.ndarray, r: int) -> np.ndarray:
    out = mask.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            out |= np.roll(np.roll(mask, dy, axis=0), dx, axis=1)
    return out


def scan_convection(run: datetime, cape: float = 2000, rate: float = 10, min_area_km2: float = 30000) -> list[dict]:
    """Organised deep convection anywhere (tropics and both hemispheres): clusters of heavy rain
    (>= rate mm/h, cells within ~50 km joined) at least min_area_km2 across, with CAPE >= cape J/kg
    in or beside them (storms use up the CAPE where they rain). 12-hourly over days 0-3."""
    steps = list(range(12, 73, 12))
    paths = _fetch_all(run, [(p, s, None) for s in steps for p in ("mucape", "tprate")])
    out = []
    for step in steps:
        try:
            c, grid = fields.read_one(paths[("mucape", step, None)])
            r, _ = fields.read_one(paths[("tprate", step, None)])
        except Exception:
            continue
        c, r = c[::2, ::2], r[::2, ::2] * 3600
        g = fields.Grid(grid.lats[::2], grid.lons[::2])
        for cells in label(_dilate(r >= rate, 1)):
            a = area_km2(cells, g)
            if a < min_area_km2:
                continue
            region = np.zeros_like(r, bool)
            region[cells[:, 0], cells[:, 1]] = True
            near = _dilate(region, 2)                         # within about 100 km
            peak_cape = float(c[near].max())
            if peak_cape < cape:
                continue
            d = describe(cells, r, g)
            out.append({"signal": "convection", "what": "organised deep convection (heavy rain with high CAPE)",
                        "units": "mm/h", "valid": fields.iso(run + step * H), "area_km2": round(a), **d,
                        "peak_cape_j_kg": round(peak_cape),
                        "band": "tropics" if abs(d["at"][0]) < 23.5 else ("northern hemisphere" if d["at"][0] > 0 else "southern hemisphere"),
                        "run": fields.iso(run)})
    out.sort(key=lambda d: -d["area_km2"])
    return out[:15]


def scan_freezing(run: datetime, min_area_km2: float = 5000) -> list[dict]:
    """Freezing rain or freezing drizzle falling (precipitation type 3 or 12, >= 0.1 mm/h) over
    land and coasts (not Antarctica), 6-hourly, days 0-3."""
    steps = list(range(0, 73, 6))
    paths = _fetch_all(run, [(p, s, None) for s in steps for p in ("ptype", "tprate")])
    out, land = [], None
    for step in steps:
        try:
            t, grid = fields.read_one(paths[("ptype", step, None)])
            r, _ = fields.read_one(paths[("tprate", step, None)])
        except Exception:
            continue
        if land is None:
            land = land_and_coast(grid)
        code, rr = np.rint(t), r * 3600
        mask = np.isin(code, (3, 12)) & (rr >= 0.1) & land
        for cells in label(mask):
            a = area_km2(cells, grid)
            if a < min_area_km2:
                continue
            d = describe(cells, rr, grid)
            kinds = sorted({"freezing rain" if k == 3 else "freezing drizzle" for k in code[cells[:, 0], cells[:, 1]].tolist()})
            out.append({"signal": "freezing", "what": " and ".join(kinds), "units": "mm/h", "valid": fields.iso(run + step * H),
                        "area_km2": round(a), **d, "run": fields.iso(run)})
    out.sort(key=lambda d: -d["area_km2"])
    return out[:15]


# ---- Stratosphere: the polar vortices -------------------------------------------------------
# A reversal (easterly zonal-mean wind at 10 hPa, 60 deg) in the forecast starts a polar-vortex
# item; in midwinter, when the vortex is normally strongest, a very weak vortex (< WEAK) does too.
# The item continues every sweep until the wind is back above WEAK in the last three days'
# analyses and the whole forecast: entering needs a reversal, leaving needs a recovery, so one
# wobbly run doesn't make it come and go. Reversals in spring are the vortex's normal final warming.
WEAK = 10.0                                            # m/s
SEASON = {"north": {"breakdown": {11, 12, 1, 2, 3}, "midwinter": {12, 1, 2}, "final": {4, 5}},
          "south": {"breakdown": {5, 6, 7, 8, 9}, "midwinter": {6, 7, 8}, "final": {10, 11, 12}}}
VORTEX_PREFIX = "polar-vortex-"                        # item ids: polar-vortex-north-<winter>, ...


def _zonal_u60(vals: np.ndarray, grid: fields.Grid, hemi: str) -> float:
    return float(vals[int(np.argmin(np.abs(grid.lats - (60 if hemi == "north" else -60))))].mean())


def _cap_mean(vals: np.ndarray, grid: fields.Grid, hemi: str) -> float:
    rows = grid.lats >= 60 if hemi == "north" else grid.lats <= -60
    w = np.cos(np.radians(grid.lats[rows]))
    return float((vals[rows].mean(axis=1) * w).sum() / w.sum())


def _ens_u60(run: datetime, step: int) -> list[tuple[float, float]]:
    """10 hPa zonal-mean wind at 60N and 60S for each of the 50 ENS members at one step."""
    path = fields.CACHE / "0p25" / f"{run:%Y%m%d%H}" / "enfo-ef" / f"u_10_{step}_members.grib2"
    if not path.exists():
        rel = fields.file_path(run, "0p25", "enfo", "ef", step, "pl")
        found = fields.index_rows(rel)
        if not found:
            return []
        root, rows = found
        rows = [r for r in rows if r.get("param") == "u" and str(r.get("levelist")) == "10"]
        def part(r):                                   # each member, from the other mirror if S3 refuses
            rng = {"Range": f"bytes={r['_offset']}-{r['_offset'] + r['_length'] - 1}"}
            roots = [root] + [x for x in fields.ROOTS if x != root]
            for i, r0 in enumerate(roots):
                try:
                    return fields._get(f"{r0}/{rel}.grib2", rng)
                except Exception:
                    if i == len(roots) - 1:
                        raise
        with ThreadPoolExecutor(4) as ex:
            parts = list(ex.map(part, rows))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.with_suffix(".part").write_bytes(b"".join(parts))
        path.with_suffix(".part").rename(path)
    return [(_zonal_u60(v, g, "north"), _zonal_u60(v, g, "south")) for _, v, g in fields.read_fields(path)]


def _previous_vortex_items() -> dict[str, str]:
    """Polar-vortex items in the feed on show, by hemisphere: {"north": id, ...}."""
    try:
        items = json.loads(explainers.FEED.read_text()).get("items", [])
    except (OSError, ValueError):
        return {}
    return {h: it["id"] for it in items for h in ("north", "south")
            if str(it.get("id", "")).startswith(f"{VORTEX_PREFIX}{h}")}


def _pole_minus_60(vals: np.ndarray, grid: fields.Grid, hemi: str) -> float:
    """Zonal-mean temperature at the pole minus at 60 deg (K). Above zero, with the wind reversed,
    is the temperature half of the WMO definition of a major warming."""
    sign = 1 if hemi == "north" else -1
    row = lambda lat: int(np.argmin(np.abs(grid.lats - lat)))  # noqa: E731
    return float(vals[row(89.75 * sign)].mean() - vals[row(60 * sign)].mean())


def _vortex_centres(vals: np.ndarray, grid: fields.Grid, hemi: str, k: int = 3) -> dict:
    """Where the vortex is: up to k separate lows of 10 hPa height poleward of 45 deg (lobes, deepest
    first, at least 20 deg of arc apart and in the lowest quarter of heights there) and the highest
    height (the anticyclone that pushes it). On a 1 deg subsample, so it is cheap."""
    rows = grid.lats >= 45 if hemi == "north" else grid.lats <= -45
    z = vals[rows][::4, ::4] / 10.0                         # dam
    lats, lons = grid.lats[rows][::4], grid.lons[::4]
    mins = local_minima(z, 12) & (z <= np.percentile(z, 25))
    found = []
    for j, i in sorted(zip(*np.nonzero(mins)), key=lambda c: z[c]):
        lat, lon = float(lats[j]), float(lons[i])
        if all(_arc(lat, lon, f[0], f[1]) >= 20 for f in found):
            found.append((lat, lon, float(z[j, i])))
        if len(found) == k:
            break
    j, i = np.unravel_index(int(np.argmax(z)), z.shape)
    return {"lows": [{"lat": round(a, 1), "lon": round(o, 1), "dam": round(d)} for a, o, d in found],
            "high": {"lat": round(float(lats[j]), 1), "lon": round(float(lons[i]), 1), "dam": round(float(z[j, i]))}}


def _arc(lat1, lon1, lat2, lon2) -> float:
    p1, p2, dl = np.radians(lat1), np.radians(lat2), np.radians(lon2 - lon1)
    return float(np.degrees(np.arccos(np.clip(np.sin(p1) * np.sin(p2) + np.cos(p1) * np.cos(p2) * np.cos(dl), -1, 1))))


# NASA Ozone Watch publishes the MERRA-2 / GEOS FP analysis of the same two quantities with their
# 1979-onwards climatology (minimum, percentiles, maximum by date), one file per July-June season.
NASA = "https://ozonewatch.gsfc.nasa.gov/meteorology/figures/merra2"
NASA_FILES = {"north": ("wind/u60n_10_{y}_merra2.txt", "temperature/t60_90n_10_{y}_merra2.txt"),
              "south": ("wind/u60s_10_{y}_merra2.txt", "temperature/t60_90s_10_{y}_merra2.txt")}
NASA_COLS = ("data", "min", "p10", "p30", "mean", "p70", "p90", "max", "fcst")


def _nasa(hemi: str, now: datetime) -> tuple[dict, dict]:
    """{date: row} for the wind and the cap temperature (K), or empty dicts if NASA can't be read."""
    years = [now.year if now.month >= 7 else now.year - 1] if hemi == "north" else [now.year, now.year - 1]
    out = []
    for path in NASA_FILES[hemi]:
        rows = {}
        for y in years:
            try:
                text = fields._get(f"{NASA}/{path.format(y=y)}", timeout=30, attempts=2).decode()
            except Exception:
                continue
            for line in text.splitlines():
                p = line.split()
                if len(p) == 10 and p[0][:4].isdigit() and "-" in p[0]:
                    rows[p[0]] = dict(zip(NASA_COLS, (float(x) for x in p[1:])))
            if rows:
                break
        out.append(rows)
    return out[0], out[1]


def _rank(v: float, c: dict) -> str:
    """Where a value sits in NASA's climatology for its date."""
    if v < c["min"]:
        return "below the record minimum"
    if v > c["max"]:
        return "above the record maximum"
    for lo, hi, name in (("min", "p10", "lowest 10 %"), ("p10", "p30", "10-30th percentile"), ("p30", "p70", "near normal"),
                         ("p70", "p90", "70-90th percentile")):
        if v <= c[hi]:
            return name
    return "highest 10 %"


def _climate(day: dict, wind: dict, temp: dict) -> None:
    """Adds NASA's climatology for the day (u60 and cap T) to a daily entry."""
    w, t = wind.get(day["date"]), temp.get(day["date"])
    if w:
        day["u60_normal_ms"] = round(w["mean"], 1)
        day["u60_record_min_max_ms"] = [round(w["min"], 1), round(w["max"], 1)]
        day["u60_rank"] = _rank(day["u60_ms"], w)
    if t and day.get("cap_t_C") is not None:
        k = day["cap_t_C"] + 273.15
        day["cap_t_anomaly_K"] = round(k - t["mean"], 1)
        day["cap_t_record_min_max_C"] = [round(t["min"] - 273.15, 1), round(t["max"] - 273.15, 1)]
        day["cap_t_rank"] = _rank(k, t)


def scan_stratosphere(run: datetime) -> dict:
    """Twice-daily zonal-mean wind at 60 deg, cap temperature and pole-minus-60 temperature to +360 h,
    as daily means (a single snapshot can dip below zero for six hours), the vortex's centres daily
    to +240 h, and NASA's analysis and climatology for context."""
    steps = [s for s in range(0, 361, 12) if s in fields.native_steps(run.hour)]
    zsteps = [s for s in steps if s % 24 == 0 and s <= 240]
    jobs = [(p, s, 10) for p in ("u", "t") for s in steps] + [("gh", s, 10) for s in zsteps]
    past = [run - d * H for d in (24, 48, 72)]             # analyses of the last three days
    with ThreadPoolExecutor(8) as ex:
        past_paths = list(ex.map(lambda r: fields.opendata_file(r, "u", 0, 10), past))
    paths = _fetch_all(run, jobs)
    decoded = {j: fields.read_one(p) for j, p in paths.items() if p}
    analyses = [fields.read_one(p) for p in past_paths if p]
    previous = _previous_vortex_items()
    newest = fields.latest_run()
    out = {"run": fields.iso(run), "weak_threshold_ms": WEAK, "previous_items": previous,
           "animation_note": (f"Scenes show the newest run ({fields.iso(newest)}) to +144 h and this run after it; "
                              "numbers in captions come from check_scenes.py, not from this series.")
           if newest != run else None}
    for hemi in ("north", "south"):
        series = []
        for s in steps:
            if ("u", s, 10) not in decoded:
                continue
            r = {"t": fields.iso(run + s * H), "u60_ms": round(_zonal_u60(*decoded[("u", s, 10)], hemi), 1)}
            if ("t", s, 10) in decoded:
                r["cap_t_C"] = round(_cap_mean(*decoded[("t", s, 10)], hemi) - 273.15, 1)
                r["pole_minus_60_K"] = round(_pole_minus_60(*decoded[("t", s, 10)], hemi), 1)
            series.append(r)
        if not series:
            continue
        daily: dict[str, list[dict]] = {}
        for r in series:
            daily.setdefault(r["t"][:10], []).append(r)
        days = []
        for d, rs in daily.items():
            e = {"date": d, "u60_ms": round(float(np.mean([r["u60_ms"] for r in rs])), 1)}
            for k in ("cap_t_C", "pole_minus_60_K"):
                if all(k in r for r in rs):
                    e[k] = round(float(np.mean([r[k] for r in rs])), 1)
            days.append(e)
        try:
            wind, temp = _nasa(hemi, run)
        except Exception:
            wind, temp = {}, {}
        for e in days:
            _climate(e, wind, temp)
        recent = [round(_zonal_u60(v, g, hemi), 1) for v, g in analyses] + [series[0]["u60_ms"]]
        low = min(series, key=lambda r: r["u60_ms"])
        month = fields.parse_time(low["t"]).month
        season = SEASON[hemi]
        reversed_ = [r for r in series if r["u60_ms"] < 0]
        flag = None
        if reversed_ and month in season["breakdown"]:
            flag = "reversal"
        elif low["u60_ms"] < WEAK and month in season["midwinter"]:
            flag = "very weak"
        recovered = min(recent + [r["u60_ms"] for r in series]) >= WEAK
        had = previous.get(hemi)
        status = ("continue" if not recovered else "end") if had else ("start" if flag else "none")
        # The reversal date to quote: the first of at least two days in a row with an easterly daily mean.
        onset = next((a["date"] for a, b in zip(days, days[1:]) if a["u60_ms"] < 0 and b["u60_ms"] < 0), None)
        warm = next((a["date"] for a, b in zip(days, days[1:])
                     if a.get("pole_minus_60_K", -1) > 0 and b.get("pole_minus_60_K", -1) > 0), None)
        h = {"status": status, "flag": flag, "min_u60_ms": low["u60_ms"], "min_at": low["t"],
             "first_easterly": reversed_[0]["t"] if reversed_ else None,
             "reversal_onset_day": onset, "easterly_days": sum(1 for e in days if e["u60_ms"] < 0),
             "pole_warmer_than_60_from": warm,
             "recent_analyses_u60_ms": recent, "daily": days, "series": series}
        latest_nasa = max((d for d, r in wind.items() if r["fcst"] == 0 and r["data"] > -9000), default=None)
        if latest_nasa:
            r = wind[latest_nasa]
            h["nasa_latest_analysis"] = {"date": latest_nasa, "u60_ms": round(r["data"], 1),
                                         "normal_ms": round(r["mean"], 1), "record_min_ms": round(r["min"], 1),
                                         "rank": _rank(r["data"], r), "source": "NASA Ozone Watch (MERRA-2 / GEOS FP), 1979 onwards"}
        elif reversed_ or low["u60_ms"] < WEAK:
            h["nasa_error"] = "NASA Ozone Watch files not read; climatology context unavailable"
        h["centres"] = [{"t": fields.iso(run + s * H), **_vortex_centres(*decoded[("gh", s, 10)], hemi)}
                        for s in zsteps if ("gh", s, 10) in decoded]
        if reversed_ and month in season["final"]:
            h["note"] = "reversal in spring: the vortex's normal final warming, not a breakdown story"
        if series[0].get("cap_t_C") is not None:
            h["cap_t_rise_C"] = round(max(r.get("cap_t_C", -999) for r in series) - series[0]["cap_t_C"], 1)
        out[hemi] = h
    # When a story is on, the ensemble's view: the share of members with reversed winds round the
    # deterministic minimum (three steps, 50 members each, only then: about 150 small downloads).
    for hemi in ("north", "south"):
        h = out.get(hemi)
        if not h or h["status"] not in ("start", "continue"):
            continue
        k = int((fields.parse_time(h["min_at"]) - run) / H)
        ens = []
        for step in sorted({min(360, max(24, k + d)) for d in (-48, 0, 48)}):
            try:
                members = [u[0 if hemi == "north" else 1] for u in _ens_u60(run, step)]
            except Exception as e:                     # keep the status; say the ensemble is missing
                h.setdefault("ensemble_errors", []).append(f"+{step} h: {type(e).__name__}: {e}")
                continue
            if members:
                q = np.percentile(members, [10, 50, 90])
                ens.append({"t": fields.iso(run + step * H), "members": len(members),
                            "pct_easterly": round(100 * sum(u < 0 for u in members) / len(members)),
                            "u60_p10_p50_p90_ms": [round(float(x), 1) for x in q]})
        h["ensemble"] = ens
    return out


# ---- Polar weather: polar lows and warm intrusions -------------------------------------------
POLAR_LOW_BANDS = {"north": (50, 82), "south": (-75, -45)}
POLAR_LOW_MONTHS = {"north": {10, 11, 12, 1, 2, 3, 4}, "south": {4, 5, 6, 7, 8, 9, 10}}
POLAR_LOW_CONTRAST = 43.0     # K, sea surface minus 500 hPa temperature (a standard polar-low criterion)
POLAR_LOW_WIND = 15.0         # m/s, 10 m wind near the centre


def _window(a: np.ndarray, y: int, x: int, r: int) -> np.ndarray:
    """Cells within +-r of (y, x), wrapping in longitude."""
    rows = slice(max(0, y - r), y + r + 1)
    cols = np.arange(x - r, x + r + 1) % a.shape[1]
    return a[rows][:, cols]


def scan_polar_lows(run: datetime) -> list[dict]:
    """Small closed lows over open sea in very cold air, with gale-force wind: polar-low candidates.
    Lows that are the lowest point for 8 degrees around are synoptic cyclones, not polar lows."""
    month = run.month
    hemis = [h for h in POLAR_LOW_BANDS if month in POLAR_LOW_MONTHS[h]]
    if not hemis:
        return []
    steps = list(range(0, 73, 6))
    jobs = [(p, s, None) for s in steps for p in ("msl", "skt", "10u", "10v")] + [("t", s, 500) for s in steps]
    paths = _fetch_all(run, jobs + [("lsm", 0, None)])
    lsm, _ = fields.read_one(paths[("lsm", 0, None)])
    tracks: list[dict] = []
    for step in steps:
        try:
            msl, grid = fields.read_one(paths[("msl", step, None)])
            skt, _ = fields.read_one(paths[("skt", step, None)])
            t5, _ = fields.read_one(paths[("t", step, 500)])
            u, _ = fields.read_one(paths[("10u", step, None)])
            v, _ = fields.read_one(paths[("10v", step, None)])
        except Exception:
            continue
        msl, spd, contrast = msl / 100.0, np.hypot(u, v), skt - t5
        for h in hemis:
            lo, hi = POLAR_LOW_BANDS[h]
            rows = np.nonzero((grid.lats >= lo) & (grid.lats <= hi))[0]
            sub = msl[rows]
            for y, x in zip(*np.nonzero(local_minima(sub, 6))):
                Y = rows[y]
                if lsm[Y, x] >= 0.5 or _window(msl, Y, x, 8).mean() - msl[Y, x] < 2:
                    continue                                   # over land, or too shallow to be a closed low
                if msl[Y, x] <= _window(msl, Y, x, 32).min():
                    continue                                   # the lowest for 8 degrees around: a synoptic low
                c, w = float(_window(contrast, Y, x, 4).max()), float(_window(spd, Y, x, 4).max())
                if c < POLAR_LOW_CONTRAST or w < POLAR_LOW_WIND:
                    continue
                pt = {"t": fields.iso(run + step * H), "lat": float(grid.lats[Y]), "lon": float(grid.lons[x]),
                      "mslp": round(float(msl[Y, x]), 1), "wind_ms": round(w, 1), "contrast_K": round(c, 1)}
                for tr in tracks:
                    last = tr["points"][-1]
                    if tr["last_step"] == step - 6 and abs(last["lat"] - pt["lat"]) < 3 \
                            and abs(((last["lon"] - pt["lon"] + 180) % 360) - 180) < 8:
                        tr["points"].append(pt)
                        tr["last_step"] = step
                        break
                else:
                    tracks.append({"points": [pt], "last_step": step})
    out = []
    for tr in tracks:
        pts = tr["points"]
        if len(pts) < 2:                                       # seen at two steps at least (6 h)
            continue
        peak = max(pts, key=lambda p: p["wind_ms"])
        lats = [p["lat"] for p in pts]
        lons = np.degrees(np.unwrap(np.radians([p["lon"] for p in pts])))
        out.append({"signal": "polar_low", "what": "possible polar low (model only)", "first": pts[0]["t"],
                    "last": pts[-1]["t"], "peak_wind_ms": peak["wind_ms"], "time_of_peak": peak["t"],
                    "at": [peak["lat"], peak["lon"]], "min_mslp_hPa": min(p["mslp"] for p in pts),
                    "max_contrast_K": max(p["contrast_K"] for p in pts), "track": pts,
                    "bbox": [round(min(lons) - 10, 1), round(max(-89, min(lats) - 5), 1),
                             round(max(lons) + 10, 1), round(min(89, max(lats) + 5), 1)],
                    "run": fields.iso(run)})
    out.sort(key=lambda d: -d["peak_wind_ms"])
    return out[:8]


# Warm, moist intrusions: (name, months, mask rule, threshold on the 2 m temperature in C, note).
# Fixed thresholds stand in for anomalies, as Open Data has no climatology; each is far above the
# region's normal for the season. The Antarctic plateau is where surface pressure is below 700 hPa.
WARM_REGIONS = [
    ("Central Arctic Ocean", {11, 12, 1, 2, 3}, "lat>=85", -5.0,
     "Normally about -25 to -35 C in the cold season: warm, moist air reaching the pole from the Atlantic or Pacific."),
    ("Central Arctic Ocean", {10, 4}, "lat>=85", 0.0,
     "Above freezing near the pole outside summer: a warm-air intrusion."),
    ("Greenland ice sheet summit", set(range(1, 13)), "summit", 0.0,
     "Above freezing at about 3200 m on top of the Greenland ice sheet: surface melt where it is rare."),
    ("Antarctic plateau", {4, 5, 6, 7, 8, 9}, "plateau", -30.0,
     "Normally about -55 to -70 C in winter: an atmospheric river or warm intrusion onto the high ice sheet."),
    ("Antarctic plateau", {10, 11, 12, 1, 2, 3}, "plateau", -10.0,
     "Unusually warm for the high ice sheet even in its warmer season."),
]
SUMMIT = [-41, 71.5, -35, 73.5]                        # Summit Station, 72.6N 38.5W


def scan_polar_warmth(run: datetime) -> list[dict]:
    """The warmest 2 m temperature in each polar region every 12 h to +240 h, flagged when it passes
    the region's threshold for the month, with the most water vapour (tcwv) there at that time."""
    regions = [r for r in WARM_REGIONS if run.month in r[1]]
    steps = list(range(0, 241, 12))
    paths = _fetch_all(run, [(p, s, None) for s in steps for p in ("2t", "tcwv")] + [("sp", 0, None)])
    sp, grid = fields.read_one(paths[("sp", 0, None)])
    lat2 = np.repeat(grid.lats[:, None], len(grid.lons), axis=1)
    lon2 = np.repeat(grid.lons[None, :], len(grid.lats), axis=0)
    lon2 = ((lon2 + 180) % 360) - 180
    masks = {"lat>=85": lat2 >= 85, "plateau": (lat2 < -65) & (sp < 70000),
             "summit": (lon2 >= SUMMIT[0]) & (lon2 <= SUMMIT[2]) & (lat2 >= SUMMIT[1]) & (lat2 <= SUMMIT[3])}
    best = {}
    for step in steps:
        try:
            t2, _ = fields.read_one(paths[("2t", step, None)])
            q, _ = fields.read_one(paths[("tcwv", step, None)])
        except Exception:
            continue
        t2 = t2 - 273.15
        for name, _, rule, thr, note in regions:
            m = masks[rule]
            i = np.argmax(np.where(m, t2, -np.inf))
            y, x = np.unravel_index(i, t2.shape)
            val = float(t2[y, x])
            b = best.setdefault(name, {"signal": "polar_warmth", "region": name, "threshold_C": thr, "note": note,
                                       "max_t2m_C": -999.0, "steps_above": 0, "run": fields.iso(run)})
            if val >= thr:
                b["steps_above"] += 1
                b.setdefault("first_above", fields.iso(run + step * H))
            if val > b["max_t2m_C"]:
                b.update(max_t2m_C=round(val, 1), time_of_max=fields.iso(run + step * H),
                         at=[round(float(grid.lats[y]), 2), round(float(lon2[y, x]), 2)],
                         median_t2m_C=round(float(np.median(t2[m])), 1), max_tcwv_kg_m2=round(float(q[m].max()), 1))
    for b in best.values():
        b["flag"] = b["steps_above"] > 0
    return sorted(best.values(), key=lambda b: (not b["flag"], b["region"]))


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
                     ("gusts", lambda: (scan_gusts(run, a.gust_ms), {})),
                     ("explainers", lambda: (scan_local_winds(run), {})),
                     ("convection", lambda: (scan_convection(run), {})),
                     ("freezing", lambda: (scan_freezing(run), {})),
                     ("stratosphere", lambda: (scan_stratosphere(run), {})),
                     ("polar_lows", lambda: (scan_polar_lows(run), {})),
                     ("polar_warmth", lambda: (scan_polar_warmth(run), {}))]:
        try:
            items, src = fn()
            result[name] = items
            sources.update(src)
        except Exception as e:  # report and carry on with the other signals
            errors[name] = f"{type(e).__name__}: {e}"
            result[name] = {} if name == "stratosphere" else []
    result["ensemble"] = sorted(result["ensemble"], key=lambda d: -d["extreme"] * d["area_km2"] ** 0.5)[:40]
    shown = explainers.recent()
    for x in result["explainers"]:
        r = explainers.shown_recently(x["phenomenon"], explainers.CORE.get(x["phenomenon"]), shown)
        if r:
            x["recently_shown"] = f"explainer in the {r['sweep']} sweep: {r['title']}"
    result["recent_explainers"] = shown
    result["sources"] = {**sources, "hres": f"IFS HRES {fields.iso(run)} from ECMWF Open Data (CC BY 4.0)"}
    result["errors"] = errors
    text = json.dumps(result, indent=1)
    if a.out:
        Path(a.out).write_text(text)
        counts = {k: len(result[k]) for k in ("ensemble", "lows", "rain24", "gusts", "explainers", "convection", "freezing", "polar_lows")}
        counts["polar_warmth_flags"] = sum(1 for b in result["polar_warmth"] if b.get("flag"))
        vortex = {h: result["stratosphere"][h]["status"] for h in ("north", "south") if h in result["stratosphere"]}
        print(f"wrote {a.out}: {counts}; polar vortex: {vortex}; errors: {errors or 'none'}")
    else:
        print(text)


if __name__ == "__main__":
    main()
