#!/usr/bin/env python3
"""Check a draft's captions against the frames each scene will really show.

Usage (from the backend directory):
  uv run --frozen python .claude/skills/weather-highlights/scripts/check_scenes.py data/highlights/draft.json
  uv run --frozen python .claude/skills/weather-highlights/scripts/check_scenes.py --item vortex data/highlights/draft.json

The scene builder fetches each scene from the newest run on Open Data (06/18 UTC runs to +144 h,
then the 00/12 UTC run), so the frames often come from a newer run than the scan read. Captions,
places and numbers must match these frames. For every scene this prints its frames (times, runs,
spacing), and for every caption:

  - the frame it appears on (and whether its time is between frames or after the last one);
  - the fill's value at each focus place, and the strongest value within 150 km (with distance and
    compass direction from the place);
  - the scene's maximum and minimum, with where they are relative to the nearest focus place;
  - for `mslp` / `z10hpa` / `z500` contours, the lowest centre;
  - for the 10 hPa layers, the zonal-mean wind at 60 deg, the polar-cap mean temperature and the
    pole-minus-60 deg temperature difference of that frame's run and step;
  - each number in the caption with the fill's unit that the frame doesn't contain (FLAG);
  - a caption whose numbers describe a much weaker spot than the frame's strongest colours (NOTE).

For a categorical fill (`ptype`) values are printed as their names (e.g. "Snow, moderate").

A FLAG line is a likely mismatch between the text and the animation: fix the caption (or the scene)
and run again. Values are from the same box and resolution the viewer shows, without its margin.
Downloads are cached, so the scene build afterwards reuses them. Needs no keys.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import sys
from pathlib import Path

import numpy as np

BACKEND = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(BACKEND))
import fields  # noqa: E402

PX = 400                                   # the scene builder's fill size
NEAR_KM = 150
CENTRES = {"mslp", "z10hpa", "z500"}
STRAT = {"z10hpa", "t10hpa", "wind10hpa"}
COMPASS = "N NNE NE ENE E ESE SE SSE S SSW SW WSW W WNW NW NNW".split()


def km(lat1, lon1, lat2, lon2):
    p1, p2, dl = np.radians(lat1), np.radians(lat2), np.radians(lon2 - lon1)
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 6371 * 2 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def bearing(lat1, lon1, lat2, lon2) -> str:
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    b = math.degrees(math.atan2(math.sin(dl) * math.cos(p2),
                                math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)))
    return COMPASS[int(((b % 360) + 11.25) // 22.5) % 16]


def grid(pkg: dict) -> tuple[np.ndarray, np.ndarray]:
    """2-D lons and lats of a package's cells, as fields.py lays them out."""
    nx, ny = pkg["nx"], pkg["ny"]
    if pkg.get("projection") == "polar":
        x0, x1, y0, y1 = pkg["extent"]
        X, Y = np.meshgrid(x0 + (np.arange(nx) + 0.5) * (x1 - x0) / nx, y1 - (np.arange(ny) + 0.5) * (y1 - y0) / ny)
        rho, pole = np.hypot(X, Y), pkg["pole"]
        lats = pole * (90 - np.degrees(2 * np.arcsin(np.clip(rho / 2, 0, 1))))
        lats[rho > 2] = np.nan
        return np.degrees(np.arctan2(X, -pole * Y)), lats
    b = pkg["box"]
    lons = b["west"] + (np.arange(nx) + 0.5) * (b["east"] - b["west"]) / nx
    y0, y1 = fields.merc_y(b["south"]), fields.merc_y(b["north"])
    lats = fields.inv_merc_y(y1 - (np.arange(ny) + 0.5) * (y1 - y0) / ny)
    return np.meshgrid(((lons + 180) % 360) - 180, lats)


def values(pkg: dict, frame: dict) -> np.ndarray:
    q = np.frombuffer(base64.b64decode(frame["data"]), "<u2").astype(np.float64)
    scale = (pkg["hi"] - pkg["lo"]) / 65534 if pkg["hi"] > pkg["lo"] else 1.0
    v = pkg["lo"] + q * scale
    v[q == 65535] = np.nan
    return v.reshape(pkg["ny"], pkg["nx"])


def where(lat, lon, focus) -> str:
    if not focus:
        return f"{lat:.1f},{lon:.1f}"
    f = min(focus, key=lambda f: km(lat, lon, f["lat"], f["lon"]))
    d = float(km(f["lat"], f["lon"], lat, lon))
    rel = "at" if d < 25 else f"{d:.0f} km {bearing(f['lat'], f['lon'], lat, lon)} of"
    return f"{lat:.1f},{lon:.1f} ({rel} {f['label']})"


def fmt(v: float) -> str:
    return f"{v:.1f}" if abs(v) < 100 else f"{v:.0f}"


