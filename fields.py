"""Field data service: ECMWF Open Data cut to a lat/lon box and time window, ready for the phone.

Everything comes from ECMWF Open Data (free, CC BY 4.0), the same data an iPhone could fetch:
  * IFS HRES forecasts at 0.1 deg for zoomed-in views and 0.25 deg for wide ones.
  * IFS ENS probabilities (24 h rain and gust exceedance) for the "ensemble" layers.
Files are read from the AWS mirror (fastest) with data.ecmwf.int as fallback, fetching only the
fields needed via the .index files and HTTP range requests.

Each global field is downloaded once and cached on disk. A request returns frames resampled
to a grid that is regular in Web Mercator y, so the app can stretch each frame straight onto
an MKMapRect. Values are quantised to uint16 and base64-encoded to keep payloads small.
"""
from __future__ import annotations

import base64
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

try:  # use the operating system's certificate store (works behind TLS-inspecting proxies)
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

# On Replit the disk is temporary anyway, so cache in /tmp; locally keep it with the project.
CACHE = Path(os.environ.get("WEATHER_CACHE") or ("/tmp/weather-cache" if os.environ.get("REPL_ID")
                                                 else Path(__file__).parent / "data" / "cache"))
CACHE.mkdir(parents=True, exist_ok=True)
ROOTS = ["https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com", "https://data.ecmwf.int/forecasts"]
ATTRIBUTION = "Data: © {year} ECMWF, CC BY 4.0. Regridded and unit-converted."
UA = {"User-Agent": "weather-explorer-poc/0.2"}

H = timedelta(hours=1)


# --------------------------------------------------------------------------------------------
# Layer catalogue
# --------------------------------------------------------------------------------------------

@dataclass
class Layer:
    id: str
    title: str
    units: str
    params: list[str]                 # Open Data short names
    stops: list[tuple[float, str]]    # colour ramp: value -> #RRGGBBAA
    levelist: int | None = None
    contour_interval: float | None = None
    vectors: tuple[str, str] | None = None   # u/v params for arrows
    needs_interval: bool = False      # accumulated or "since previous post-processing" fields
    source: str = "oper"              # "oper" (HRES) or "ens" (ENS probabilities, 24 h windows)
    description: str = ""
    accumulated: bool = False         # accumulated since the run started: needs the previous step too
    categories: list[tuple[int, str, str]] | None = None   # categorical: (value, label, #RRGGBBAA)
    app_ready: bool = True            # False: built and shown on the web, but not yet allowed in
                                      # highlights (the current iPhone app can't rebuild it itself)


T = "#00000000"  # transparent

# Precipitation type: ECMWF code (table 4.201) x 10 + intensity (1 light < 1 mm/h <= 2 moderate
# < 4 mm/h <= 3 heavy); 0 = nothing falling (rate below 0.1 mm/h).
# Colours as in ECMWF's OpenCharts precipitation-type chart: darker when light, full strength when heavy.
PTYPE_KINDS = [(1, "Rain", ("#217c0ac0", "#30b50fe8", "#3ce213ff")),
               (3, "Freezing rain", ("#8c0229c0", "#cc033ce8", "#ff044bff")),
               (5, "Snow", ("#563983c0", "#7e53bee8", "#9d68eeff")),
               (6, "Wet snow", ("#26258ac0", "#3736c8e8", "#4543faff")),
               (7, "Sleet (rain and snow)", ("#11655bc0", "#199385e8", "#1fb8a6ff")),
               (8, "Ice pellets", ("#8c4600c0", "#cc6600e8", "#ff8000ff")),
               (12, "Freezing drizzle", ("#8a3d6ac0", "#c9589ae8", "#fb6ec0ff"))]
PTYPE = [(code * 10 + i, f"{name}, {level}", colours[i - 1])
         for code, name, colours in PTYPE_KINDS for i, level in ((1, "light"), (2, "moderate"), (3, "heavy"))]
