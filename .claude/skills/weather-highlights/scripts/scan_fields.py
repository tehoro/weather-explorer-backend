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
  * explainers - textbook local winds at known places, for "explainer" highlights: Greenland tip
               jets, fall and gap winds (bora, mistral, piteraq, Tehuantepecer, Papagayo, Santa
               Ana, Antarctic katabatics) and low-level jets (Great Plains, Somali, South American,
               Caribbean), each when strong enough in IFS HRES, days 0-5.
  * convection - organised deep convection anywhere: CAPE >= 2500 J/kg with heavy rain falling.
  * freezing - freezing rain or freezing drizzle falling over a sizeable area.

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


# ---- Explainers: local winds at known places -------------------------------------------------
# (name, kind, box [w, s, e, n], wind-from sector [deg from, deg to], threshold m/s, level, note)
# level "10m" uses 10 m wind; "850" uses 850 hPa wind with the 10 m wind much weaker (a jet aloft).
LOCAL_WINDS = [
    ("Greenland tip jet", "tip jet", [-46, 57.5, -38, 61.5], (240, 320), 25, "10m",
     "Westerly flow forced round Cape Farewell accelerates into a narrow jet off Greenland's southern tip."),
    ("Greenland reverse tip jet", "tip jet", [-50, 57, -42, 61], (30, 120), 25, "10m",
     "Easterly flow round Cape Farewell: the 'reverse' (easterly) tip jet."),
    ("Piteraq (East Greenland)", "katabatic", [-38.5, 64.5, -35.5, 66.5], (270, 30), 20, "10m",
     "Cold air draining off the Greenland ice sheet into Ammassalik fjord: a violent katabatic wind."),
    ("Bora (Adriatic)", "fall wind", [13.5, 43.0, 16.5, 45.6], (0, 90), 15, "10m",
     "Cold north-easterly air spilling over the Dinaric Alps onto the Adriatic coast in strong gusts."),
    ("Mistral (Gulf of Lion)", "gap wind", [3.5, 41.5, 6.5, 43.3], (300, 30), 15, "10m",
     "Northerly air funnelled down the Rhône valley between the Alps and the Massif Central."),
    ("Novaya Zemlya bora", "fall wind", [52, 69.5, 58, 72.5], (45, 135), 20, "10m",
     "Cold air from the Kara Sea plunging over Novaya Zemlya's mountains onto the Barents Sea."),
    ("Tehuantepecer", "gap wind", [-96, 13.5, -94, 16], (315, 45), 15, "10m",
     "A cold surge from North America squeezed through the Chivela Pass onto the Gulf of Tehuantepec."),
    ("Papagayo jet", "gap wind", [-88.5, 10, -86, 12], (30, 120), 12, "10m",
     "Trade winds accelerated through the gap in Central America's mountains at Lake Nicaragua."),
    ("Santa Ana winds", "fall wind", [-119.5, 33.2, -117, 34.5], (0, 110), 12, "10m",
     "Dry offshore winds descending from the Great Basin through southern California's passes."),
    ("Adélie Land katabatic", "katabatic", [138, -68, 148, -65.5], (120, 210), 22, "10m",
     "Cold, dense air draining off the Antarctic plateau: some of the windiest coasts on Earth."),
    ("Terra Nova Bay katabatic", "katabatic", [162, -75.5, 166.5, -74], (230, 320), 22, "10m",
     "Katabatic outflow from the Antarctic plateau funnelled down Reeves Glacier."),
    ("Great Plains low-level jet", "low-level jet", [-102, 27, -94, 40], (135, 225), 18, "850",
     "A southerly jet a kilometre up at night, feeding Gulf moisture into Plains thunderstorms."),
    ("Somali (Findlater) jet", "low-level jet", [45, 0, 60, 15], (180, 270), 18, "850",
     "The cross-equatorial jet off East Africa that carries moisture into the Indian monsoon."),
    ("South American low-level jet", "low-level jet", [-66, -26, -58, -14], (315, 45), 18, "850",
     "A northerly jet east of the Andes carrying Amazon moisture towards the storms of the La Plata basin."),
    ("Caribbean low-level jet", "low-level jet", [-80, 11, -70, 16], (45, 135), 18, "850",
     "An easterly jet over the Caribbean Sea, strongest in July and February."),
]


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
                     ("freezing", lambda: (scan_freezing(run), {}))]:
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
        counts = {k: len(result[k]) for k in ("ensemble", "lows", "rain24", "gusts", "explainers", "convection", "freezing")}
        print(f"wrote {a.out}: {counts}; errors: {errors or 'none'}")
    else:
        print(text)


if __name__ == "__main__":
    main()
