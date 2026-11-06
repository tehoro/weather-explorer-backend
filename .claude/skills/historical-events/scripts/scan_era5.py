"""What ERA5 shows for a past event, hour by hour: the material for captions and the ERA5 column.

  uv run --frozen python scan_era5.py --bbox=W,S,E,N --start 2005-08-28T00:00Z --end 2005-08-30T12:00Z \
      [--places "New Orleans:29.95:-90.07;Biloxi:30.40:-88.89"] --out data/history/scan.json

Output (JSON; times UTC, pressure hPa, wind m/s, rain mm):
  window    {bbox, start, end}
  track     the main low (deepest in the box at the hour of lowest pressure), hour by hour:
            [{t, lat, lon, msl}] (backend tracks.py: the same track the viewer draws)
  lows      {t: [{lat, lon, msl}]}   every low below 1000 hPa in the box, deepest first
  gusts     {t: [{lat, lon, gust}]}  strongest gusts in the hour before t, up to 5, 200 km apart
  rain      {"totals": [{lat, lon, mm}], "hour": [{t, lat, lon, mm}]}   window totals; wettest hours
  places    {name: {lat, lon, hours: [{t, msl, wind, gust, rain}], lowest_msl, strongest_gust}}
  summary   lowest central pressure, strongest gust, wettest place, with times and places

Reads ERA5 (era5.py, Google's public ARCO-ERA5 store); about 5 MB an hour of window.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))   # the backend directory
import era5      # noqa: E402
import fields    # noqa: E402
import tracks    # noqa: E402

G = era5.GRID


def box_index(bbox):
    w, s, e, n = bbox
    rows = np.where((G.lats >= s) & (G.lats <= n))[0]
    cols = np.where(((G.lons - w) % 360) <= ((e - w) % 360 or 360))[0]
    return rows, cols


def km(lat1, lon1, lat2, lon2):
    return tracks._km(lat1, lon1, lat2, lon2)


def peaks(vals, rows, cols, n=5, apart_km=200, lowest=False, limit=None):
    """Up to n extreme points in the box, at least apart_km apart."""
    sub = vals[np.ix_(rows, cols)]
    order = np.argsort(sub, axis=None)
    if not lowest:
        order = order[::-1]
    out = []
    for k in order[:4000]:
        i, j = np.unravel_index(k, sub.shape)
        v = float(sub[i, j])
        if limit is not None and (v > limit if lowest else v < limit):
            break
        lat, lon = float(G.lats[rows[i]]), float(G.lons[cols[j]])
        if all(km(lat, lon, p["lat"], p["lon"]) >= apart_km for p in out):
            out.append({"lat": round(lat, 2), "lon": round(lon, 2), "v": round(v, 1)})
        if len(out) >= n:
            break
    return out


def lows_in(msl_hpa, rows, cols):
    """Closed lows below 1000 hPa: points lowest within about 300 km."""
    sub = msl_hpa[np.ix_(rows, cols)]
    out = []
    r = 6   # grid points (1.5 deg)
    for i, j in zip(*np.where(sub < 1000)):
        win = sub[max(0, i - r):i + r + 1, max(0, j - r):j + r + 1]
        if sub[i, j] <= win.min() and 0 < i < sub.shape[0] - 1 and 0 < j < sub.shape[1] - 1:
            out.append({"lat": round(float(G.lats[rows[i]]), 2), "lon": round(float(G.lons[cols[j]]), 2),
                        "msl": round(float(sub[i, j]), 1)})
    out.sort(key=lambda x: x["msl"])
    return out[:6]


def at(vals, lat, lon):
    return float(fields.resample(vals, G, np.array([lon]), np.array([lat]))[0, 0])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--places", default="")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    bbox = [float(x) for x in a.bbox.split(",")]
    t0, t1 = fields.parse_time(a.start), fields.parse_time(a.end)
    if (t1 - t0).total_seconds() > 7 * 86400:
        sys.exit("window longer than 7 days")
    times = [t0 + k * fields.H for k in range(int((t1 - t0) / fields.H) + 1)]
    places = []
    for p in filter(None, (x.strip() for x in a.places.split(";"))):
        name, lat, lon = p.rsplit(":", 2)
        places.append((name, float(lat), float(lon)))

    # Download everything first, in parallel (each field is cached by era5.py).
    params = ["msl", "10u", "10v", "10fg", "tp"]
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda job: era5.field(*job), [(p, t) for t in times for p in params]))
    rows, cols = box_index(bbox)
    iso = fields.iso

    lows, gusts, hour_rain, total = {}, {}, [], None
    place_hours = {name: [] for name, _, _ in places}
    for t in times:
        msl = era5.field("msl", t) / 100.0
        gust = era5.field("10fg", t)
        wind = np.hypot(era5.field("10u", t), era5.field("10v", t))
        tp = era5.field("tp", t) * 1000.0
        total = tp if total is None else total + tp
        lows[iso(t)] = lows_in(msl, rows, cols)
        gusts[iso(t)] = [{"lat": p["lat"], "lon": p["lon"], "gust": p["v"]} for p in peaks(gust, rows, cols, limit=15)]
        top = peaks(tp, rows, cols, n=1)
        if top and top[0]["v"] >= 1:
            hour_rain.append({"t": iso(t), "lat": top[0]["lat"], "lon": top[0]["lon"], "mm": top[0]["v"]})
        for name, lat, lon in places:
            place_hours[name].append({"t": iso(t), "msl": round(at(msl, lat, lon), 1), "wind": round(at(wind, lat, lon), 1),
                                      "gust": round(at(gust, lat, lon), 1), "rain": round(max(0.0, at(tp, lat, lon)), 1)})

    deepest = min(((iso(t), lo) for t in times for lo in lows[iso(t)][:1]), key=lambda x: x[1]["msl"], default=None)
    track = []
    if deepest:
        pts = tracks.follow(lambda t: (era5.field("msl", t), G), times, fields.parse_time(deepest[0]), bbox)
        track = [{"t": iso(p["t"]), "lat": round(p["lat"], 2), "lon": round(p["lon"], 2), "msl": round(p["msl"], 1)}
                 for p in pts]
    rain_totals = [{"lat": p["lat"], "lon": p["lon"], "mm": p["v"]} for p in peaks(total, rows, cols, n=5, limit=5)]
    hour_rain.sort(key=lambda x: -x["mm"])
    all_gusts = [(t, g) for t, gs in gusts.items() for g in gs]
    strongest = max(all_gusts, key=lambda x: x[1]["gust"], default=None)

    out = {
        "window": {"bbox": bbox, "start": iso(t0), "end": iso(t1)},
        "track": track,
        "lows": lows,
        "gusts": gusts,
        "rain": {"totals": rain_totals, "hour": hour_rain[:10]},
        "places": {name: {"lat": lat, "lon": lon, "hours": place_hours[name],
                          "lowest_msl": min(place_hours[name], key=lambda h: h["msl"]),
                          "strongest_gust": max(place_hours[name], key=lambda h: h["gust"]),
                          "rain_total": round(sum(h["rain"] for h in place_hours[name]), 1)}
                   for name, lat, lon in places},
        "summary": {
            "lowest_centre": min(track, key=lambda p: p["msl"]) if track else None,
            "strongest_gust": {"t": strongest[0], **strongest[1]} if strongest else None,
            "wettest_total": rain_totals[0] if rain_totals else None,
            "wettest_hour": hour_rain[0] if hour_rain else None,
            "notes": ("gusts are the maximum in the hour before t; rain totals cover the whole window; "
                      "positions are ERA5 grid points (0.25 deg) except the track, which is refined"),
        },
    }
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, indent=1))
    s = out["summary"]
    print(f"track: {len(track)} hours" + (f", lowest {s['lowest_centre']['msl']} hPa at {s['lowest_centre']['t']} "
          f"({s['lowest_centre']['lat']}, {s['lowest_centre']['lon']})" if track else ""))
    if strongest:
        print(f"strongest gust: {strongest[1]['gust']} m/s at {strongest[0]} ({strongest[1]['lat']}, {strongest[1]['lon']})")
    for name, p in out["places"].items():
        print(f"{name}: lowest {p['lowest_msl']['msl']} hPa at {p['lowest_msl']['t']}, "
              f"gust {p['strongest_gust']['gust']} m/s at {p['strongest_gust']['t']}, rain {p['rain_total']} mm")
    print(f"written {a.out}")


if __name__ == "__main__":
    main()