PROB = [(5, T), (10, "#c6dbef66"), (25, "#fed976bb"), (50, "#fd8d3cdd"), (75, "#e31a1cee"), (95, "#ae017eff"), (100, "#49006aff")]
LAYERS: dict[str, Layer] = {l.id: l for l in [
    Layer("mslp", "Mean sea level pressure", "hPa", ["msl"],
          [(960, "#5b2a86cc"), (985, "#3f6fb5aa"), (1000, "#9ecae155"), (1013, T), (1025, "#fdae6b55"), (1040, "#d94801aa")],
          contour_interval=4, description="Isobars every 4 hPa. Lows, highs, fronts and cyclone structure."),
    Layer("wind10", "10 m wind speed", "m/s", ["10u", "10v"],
          [(0, T), (6, "#c6dbef66"), (10, "#6baed6aa"), (15, "#31a354cc"), (20, "#fed976dd"), (25, "#fd8d3cee"), (30, "#e31a1cff"), (40, "#ae017eff"), (50, "#ffffffff")],
          vectors=("10u", "10v"), description="Near-surface wind speed with direction arrows."),
    Layer("gust", "10 m wind gust", "m/s", ["10fg"],
          [(0, T), (15, "#c6dbef55"), (20, "#6baed6aa"), (25, "#fed976dd"), (30, "#fd8d3cee"), (35, "#e31a1cff"), (45, "#ae017eff"), (60, "#ffffffff")],
          needs_interval=True, description="Maximum gust in the interval ending at the valid time."),
    Layer("t2m", "2 m temperature", "°C", ["2t"],
          [(-40, "#3b0f70ee"), (-25, "#2c7fb8ee"), (-10, "#7fcdbbdd"), (0, "#edf8b1cc"), (10, "#fee391cc"), (20, "#fdae61dd"), (30, "#f46d43ee"), (40, "#a50026ff"), (48, "#4d0013ff")],
          description="Air temperature at 2 m."),
    Layer("precip", "Precipitation rate", "mm/h", ["tp"],
          [(0.1, T), (0.3, "#a6dba088"), (1, "#41b6c4cc"), (2, "#2c7fb8dd"), (5, "#fed976ee"), (10, "#fd8d3cff"), (20, "#e31a1cff"), (50, "#ae017eff")],
          needs_interval=True, accumulated=True, description="Mean precipitation rate over the interval ending at the valid time."),
    Layer("tcwv", "Total column water vapour", "kg/m²", ["tcwv"],
          [(15, T), (25, "#c7e9c066"), (35, "#74c476aa"), (45, "#fed976cc"), (55, "#fd8d3cdd"), (65, "#e31a1cee"), (75, "#7a0177ff")],
          description="Moisture plumes: atmospheric rivers and tropical moisture."),
    Layer("jet250", "Jet stream (250 hPa wind)", "m/s", ["u", "v"],
          [(25, T), (35, "#c6dbef77"), (45, "#6baed6bb"), (60, "#fed976dd"), (75, "#fd8d3cee"), (90, "#e31a1cff"), (110, "#ae017eff")],
          levelist=250, vectors=("u", "v"), description="Upper-level jet stream winds at 250 hPa."),
    Layer("z500", "500 hPa geopotential height", "dam", ["gh"],
          [(480, "#3b0f70bb"), (520, "#2c7fb8aa"), (552, "#7fcdbb88"), (568, "#fee39188"), (584, "#f46d43aa"), (600, "#a50026bb")],
          levelist=500, contour_interval=6, description="Mid-tropospheric troughs and ridges, contoured every 6 dam."),
    # Added October 2026 (iPhone app build 24 and later know them).
    Layer("wind850", "850 hPa wind", "m/s", ["u", "v"],
          [(10, T), (15, "#c6dbef77"), (20, "#6baed6bb"), (25, "#31a354cc"), (30, "#fed976dd"), (40, "#fd8d3cee"), (50, "#e31a1cff")],
          levelist=850, vectors=("u", "v"),
          description="Wind about 1.5 km up: low-level jets, warm conveyor belts and monsoon flow."),
    Layer("t850", "850 hPa temperature", "°C", ["t"],
          [(-40, "#3b0f70ee"), (-30, "#2c7fb8ee"), (-20, "#41b6c4dd"), (-10, "#a1dab4cc"), (0, "#ffffcc99"), (10, "#fed976cc"), (20, "#fd8d3cdd"), (30, "#e31a1cee")],
          levelist=850,
          description="Temperature about 1.5 km up, free of the daily cycle: air masses, cold outbreaks, warm sectors."),
    Layer("cape", "Storm energy (CAPE)", "J/kg", ["mucape"],
          [(250, T), (500, "#fee391aa"), (1000, "#fec44fcc"), (2000, "#fe9929dd"), (3000, "#ec7014ee"), (4000, "#cc4c02ff"), (6000, "#8c2d04ff")],
          description="Most-unstable convective available potential energy: the fuel for thunderstorms."),
    Layer("olr", "Cloud tops (satellite-like)", "W/m²", ["ttr"],
          [(100, "#f2f7ffff"), (140, "#ffffffee"), (180, "#ffffffbb"), (220, "#ffffff77"), (250, "#ffffff33"), (275, "#ffffff00")],
          needs_interval=True, accumulated=True,
          description="Outgoing longwave radiation: low values (white) are cold, high cloud tops, like an infrared satellite image."),
    Layer("ptype", "Precipitation type", "", ["ptype", "tprate"],
          [(0, T)] + [(v, c) for v, _, c in PTYPE],
          categories=PTYPE,
          description="Rain, snow, freezing rain, ice pellets and mixes, shaded light, moderate or heavy by rate."),
] + [
    Layer(f"ens_{k}", title, "%", [p], PROB, source="ens",
          description=f"IFS ENS probability of {what} in a 24 h window (windows every 12 h). High values mean the ensemble agrees on an unusual event.")
    for k, title, p, what in [("rain20", "Chance of >20 mm rain", "tpg20", "more than 20 mm of precipitation"),
                              ("rain50", "Chance of >50 mm rain", "tpg50", "more than 50 mm of precipitation"),
                              ("gust15", "Chance of gusts >15 m/s", "10fgg15", "gusts above 15 m/s (54 km/h)"),
                              ("gust25", "Chance of gusts >25 m/s", "10fgg25", "gusts above 25 m/s (90 km/h)")]
]}


