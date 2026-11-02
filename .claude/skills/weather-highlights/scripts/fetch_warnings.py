#!/usr/bin/env python3
"""Collect official warning/alert evidence for ECMWF Member and Co-operating States.

Sources:
  * Meteoalarm (feeds.meteoalarm.org) - active orange/red warnings per country.
  * GDACS - orange/red tropical cyclone, flood, drought and wildfire alerts from the
    last N days that touch any state or overseas territory in references/states.json.
  * National CAP feeds read directly (CAP_FEEDS, e.g. MetService New Zealand): every current
    warning with its full text, areas, times (UTC) and its permanent link on the agency's site.
  * WMO Severe Weather Information Centre (severeweather.wmo.int) - the CAP warnings of about
    45 national services, Moderate severity or worse, grouped by agency, event and severity.
    Countries on MeteoAlarm, and agencies read directly, are left out (they are listed above).

Usage: fetch_warnings.py [--hours-ahead 24] [--gdacs-days 7] [--worldwide] [--out FILE]
Prints (or writes) a JSON summary. It never decides what counts as "impactful":
it gathers evidence for Claude to weigh alongside news reports.
"""
import argparse, json, os, subprocess, sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
STATES = json.load(open(os.path.join(HERE, "..", "references", "states.json")))["states"]
UA = "ecmwf-weather-highlights/0.1"
# GDACS uses ISO3 codes; a few of our map unit ids differ.
ISO3_TO_UNIT = {"BES": "NLY", "SJM": "NSV"}
GDACS_TYPES = {"TC": "Tropical cyclone", "FL": "Flood", "DR": "Drought", "WF": "Wildfire"}
WMO = "https://severeweather.wmo.int"
SEVERITY = {0: "Unknown", 1: "Minor", 2: "Moderate", 3: "Severe", 4: "Extreme"}  # WMO's "s"
CAP = "{urn:oasis:names:tc:emergency:cap:1.2}"
# National CAP feeds read directly: their RSS carries each warning's full text, which WMO's list
# lacks. wmo_source is the feed's prefix in WMO's list, so the agency isn't listed twice.
CAP_FEEDS = [{"agency": "MetService", "country": "New Zealand", "code": "NZL",
              "rss": "https://alerts.metservice.com/cap/rss", "wmo_source": "nz-nms-en"}]


def get_text(url, timeout=30, accept="application/json"):
    # curl rather than urllib: GDACS stalls Python's TLS client but answers curl promptly.
    try:
        r = subprocess.run(["curl", "-sSfL", "-m", str(timeout), "-A", UA, "-H", f"Accept: {accept}", url],
                           capture_output=True, text=True)
    except FileNotFoundError:  # no curl (some cloud images): fall back to urllib
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or f"curl exit {r.returncode}")
    return r.stdout


def get_json(url, timeout=30):
    return json.loads(get_text(url, timeout))


def parse_t(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def wmo_t(s):
    """WMO's list gives UTC times as '2026-11-02 22:00:00'."""
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc) if s else None


def utc(t):
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ") if t else None


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
            rec["areas"].add((a.get("areaDesc") or "").strip())
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


def cap_feed(feed, now, horizon):
    """One national CAP RSS feed, each warning completed from its own CAP message."""
    try:
        items = list(ET.fromstring(get_text(feed["rss"], accept="application/rss+xml")).iter("item"))
    except Exception as e:
        return {"agency": feed["agency"], "country": feed["country"], "error": str(e)}
    warnings = []
    for it in items:
        rec = {"title": it.findtext("title"), "text": (it.findtext("description") or "").strip()[:800],
               "url": it.findtext("link")}
        try:
            msg = ET.fromstring(get_text(rec["url"], accept="application/xml"))
        except Exception as e:
            warnings.append({**rec, "error": str(e)})
            continue
        info = msg.find(f"{CAP}info")
        if msg.findtext(f"{CAP}msgType") == "Cancel" or info is None:
            continue
        onset = parse_t(info.findtext(f"{CAP}onset") or info.findtext(f"{CAP}effective"))
        expires = parse_t(info.findtext(f"{CAP}expires"))
        if (expires and expires < now) or (onset and onset > horizon):
            continue
        warnings.append({**rec, "severity": info.findtext(f"{CAP}severity"), "onset": utc(onset),
                         "expires": utc(expires), "web": info.findtext(f"{CAP}web"),
                         "areas": [(a.findtext(f"{CAP}areaDesc") or "").strip() for a in info.iter(f"{CAP}area")]})
    return {"agency": feed["agency"], "country": feed["country"], "warnings": warnings}


