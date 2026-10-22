#!/usr/bin/env python3
"""Collect official warning/alert evidence for ECMWF Member and Co-operating States.

Sources:
  * Meteoalarm (feeds.meteoalarm.org) - active orange/red warnings per country.
  * GDACS - orange/red tropical cyclone, flood, drought and wildfire alerts from the
    last N days that touch any state or overseas territory in references/states.json.

Usage: fetch_warnings.py [--hours-ahead 24] [--gdacs-days 7] [--out FILE]
Prints (or writes) a JSON summary. It never decides what counts as "impactful":
it gathers evidence for Claude to weigh alongside news reports.
"""
import argparse, json, os, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
STATES = json.load(open(os.path.join(HERE, "..", "references", "states.json")))["states"]
UA = "ecmwf-weather-highlights/0.1"
# GDACS uses ISO3 codes; a few of our map unit ids differ.
ISO3_TO_UNIT = {"BES": "NLY", "SJM": "NSV"}
GDACS_TYPES = {"TC": "Tropical cyclone", "FL": "Flood", "DR": "Drought", "WF": "Wildfire"}


def get_json(url, timeout=30):
    # curl rather than urllib: GDACS stalls Python's TLS client but answers curl promptly.
    try:
        r = subprocess.run(["curl", "-sSfL", "-m", str(timeout), "-A", UA, "-H", "Accept: application/json", url],
                           capture_output=True, text=True)
    except FileNotFoundError:  # no curl (some cloud images): fall back to urllib
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or f"curl exit {r.returncode}")
    return json.loads(r.stdout)


def parse_t(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def meteoalarm(state, now, horizon):
    slug = state["meteoalarm"]
    try:
        data = get_json(f"https://feeds.meteoalarm.org/api/v1/warnings/feeds-{slug}")
    except Exception as e:
        return {"state": state["name"], "error": str(e)}
    found = {}
    for w in data.get("warnings", []):
        alert = w.get("alert", {})
        if alert.get("msgType") == "Cancel":
            continue
        infos = alert.get("info", [])
        # Prefer the English block of a multilingual alert.
        info = next((i for i in infos if i.get("language", "").startswith("en")), infos[0] if infos else None)
        if not info or "cancel" in (info.get("headline") or "").lower():
            continue
        params = {p["valueName"]: p["value"] for p in info.get("parameter", [])}
        level = params.get("awareness_level", "")
        colour = next((c for c in ("red", "orange") if c in level.lower()), None)
        if not colour:
            continue
        onset = parse_t(info.get("onset") or info.get("effective"))
        expires = parse_t(info.get("expires"))
        if expires and expires < now:
            continue
        if onset and onset > horizon:
            continue
        hazard = params.get("awareness_type", "").split(";")[-1].strip() or info.get("event", "")
        key = (colour, hazard)
        rec = found.setdefault(key, {"level": colour, "hazard": hazard, "areas": set(),
                                     "onset": info.get("onset"), "expires": info.get("expires"),
                                     "headline": info.get("headline"), "sender": info.get("senderName"),
                                     "web": info.get("web")})
        for a in info.get("area", []):
            rec["areas"].add(a.get("areaDesc"))
        if info.get("onset") and (rec["onset"] is None or info["onset"] < rec["onset"]):
            rec["onset"] = info["onset"]
        if info.get("expires") and (rec["expires"] is None or info["expires"] > rec["expires"]):
            rec["expires"] = info["expires"]
    warnings = []
    for rec in sorted(found.values(), key=lambda r: (r["level"] != "red", r["hazard"])):
        areas = sorted(a for a in rec.pop("areas") if a)
        rec["areas"] = areas[:25] + ([f"... +{len(areas) - 25} more"] if len(areas) > 25 else [])
        warnings.append(rec)
    return {"state": state["name"], "unit": state["unit"], "warnings": warnings}


def gdacs(days, units, worldwide=False):
    today = datetime.now(timezone.utc).date()
    url = ("https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH?"
           f"eventlist={';'.join(GDACS_TYPES)}&alertlevel=Orange;Red"
           f"&fromdate={today - timedelta(days=days)}&todate={today}")
    data, err = None, None
    for _ in range(2):  # GDACS is often slow; one retry
        try:
            data = get_json(url, timeout=60)
            break
        except Exception as e:
            err = str(e)
    if data is None:
        return {"error": err, "fallback": "check https://www.gdacs.org manually"}
    out = []
    for f in data.get("features", []):
        p = f["properties"]
        hits = []
        for c in p.get("affectedcountries", []) or []:
            u = ISO3_TO_UNIT.get(c.get("iso3"), c.get("iso3"))
            if u in units:
                hits.append({"unit": u, "name": units[u]})
        if not hits and not worldwide:
            continue
        geom = f.get("geometry") or {}
        out.append({"type": GDACS_TYPES.get(p["eventtype"], p["eventtype"]), "name": p.get("name"),
                    "country": p.get("country"),
                    "point": geom.get("coordinates") if geom.get("type") == "Point" else None,
                    "alert": p.get("alertlevel"), "current": p.get("iscurrent"),
                    "from": p.get("fromdate"), "to": p.get("todate"),
                    "affects": hits, "report": p.get("url", {}).get("report")})
    return {"events": out}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours-ahead", type=int, default=24,
                    help="include warnings starting within this many hours (default 24)")
    ap.add_argument("--gdacs-days", type=int, default=7)
    ap.add_argument("--worldwide", action="store_true",
                    help="keep GDACS alerts anywhere in the world, not only in ECMWF states")
    ap.add_argument("--out")
    a = ap.parse_args()

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=a.hours_ahead)
    units = {}
    for s in STATES:
        units[s["unit"]] = s["name"]
        for t in s.get("territories", []):
            units[t["unit"]] = f"{t['name']} ({s['name']})"

    with ThreadPoolExecutor(12) as ex:
        ma = list(ex.map(lambda s: meteoalarm(s, now, horizon),
                         [s for s in STATES if s.get("meteoalarm")]))
    result = {
        "generated_utc": now.strftime("%Y-%m-%dT%H:%MZ"),
        "meteoalarm_orange_red": [m for m in ma if m.get("warnings") or m.get("error")],
        "meteoalarm_quiet": [m["state"] for m in ma if not m.get("warnings") and not m.get("error")],
        "not_on_meteoalarm": [s["name"] for s in STATES if not s.get("meteoalarm")] +
                             ["all overseas territories"],
        "gdacs": gdacs(a.gdacs_days, units, a.worldwide),
    }
    text = json.dumps(result, indent=1, ensure_ascii=False)
    if a.out:
        open(a.out, "w").write(text)
        print(f"wrote {a.out}")
    else:
        print(text)


if __name__ == "__main__":
    sys.exit(main())