APP_READY = {l.id for l in LAYERS.values() if l.app_ready}


def layer_catalogue() -> list[dict]:
    return [{"id": l.id, "title": l.title, "units": l.units, "description": l.description,
             "stops": l.stops, "contour_interval": l.contour_interval, "has_vectors": l.vectors is not None}
            for l in LAYERS.values()]


# --------------------------------------------------------------------------------------------
# Open Data access: index files + HTTP range requests, AWS first
# --------------------------------------------------------------------------------------------

def file_path(run: datetime, res: str, stream: str, type_: str, step: int, levtype: str) -> str:
    """Relative path (no extension). 0.1 deg files are split into -sfc and -pl."""
    suffix = f"-{levtype}" if res == "0p1" else ""
    return f"{run:%Y%m%d}/{run:%H}z/ifs/{res}/{stream}/{run:%Y%m%d%H}0000-{step}h-{stream}-{type_}{suffix}"


_index_cache: dict[str, list[dict]] = {}
_index_lock = threading.Lock()


def _get(url: str, headers: dict | None = None, timeout: int = 60, attempts: int = 5) -> bytes:
    """GET with retries: S3 answers bursts of range requests with 503 Slow Down."""
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={**UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or i == attempts - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if i == attempts - 1:
                raise
        time.sleep(0.4 * 2 ** i)
    raise IOError("unreachable")


def index_rows(rel: str) -> tuple[str, list[dict]] | None:
    """(root, index rows) for a file, trying the mirrors in order."""
    with _index_lock:
        if rel in _index_cache:
            return _index_cache[rel]
    for root in ROOTS:
        try:
            rows = [json.loads(l) for l in _get(f"{root}/{rel}.index", timeout=30).decode().splitlines() if l.strip()]
            with _index_lock:
                _index_cache[rel] = (root, rows)
            return root, rows
        except Exception:
            continue
    return None


def exists(rel: str) -> bool:
    for root in ROOTS:
        try:
            req = urllib.request.Request(f"{root}/{rel}.index", method="HEAD", headers=UA)
            with urllib.request.urlopen(req, timeout=15) as r:
                if r.status == 200:
                    return True
        except Exception:
            continue
    return False


_dl_locks: dict[Path, threading.Lock] = {}
_dl_guard = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    with _dl_guard:
        return _dl_locks.setdefault(path, threading.Lock())


def fetch_field(run: datetime, res: str, stream: str, type_: str, container: int, param: str,
                step: str, levelist: int | None = None) -> Path | None:
    """Download one GRIB message (cached). `container` is the file's step, `step` the message's."""
    lev = f"_{levelist}" if levelist else ""
    path = CACHE / res / f"{run:%Y%m%d%H}" / f"{stream}-{type_}" / f"{param}{lev}_{step.replace('-', 'to')}.grib2"
    with _lock_for(path):
        if path.exists():
            return path
        rel = file_path(run, res, stream, type_, container, "pl" if levelist else "sfc")
        found = index_rows(rel)
        if not found:
            print(f"opendata: no index for {rel}", file=sys.stderr)
            return None
        root, rows = found
        row = next((r for r in rows if r.get("param") == param and str(r.get("step")) == step
                    and (levelist is None or str(r.get("levelist")) == str(levelist))), None)
        if not row:
            print(f"opendata: {param}{lev} step {step} not in {rel}", file=sys.stderr)
            return None
        rng = f"bytes={row['_offset']}-{row['_offset'] + row['_length'] - 1}"
        for r0 in [root] + [r for r in ROOTS if r != root]:
            try:
                data = _get(f"{r0}/{rel}.grib2", {"Range": rng})
                if len(data) != row["_length"]:
                    raise IOError(f"short read {len(data)}/{row['_length']}")
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".part")
                tmp.write_bytes(data)
                tmp.rename(path)
                return path
            except Exception as e:
                print(f"opendata: {r0} {rel} {param}: {e}", file=sys.stderr)
        return None