# Layers where "more" is what the eye goes to (bright colours), so a caption quoting a much weaker
# value than the frame's peak is describing something the viewer won't pick out.
PEAK_UNITS = {"mm/h", "mm", "m/s", "%", "J/kg", "kg/m²"}


def show(v: float, unit: str, cats: dict[int, str]) -> str:
    if cats:
        return cats.get(int(round(v)), "nothing falling") if np.isfinite(v) else "no data"
    return f"{fmt(v)} {unit}".rstrip()


def unit_numbers(text: str, unit: str) -> list[float]:
    if not unit:
        return []
    u = re.escape(unit).replace("°", "°?\\s?")
    out = []
    for m in re.finditer(rf"([−\-–]?\d+(?:\.\d+)?)(?:\s*[–\-]\s*(\d+(?:\.\d+)?))?\s*{u}(?![\w/])", text):
        a = float(m.group(1).replace("−", "-").replace("–", "-"))
        out.append(a)
        if m.group(2):
            out.append(float(m.group(2)))
    return out


def stratosphere(run: str, step: int, hemi: int) -> str:
    """Zonal-mean u at 60 deg, polar-cap mean T and pole-minus-60 T for one run and step."""
    r = fields.parse_time(run)
    pu, pt = fields.oper_field(r, "0p25", "u", step, 10), fields.oper_field(r, "0p25", "t", step, 10)
    if not pu or not pt:
        return "10 hPa diagnostics: fields missing"
    u, g = fields.read_one(pu)
    t, _ = fields.read_one(pt)
    row = lambda lat: int(np.argmin(np.abs(g.lats - lat)))  # noqa: E731
    u60 = float(u[row(60 * hemi)].mean())
    cap = g.lats * hemi >= 60
    w = np.cos(np.radians(g.lats[cap]))
    capt = float((t[cap].mean(axis=1) * w).sum() / w.sum()) - 273.15
    dpole = float(t[row(89.75 * hemi)].mean() - t[row(60 * hemi)].mean())
    return f"10 hPa: zonal-mean u at {60 * hemi:+d}° {u60:+.1f} m/s, cap mean T {capt:.1f} °C, pole minus 60° {dpole:+.1f} K"


