"""ERA5 reanalysis for historical events, read from Google's public ARCO-ERA5 store.

Live and requested highlights use ECMWF Open Data only. ERA5 (Copernicus Climate Change Service,
openly licensed) is the agreed exception for past events: hourly, 0.25 deg, 37 pressure levels,
from 1940. The store is Zarr v2 on Google Cloud Storage, readable over plain HTTPS with no account:

  <STORE>/<variable>/<t>.0.0        one hour of a single-level field: float32 [721 lat, 1440 lon]
  <STORE>/<variable>/<t>.0.0.0      one hour of a pressure-level field: float32 [37, 721, 1440]

t counts hours since 1900-01-01 00 UTC. Chunks are Blosc (lz4) compressed, decoded with numcodecs
(the backend's optional "history" extra, so the Replit server doesn't need it). Latitudes run
90 to -90, longitudes 0 to 359.75; fields are returned on a fields.Grid with longitudes in
[-180, 180), like decoded Open Data.

Accumulations (tp, ttr) and "since previous post-processing" maxima (10fg) cover the hour ending at
the valid time; interval() sums or takes the maximum over longer windows. Each decoded single-level
field is cached as .npy (about 4 MB each) in ERA5_CACHE, by default the system's temporary folder
(never the project, which may be in a synced folder); ERA5_CACHE=off keeps only the last few in
memory. Pressure levels (levels()) are never cached: a 3D build reads each hour once, and caching
them would take gigabytes.
"""
from __future__ import annotations

import functools
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import fields

STORE = "https://storage.googleapis.com/gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3"
EPOCH = datetime(1900, 1, 1, tzinfo=timezone.utc)
FIRST = datetime(1940, 1, 1, tzinfo=timezone.utc)
_where = os.environ.get("ERA5_CACHE", "")
CACHE = None if _where == "off" else Path(_where or Path(tempfile.gettempdir()) / "weather-explorer-era5")
MODEL = "ERA5 reanalysis"
ATTRIBUTION = ("Contains modified Copernicus Climate Change Service information {year}. "
               "ERA5: Hersbach et al. (2020). Regridded and unit-converted.")

# Open Data short names (as used in the layer catalogue) -> ARCO-ERA5 variable names.
VARIABLES = {
    "msl": "mean_sea_level_pressure",
    "10u": "10m_u_component_of_wind",
    "10v": "10m_v_component_of_wind",
    "10fg": "10m_wind_gust_since_previous_post_processing",
    "2t": "2m_temperature",
    "tp": "total_precipitation",
    "tcwv": "total_column_water_vapour",
    "mucape": "convective_available_potential_energy",   # ERA5 has surface-based CAPE, not most-unstable
    "ttr": "top_net_thermal_radiation",
    "ptype": "precipitation_type",
    "tprate": "mean_total_precipitation_rate",
    "u": "u_component_of_wind",
    "v": "v_component_of_wind",
    "t": "temperature",
    "gh": "geopotential",
    "q": "specific_humidity",
    "lsm": "land_sea_mask",
    "zs": "geopotential_at_surface",   # the model's terrain (m2/s2; divide by G for metres)
}
PRESSURE = {"u", "v", "t", "gh", "q"}
LEVELS = [1, 2, 3, 5, 7, 10, 20, 30, 50, 70, 100, 125, 150, 175, 200, 225, 250, 300, 350, 400, 450, 500,
          550, 600, 650, 700, 750, 775, 800, 825, 850, 875, 900, 925, 950, 975, 1000]
SUMMED = {"tp", "ttr"}     # hourly accumulations
MAXIMA = {"10fg"}          # hourly maxima
G = 9.80665

GRID = fields.Grid(np.linspace(90, -90, 721), np.arange(-720, 720) * 0.25)
_ORDER = np.r_[720:1440, 0:720]          # 0..359.75 -> -180..179.75

_locks: dict[Path, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(path: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(path, threading.Lock())


def hour_index(t: datetime) -> int:
    return int((t - EPOCH).total_seconds() // 3600)


def field(param: str, t: datetime, level: int | None = None) -> np.ndarray:
    """One hourly global field on GRID, in the store's units (geopotential converted to metres)."""
    if t < FIRST:
        raise ValueError(f"ERA5 starts in 1940, not {t:%Y}")
    if param in PRESSURE:
        if level not in LEVELS:
            raise ValueError(f"{param}: no ERA5 level {level}")
        return levels(param, t, [level])[level]
    if CACHE is None:
        return _download(param, t)
    path = CACHE / param / f"{t:%Y%m%d%H}.npy"
    with _lock(path):
        if path.exists():
            return np.load(path)
        vals = _download(param, t)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part.npy")
        np.save(tmp, vals)
        tmp.replace(path)
        return vals


@functools.lru_cache(maxsize=48)
def _download(param: str, t: datetime) -> np.ndarray:
    """A single-level field from the store (the last few are kept in memory)."""
    from numcodecs import Blosc
    raw = Blosc().decode(fields._get(f"{STORE}/{VARIABLES[param]}/{hour_index(t)}.0.0", timeout=120))
    return np.frombuffer(raw, "<f4").reshape(721, 1440)[:, _ORDER].astype(np.float32)


def levels(param: str, t: datetime, wanted: list[int]) -> dict[int, np.ndarray]:
    """Several pressure levels of one hourly field from a single download of its 37-level chunk
    (about 85 MB). Not cached: ask for every level needed at once."""
    from numcodecs import Blosc
    raw = Blosc().decode(fields._get(f"{STORE}/{VARIABLES[param]}/{hour_index(t)}.0.0.0", timeout=300))
    cube = np.frombuffer(raw, "<f4").reshape(len(LEVELS), 721, 1440)
    out = {}
    for lev in wanted:
        vals = cube[LEVELS.index(lev)][:, _ORDER].astype(np.float32)
        out[lev] = vals / G if param == "gh" else vals
    return out


def relative_humidity(q: np.ndarray, t: np.ndarray, p_hpa: float) -> np.ndarray:
    """Relative humidity (%) from specific humidity and temperature, over water above 0 °C, over
    ice below -23 °C and mixed between, as the IFS does."""
    e = q * p_hpa / (0.622 + 0.378 * q)
    water = 6.1121 * np.exp(17.502 * (t - 273.16) / (t - 32.19))
    ice = 6.1121 * np.exp(22.587 * (t - 273.16) / (t + 0.7))
    a = np.clip((t - 250.16) / 23.0, 0, 1) ** 2
    return 100 * e / (a * water + (1 - a) * ice)


def interval(param: str, t: datetime, hours: int) -> np.ndarray:
    """Sum (accumulations) or maximum (gusts) over the `hours` hours ending at t."""
    parts = [field(param, t - k * fields.H) for k in range(hours)]
    if param in SUMMED:
        return np.sum(parts, axis=0, dtype=np.float32)
    if param in MAXIMA:
        return np.max(parts, axis=0)
    raise ValueError(f"{param} is not accumulated")