def oper_field(run: datetime, res: str, param: str, step: int, levelist: int | None) -> Path | None:
    """An HRES field, falling back to the other resolution if this one isn't published yet."""
    return (fetch_field(run, res, "oper", "fc", step, param, str(step), levelist)
            or fetch_field(run, "0p25" if res == "0p1" else "0p1", "oper", "fc", step, param, str(step), levelist))


def opendata_file(run: datetime, param: str, step: int, levelist: int | None, res: str = "0p25") -> Path | None:
    """An HRES forecast field (kept for the highlights scan scripts)."""
    return fetch_field(run, res, "oper", "fc", step, param, str(step), levelist)


# --------------------------------------------------------------------------------------------
# Runs and steps
# --------------------------------------------------------------------------------------------

def native_steps(run_hour: int) -> list[int]:
    steps = list(range(0, 145, 3))
    if run_hour in (0, 12):
        steps += list(range(150, 361, 6))
    return steps


def floor6(t: datetime) -> datetime:
    return t.replace(hour=t.hour - t.hour % 6, minute=0, second=0, microsecond=0)


_latest: dict[str, tuple[float, datetime]] = {}
_latest_lock = threading.Lock()


def latest_run(kind: str = "oper") -> datetime:
    """Latest complete run on Open Data (cached 10 min). kind 'ens' needs the 00/12 ENS probabilities."""
    with _latest_lock:
        if kind in _latest and time.time() - _latest[kind][0] < 600:
            return _latest[kind][1]
    t = floor6(datetime.now(timezone.utc))
    for back in range(0, 8):
        run = t - back * 6 * H
        if kind == "ens":
            if run.hour not in (0, 12):
                continue
            rel = file_path(run, "0p25", "enfo", "ep", 360, "sfc")
        else:
            rel = file_path(run, "0p1", "oper", "fc", 144, "sfc")
        if exists(rel):
            with _latest_lock:
                _latest[kind] = (time.time(), run)
            return run
    raise RuntimeError("no recent run found on ECMWF Open Data")