def check_scene(it: dict, n: int, sc: dict, flags: list[str], notes: list[str]) -> None:
    name = f"{it['id']} scene {n}"
    fill = sc.get("fill")
    print(f"\n  SCENE {n}: {sc.get('title')}  [{fill}{' + ' + sc['contours'] if sc.get('contours') else ''}]")
    if fill not in fields.LAYERS:
        print("    unknown fill layer")
        return
    polar = fields.scene_polar_box(sc["bbox"], 0)
    w, s, e, nn = sc["bbox"]
    box = polar or f"{w},{s},{e if e > w else e + 360},{nn}"
    count = fields.scene_frame_count(sc.get("start", ""), sc.get("end", ""))
    pkg = fields.get_frames(fill, box, sc.get("start"), sc.get("end"), PX, count)
    frames = pkg.get("frames") or []
    if not frames:
        print("    NO FRAMES (window outside the data?)")
        flags.append(f"{name}: no frames")
        return
    lons, lats = grid(pkg)
    times = [f["valid_time"] for f in frames]
    ts = [fields.parse_time(t) for t in times]
    gaps = sorted({int((b - a).total_seconds() // 3600) for a, b in zip(ts, ts[1:])})
    runs = sorted({f"{f['run'][8:10]} {f['run'][11:13]}z" for f in frames if f.get("run")})
    print(f"    {len(frames)} frames {times[0]} -> {times[-1]} (end asked {sc.get('end')}), every {gaps} h, runs {runs}")
    cont = None
    if sc.get("contours") in CENTRES and sc["contours"] != fill:
        cont = fields.get_frames(sc["contours"], box, sc.get("start"), sc.get("end"), 200, count)
    elif fill in CENTRES:
        cont = pkg
    focus = sc.get("focus") or []
    caps = sorted(sc.get("captions") or [], key=lambda c: c.get("at", ""))
    for ci, c in enumerate(caps):
        t = fields.parse_time(c["at"])
        later = [i for i, x in enumerate(ts) if x >= t]
        if not later:
            print(f"    CAPTION {c['at']} NEVER SHOWN (after the last frame): {c['text']}")
            flags.append(f"{name}: caption {c['at']} is after the last frame {times[-1]}")
            continue
        k = later[0]
        note = "" if ts[k] == t else f"  ** between frames: first shows at {times[k]}"
        if ts[k] != t:
            flags.append(f"{name}: caption {c['at']} is between frames (shows at {times[k]})")
        f = frames[k]
        until = c.get("until") or (caps[ci + 1]["at"] if ci + 1 < len(caps) else times[-1])
        print(f"    CAPTION {c['at']} -> frame {times[k]} ({f.get('run', '')[:13]}Z run +{f.get('step')} h), "
              f"on screen until {until}{note}")
        print(f"      \"{c['text']}\"")
        v = values(pkg, f)
        ok = np.isfinite(v) & np.isfinite(lats)
        if not ok.any():
            continue
        unit = pkg.get("units", "")
        cats = {c[0]: c[1] for c in (fields.LAYERS[fill].categories or [])}
        vmax, vmin = np.nanmax(np.where(ok, v, np.nan)), np.nanmin(np.where(ok, v, np.nan))
        jmax, jmin = np.nanargmax(np.where(ok, v, -np.inf)), np.nanargmin(np.where(ok, v, np.inf))
        if not cats:
            print(f"      max {fmt(vmax)} {unit} at {where(lats.flat[jmax], lons.flat[jmax], focus)}; "
                  f"min {fmt(vmin)} {unit} at {where(lats.flat[jmin], lons.flat[jmin], focus)}")
        for fo in focus:
            d = km(fo["lat"], fo["lon"], lats, lons)
            d = np.where(ok, d, np.inf)
            here = v.flat[int(np.argmin(d))]
            near = ok & (d <= NEAR_KM)
            if near.any():
                j = int(np.nanargmax(np.where(near, v, -np.inf)))
                dist = float(d.flat[j])
                rel = "" if dist < 25 else f" {dist:.0f} km {bearing(fo['lat'], fo['lon'], lats.flat[j], lons.flat[j])}"
                if cats:
                    print(f"      {fo['label']}: {show(here, unit, cats)}")
                else:
                    print(f"      {fo['label']}: {fmt(here)} {unit} (strongest within {NEAR_KM} km {fmt(v.flat[j])}{rel})")
        if cont and cont.get("frames"):
            cf = next((x for x in cont["frames"] if x["valid_time"] == times[k]), None)
            if cf:
                cv = values(cont, cf)
                clons, clats = grid(cont)
                cok = np.isfinite(cv) & np.isfinite(clats)
                j = int(np.nanargmin(np.where(cok, cv, np.inf)))
                print(f"      lowest {cont['layer']} {fmt(cv.flat[j])} {cont['units']} at {where(clats.flat[j], clons.flat[j], focus)}")
        if fill in STRAT or sc.get("contours") in STRAT or sc.get("vectors") in STRAT:
            if f.get("run") and f.get("step") is not None:
                print("      " + stratosphere(f["run"], f["step"], 1 if (sc["bbox"][3] > 0) else -1))
        span = (vmax - vmin) or 1.0
        said = unit_numbers(c["text"], unit)
        for x in said:
            if not vmin - 0.15 * span <= x <= vmax + 0.15 * span:
                print(f"      FLAG: the caption says {fmt(x)} {unit}, but this frame ranges {fmt(vmin)} to {fmt(vmax)}")
                flags.append(f"{name}: caption {c['at']} says {fmt(x)} {unit}; the frame ranges {fmt(vmin)}..{fmt(vmax)}")
        if unit in PEAK_UNITS and said and max(said) < 0.5 * vmax:
            msg = (f"the frame's strongest colours are {fmt(vmax)} {unit} at {where(lats.flat[jmax], lons.flat[jmax], focus)}, "
                   f"but the caption quotes {fmt(max(said))} {unit}")
            print(f"      NOTE: {msg}: describe what the eye goes to, or crop the box")
            notes.append(f"{name}: caption {c['at']}: {msg}")
    last = caps[-1] if caps else None
    if last and not last.get("until") and last["at"] < times[-1]:
        print(f"    last caption stays on screen to {times[-1]}: is it still true then?")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("draft")
    ap.add_argument("--item", help="only items whose id contains this")
    a = ap.parse_args()
    try:
        os.nice(10)                         # never compete with the server
    except OSError:
        pass
    feed = json.loads(Path(a.draft).read_text())
    print(f"newest run on Open Data: {fields.iso(fields.latest_run())}")
    flags: list[str] = []
    notes: list[str] = []
    for it in feed.get("items", []):
        if a.item and a.item not in it.get("id", ""):
            continue
        print("=" * 100 + f"\n{it.get('id')}: {it.get('title')}")
        for n, sc in enumerate(it.get("scenes", [])):
            try:
                check_scene(it, n, sc, flags, notes)
            except Exception as e:          # one scene failing doesn't stop the others
                print(f"    could not check: {type(e).__name__}: {e}")
    if notes:
        print("\nNOTES (the text points at a weaker spot than the animation shows; look again):\n  - " + "\n  - ".join(notes))
    print("\n" + ("FLAGS (fix these, then run again):\n  - " + "\n  - ".join(flags) if flags else "No flags."))


if __name__ == "__main__":
    main()