def wmo_cap(now, horizon, keep, skip_sources):
    """WMO's combined CAP list, Moderate or worse, grouped by agency, event and severity.
    keep(iso3) says whether a country is wanted; skip_sources are WMO source prefixes to leave out."""
    try:
        alerts = get_json(f"{WMO}/json/wmo_all.json", timeout=60)["items"]
        members = {m["mid"]: m for r in get_json(f"{WMO}/json/wmo_member.json") for m in r["members"]}
    except Exception as e:
        return {"error": str(e), "fallback": f"check {WMO} manually"}
    groups = {}
    for a in alerts:
        # This list's CAP paths resolve under /v2/cap-alerts/ (the /v2/json list's paths don't).
        path = a.get("capURL") or a.get("url") or ""
        source, member = path.split("/")[0], members.get(a.get("mid"), {})
        if a.get("s", 0) < 2 or "marine" in source or source in skip_sources or not keep(member.get("code")):
            continue
        onset, expires = wmo_t(a.get("effective")), wmo_t(a.get("expires"))
        if (expires and expires < now) or (onset and onset > horizon):
            continue
        rec = groups.setdefault((a.get("mid"), a.get("event"), a["s"]), {
            "country": member.get("name"), "agency": member.get("dept"),
            "agency_site": f"https://{member['url']}" if member.get("url") else None,
            "event": a.get("event"), "severity": SEVERITY.get(a["s"]), "headline": a.get("headline"),
            "count": 0, "areas": set(), "expires": None, "cap": [], "s": a["s"]})
        rec["count"] += 1
        rec["areas"].add((a.get("areaDesc") or "").strip())
        rec["expires"] = max(filter(None, (rec["expires"], utc(expires))), default=None)
        if len(rec["cap"]) < 3:
            rec["cap"].append(f"{WMO}/v2/cap-alerts/{path}")
    out = []
    for rec in sorted(groups.values(), key=lambda r: (-r.pop("s"), r["country"] or "")):
        areas = sorted(a for a in rec.pop("areas") if a)
        rec["areas"] = areas[:15] + ([f"... +{len(areas) - 15} more"] if len(areas) > 15 else [])
        out.append(rec)
    return {"note": "Severity is CAP's, not the agency's colour: many services' orange warning is "
                    "Moderate. Times UTC. 'cap' links are the CAP messages as WMO holds them; cite the "
                    "agency's own warnings page where you can find it.",
            "alerts": out}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours-ahead", type=int, default=24,
                    help="include warnings starting within this many hours (default 24)")
    ap.add_argument("--gdacs-days", type=int, default=7)
    ap.add_argument("--worldwide", action="store_true",
                    help="keep GDACS and national CAP alerts anywhere in the world, not only in ECMWF states")
    ap.add_argument("--out")
    a = ap.parse_args()

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=a.hours_ahead)
    units = {}
    for s in STATES:
        units[s["unit"]] = s["name"]
        for t in s.get("territories", []):
            units[t["unit"]] = f"{t['name']} ({s['name']})"

    on_meteoalarm = {s["unit"] for s in STATES if s.get("meteoalarm")}
    feeds = [f for f in CAP_FEEDS if a.worldwide or f["code"] in units]

    def keep(code):
        return code not in on_meteoalarm and (a.worldwide or code in units)

    with ThreadPoolExecutor(12) as ex:
        national = ex.map(lambda f: cap_feed(f, now, horizon), feeds)
        wmo = ex.submit(wmo_cap, now, horizon, keep, {f["wmo_source"] for f in feeds})
        ma = list(ex.map(lambda s: meteoalarm(s, now, horizon),
                         [s for s in STATES if s.get("meteoalarm")]))
        national, wmo = list(national), wmo.result()
    result = {
        "generated_utc": now.strftime("%Y-%m-%dT%H:%MZ"),
        "meteoalarm_orange_red": [m for m in ma if m.get("warnings") or m.get("error")],
        "meteoalarm_quiet": [m["state"] for m in ma if not m.get("warnings") and not m.get("error")],
        "not_on_meteoalarm": [s["name"] for s in STATES if not s.get("meteoalarm")] +
                             ["all overseas territories"],
        "gdacs": gdacs(a.gdacs_days, units, a.worldwide),
        "national_cap_feeds": national,
        "wmo_cap_moderate_or_worse": wmo,
    }
    text = json.dumps(result, indent=1, ensure_ascii=False)
    if a.out:
        open(a.out, "w").write(text)
        print(f"wrote {a.out}")
    else:
        print(text)


if __name__ == "__main__":
    sys.exit(main())