def pick_source(t: datetime, latest: datetime, needs_interval: bool) -> tuple[datetime, int] | None:
    """Which (run, step) provides valid time t.

    Times after the latest run come from its forecast. Earlier times are filled from the
    short-range steps of the run just before them, giving a sequence close to the analysis.
    Interval fields (precipitation, gusts) need a step > 0.
    """
    if t > latest or (t == latest and not needs_interval):
        run = latest
        step = int((t - run) / H)
        if step > 144 and run.hour in (6, 18):
            run -= 6 * H
            step += 6
        return (run, step) if step in native_steps(run.hour) and (step > 0 or not needs_interval) else None
    run = floor6(t)
    if needs_interval and run == t:
        run -= 6 * H
    return run, int((t - run) / H)


def previous_step(run: datetime, step: int) -> int:
    steps = native_steps(run.hour)
    return steps[steps.index(step) - 1]


def frame_times(start: datetime, end: datetime, latest: datetime, max_frames: int) -> list[datetime]:
    """Valid times at the native cadence (3 h, 6 h beyond +144 h), thinned to max_frames."""
    times, t = [], start.replace(minute=0, second=0, microsecond=0)
    t += ((-t.hour) % 3) * H
    while t <= end:
        lead = (t - latest) / H
        if lead <= 144 or t.hour % 6 == 0:
            times.append(t)
        t += 3 * H
    if len(times) > max_frames:
        stride = math.ceil(len(times) / max_frames)
        times = times[::stride]
    return times


# --------------------------------------------------------------------------------------------
# Decoding
# --------------------------------------------------------------------------------------------

@dataclass
class Grid:
    lats: np.ndarray   # 1-D, descending
    lons: np.ndarray   # 1-D, ascending in [-180, 180)


_grids: dict[tuple, tuple[Grid, np.ndarray]] = {}


def read_fields(path: Path) -> list[tuple[dict, np.ndarray, Grid]]:
    """Decode every field in a GRIB file into (metadata, values[lat, lon], grid) with lon in [-180, 180)."""
    import earthkit.data as ekd
    out = []
    for f in ekd.from_source("file", str(path)).to_fieldlist():
        vals = f.to_numpy().astype(np.float32)
        key = vals.shape
        if key not in _grids:   # regular global grids: compute coordinates once per resolution
            lat2, lon2 = f.geography.latlons()
            lats, lons = lat2[:, 0], ((lon2[0, :] + 180) % 360) - 180
            order = np.argsort(lons)
            _grids[key] = (Grid(lats.astype(np.float64), lons[order].astype(np.float64)), order)
        grid, order = _grids[key]
        meta = {"var": f.get("parameter.variable"), "valid": f.time.valid_datetime().replace(tzinfo=timezone.utc),
                "base": f.time.base_datetime().replace(tzinfo=timezone.utc)}
        out.append((meta, vals[:, order], grid))
    return out


def read_one(path: Path) -> tuple[np.ndarray, Grid]:
    _, vals, grid = read_fields(path)[0]
    return vals, grid


# --------------------------------------------------------------------------------------------
# Resampling to the requested box (own numpy code: earthkit-geo has no Web Mercator target grid)
# --------------------------------------------------------------------------------------------

def merc_y(lat):
    lat = np.clip(lat, -85.0, 85.0)
    return np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))


def inv_merc_y(y):
    return np.degrees(2 * np.arctan(np.exp(y)) - np.pi / 2)


@dataclass
class Box:
    west: float
    south: float
    east: float     # may exceed 180 for boxes crossing the dateline
    north: float

    @classmethod
    def parse(cls, s: str) -> "Box":
        w, so, e, n = (float(x) for x in s.split(","))
        if e <= w:
            e += 360
        so, n = max(min(so, n), -85), min(max(so, n), 85)
        return cls(w, so, e, n)

    def target(self, max_px: int, native_res: float) -> tuple[np.ndarray, np.ndarray]:
        """Target lons (west->east) and lats (north->south, regular in Mercator y)."""
        nx = int(min(max_px, max(8, round((self.east - self.west) / native_res))))
        y0, y1 = merc_y(self.south), merc_y(self.north)
        aspect = (y1 - y0) / np.radians(self.east - self.west)
        ny = int(min(max_px, max(8, round(nx * aspect))))
        lons = self.west + (np.arange(nx) + 0.5) * (self.east - self.west) / nx
        ys = y1 - (np.arange(ny) + 0.5) * (y1 - y0) / ny
        return lons, inv_merc_y(ys)


def choose_resolution(box: Box, max_px: int) -> str:
    """0.1 deg once the box is zoomed in enough for it to show; 0.25 deg for wide views."""
    return "0p1" if (box.east - box.west) / max_px < 0.2 else "0p25"


def resample(vals: np.ndarray, grid: Grid, lons: np.ndarray, lats: np.ndarray) -> np.ndarray:
    """Bilinear interpolation of a regular global lat/lon field onto (lats x lons), touching only
    the needed points (cheap even for 0.1 deg global fields)."""
    ny, nx = vals.shape
    lon0, dlon = grid.lons[0], grid.lons[1] - grid.lons[0]
    lat0, dlat = grid.lats[0], grid.lats[1] - grid.lats[0]      # dlat < 0
    fx = ((lons - lon0) % 360) / dlon
    ix = np.floor(fx).astype(int)
    wx = (fx - ix)[None, :]
    ix0, ix1 = ix % nx, (ix + 1) % nx
    fy = np.clip((lats - lat0) / dlat, 0, ny - 1.000001)
    iy = np.floor(fy).astype(int)
    wy = (fy - iy)[:, None]
    v00 = vals[np.ix_(iy, ix0)]
    v01 = vals[np.ix_(iy, ix1)]
    v10 = vals[np.ix_(iy + 1, ix0)]
    v11 = vals[np.ix_(iy + 1, ix1)]
    return (v00 * (1 - wx) * (1 - wy) + v01 * wx * (1 - wy) + v10 * (1 - wx) * wy + v11 * wx * wy).astype(np.float32)


def resample_nearest(vals: np.ndarray, grid: Grid, lons: np.ndarray, lats: np.ndarray) -> np.ndarray:
    """Nearest grid point, for categorical fields (averaging category codes would be meaningless)."""
    ny, nx = vals.shape
    lon0, dlon = grid.lons[0], grid.lons[1] - grid.lons[0]
    lat0, dlat = grid.lats[0], grid.lats[1] - grid.lats[0]
    ix = np.rint(((lons - lon0) % 360) / dlon).astype(int) % nx
    iy = np.clip(np.rint((lats - lat0) / dlat).astype(int), 0, ny - 1)
    return vals[np.ix_(iy, ix)].astype(np.float32)


def encode(arr: np.ndarray, lo: float, hi: float) -> str:
    scale = (hi - lo) / 65534 if hi > lo else 1.0
    q = np.where(np.isfinite(arr), np.clip(np.round((arr - lo) / scale), 0, 65534), 65535).astype("<u2")
    return base64.b64encode(q.tobytes()).decode()


# --------------------------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------------------------

_pool = ThreadPoolExecutor(max_workers=8)


def _convert(layer: Layer, a: dict[str, np.ndarray], dt_hours: float) -> np.ndarray:
    if layer.id == "mslp":
        return a["msl"] / 100.0
    if layer.id in ("wind10", "jet250", "wind850"):
        u, v = layer.vectors
        return np.hypot(a[u], a[v])
    if layer.id == "t2m":
        return a["2t"] - 273.15
    if layer.id == "t850":
        return a["t"] - 273.15
    if layer.id == "precip":
        return np.maximum(a["tp"] - a["tp_prev"], 0) * 1000.0 / dt_hours
    if layer.id == "olr":
        # ttr is accumulated (J/m2, negative = outgoing): mean outgoing flux over the interval.
        return np.maximum(-(a["ttr"] - a["ttr_prev"]) / (dt_hours * 3600.0), 0)
    if layer.id == "ptype":
        rate = a["tprate"] * 3600.0                      # kg m-2 s-1 = mm/s -> mm/h
        level = np.where(rate >= 4, 3, np.where(rate >= 1, 2, np.where(rate >= 0.1, 1, 0)))
        code = np.rint(a["ptype"]).astype(int)
        return np.where((level > 0) & (code > 0), code * 10 + level, 0).astype(np.float32)
    if layer.id == "z500":
        return a["gh"] / 10.0
    return a[layer.params[0]]


def get_frames(layer_id: str, bbox: str, start: str | None, end: str | None, max_px: int = 320,
               max_frames: int = 40) -> dict:
    layer = LAYERS[layer_id]
    box = Box.parse(bbox)
    res = choose_resolution(box, max_px)
    native = 0.1 if res == "0p1" else 0.25
    if layer.source == "ens":
        return _ens_frames(layer, box, start, end, max_px, res, native)

    latest = latest_run()
    t0 = parse_time(start) if start else latest
    t1 = parse_time(end) if end else t0 + 72 * H
    t0 = max(t0, latest - 72 * H)          # Open Data keeps only the last few days
    t1 = min(t1, latest + 360 * H)
    times = frame_times(t0, t1, latest, max_frames)

    plan: list[tuple[datetime, datetime, int, int | None]] = []
    for t in times:
        src = pick_source(t, latest, layer.needs_interval)
        if not src:
            continue
        run, step = src
        prev = previous_step(run, step) if layer.accumulated else None
        plan.append((t, run, step, prev))
    futures = {}
    for i, (t, run, step, prev) in enumerate(plan):
        for p in layer.params:
            futures[(i, p)] = _pool.submit(oper_field, run, res, p, step, layer.levelist)
            if prev is not None and prev > 0:
                futures[(i, p + "_prev")] = _pool.submit(oper_field, run, res, p, prev, layer.levelist)

    lons, lats = box.target(max_px, native)
    vlons, vlats = (box.target(max(8, max_px // 6), native * 6) if layer.vectors else (None, None))
    frames, all_vals = [], []
    for i, (t, run, step, prev) in enumerate(plan):
        arrays, grid, ok = {}, None, True
        for key in [k for (j, k) in futures if j == i]:
            path = futures[(i, key)].result()
            if path is None:
                ok = False
                break
            arrays[key], grid = read_one(path)
        if not ok:
            continue
        if layer.accumulated:                       # step 0 has nothing accumulated yet
            for p in layer.params:
                arrays.setdefault(p + "_prev", np.zeros_like(arrays[p]))
        dt = (step - prev) if prev is not None else 1
        vals = (resample_nearest if layer.categories else resample)(_convert(layer, arrays, dt), grid, lons, lats)
        frame = {"valid_time": iso(t), "run": iso(run), "step": step, "_vals": vals}
        if layer.vectors:
            u, v = layer.vectors
            frame["_u"] = resample(arrays[u], grid, vlons, vlats)
            frame["_v"] = resample(arrays[v], grid, vlons, vlats)
        frames.append(frame)
        all_vals.append(vals)

    return _package(layer, box, lons, lats, frames, all_vals, latest, vlons, vlats, res=res)


def ens_windows(run: datetime, t0: datetime | None, t1: datetime | None) -> list[tuple[int, int]]:
    """24 h probability windows (start_h, end_h), every 12 h out to +360 h, overlapping [t0, t1]."""
    wins = [(e - 24, e) for e in range(24, 361, 12)]
    if t0 and t1:
        sel = [(s, e) for s, e in wins if run + e * H > t0 and run + s * H < t1]
        if sel:
            return sel
    return [(s, e) for s, e in wins if e <= 168]


def ens_field(run: datetime, res: str, param: str, s: int, e: int) -> Path | None:
    return fetch_field(run, res, "enfo", "ep", 240 if e <= 240 else 360, param, f"{s}-{e}")


def _ens_frames(layer: Layer, box: Box, start, end, max_px: int, res: str, native: float) -> dict:
    """ENS probability frames. ECMWF publishes the 0.25 and 0.1 deg probability files at slightly
    different times, so fall back to the other resolution, then to the previous run, until one is
    complete enough to use."""
    newest = latest_run("ens")
    t0, t1 = (parse_time(start) if start else None), (parse_time(end) if end else None)
    for run in (newest, newest - 12 * H, newest - 24 * H):
        for r in (res, "0p25" if res == "0p1" else "0p1"):
            wins = ens_windows(run, t0, t1)
            if not exists(file_path(run, r, "enfo", "ep", 240 if wins[0][1] <= 240 else 360, "sfc")):
                continue
            out = _ens_frames_for(layer, box, max_px, run, r, 0.1 if r == "0p1" else 0.25, wins)
            if out["frames"]:
                return out
    return _ens_frames_for(layer, box, max_px, newest, res, native, ens_windows(newest, t0, t1))


def _ens_frames_for(layer: Layer, box: Box, max_px: int, run: datetime, res: str, native: float,
                    wins: list[tuple[int, int]]) -> dict:
    futures = [(s, e, _pool.submit(ens_field, run, res, layer.params[0], s, e)) for s, e in wins]
    lons, lats = box.target(max_px, native)
    frames, all_vals = [], []
    for s, e, fut in futures:
        path = fut.result()
        if not path:
            continue
        vals, grid = read_one(path)
        if np.nanmax(vals) <= 1.0:      # probabilities may come as fractions
            vals = vals * 100
        r = resample(vals, grid, lons, lats)
        frames.append({"valid_time": iso(run + e * H), "run": iso(run), "step": e, "_vals": r,
                       "window": f"{iso(run + s * H)}/{iso(run + e * H)}"})
        all_vals.append(r)
    return _package(layer, box, lons, lats, frames, all_vals, run, None, None,
                    model="IFS ENS probabilities (Open Data)", res=res)


def _package(layer, box, lons, lats, frames, all_vals, run, vlons, vlats, model="IFS HRES (Open Data)", res="0p25") -> dict:
    stack = np.stack(all_vals) if all_vals else np.zeros((1, 1))
    lo, hi = float(np.nanmin(stack)), float(np.nanmax(stack))
    out_frames = []
    vec = None
    if vlons is not None and frames:
        uv = np.stack([np.stack([f["_u"], f["_v"]]) for f in frames])
        vmax = float(np.nanmax(np.abs(uv))) or 1.0
        vec = {"nx": len(vlons), "ny": len(vlats), "lo": -vmax, "hi": vmax}
    for f in frames:
        o = {k: v for k, v in f.items() if not k.startswith("_")}
        o["data"] = encode(f["_vals"], lo, hi)
        if vec:
            o["u"] = encode(f["_u"], vec["lo"], vec["hi"])
            o["v"] = encode(f["_v"], vec["lo"], vec["hi"])
        out_frames.append(o)
    return {
        "layer": layer.id, "title": layer.title, "units": layer.units, "model": model,
        "resolution": res.replace("0p", "0."),
        "run": iso(run) if run else None,
        "box": {"west": box.west, "south": box.south, "east": box.east, "north": box.north},
        "nx": len(lons), "ny": len(lats), "projection": "mercator",
        "lo": lo, "hi": hi, "stops": layer.stops, "contour_interval": layer.contour_interval,
        "vectors": vec, "frames": out_frames,
        **({"categories": [{"value": v, "label": lbl, "color": c} for v, lbl, c in layer.categories]}
           if layer.categories else {}),
        "attribution": ATTRIBUTION.format(year=datetime.now(timezone.utc).year),
    }


def prune_cache(max_age_hours: float) -> int:
    """Remove cached run folders (…/<res>/<YYYYMMDDHH>/) older than max_age_hours. Returns the count."""
    import shutil
    cutoff = datetime.now(timezone.utc) - max_age_hours * H
    removed = 0
    for res_dir in (CACHE / "0p1", CACHE / "0p25"):
        if not res_dir.is_dir():
            continue
        for run_dir in res_dir.iterdir():
            try:
                run = datetime.strptime(run_dir.name, "%Y%m%d%H").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if run < cutoff:
                shutil.rmtree(run_dir, ignore_errors=True)
                removed += 1
    return removed


def parse_time(s: str) -> datetime:
    t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
