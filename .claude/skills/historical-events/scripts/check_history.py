"""Check a past event's draft before it goes for review (SKILL.md §4).

  uv run --frozen python check_history.py data/history/draft.json [--scan data/history/scan.json] [--links]

Prints ERROR lines for what must be fixed, WARNING lines for what to look at, and for each caption
what the ERA5 scan shows at that hour. Ends with OK (exit 0) or FAILED (exit 1). --links fetches
every source and archive link (some sites refuse automated requests: a refusal is a warning only).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))   # the backend directory
import fields    # noqa: E402

KINDS = {"official", "peer-reviewed", "statistics", "archive"}
IMPACT_KINDS = {"official", "peer-reviewed", "archive"}
HAZARDS = {"tropical-cyclone", "wind", "rain", "flood", "convection", "snow", "ice", "coastal", "heat", "cold", "other"}
NOT_CITABLE = re.compile(r"(^|\.)(wikipedia|wikimedia|wikiwand|fandom|reddit|facebook|twitter|x|youtube|medium|blogspot|wordpress)\.(org|com)$")
SECOND_HAND = re.compile(r"(^|\.)(researchgate\.net|academia\.edu|scribd\.com|docplayer\.\w+|yumpu\.com)$")
LAYERS = {k for k, l in fields.LAYERS.items() if l.source == "oper"}
VECTORS = {"wind10", "wind850", "jet250"}
CONTOURS = {"mslp", "z500"}
# Process language: what people read never mentions how it was made.
PROCESS = re.compile(r"\b(the|this|our) (scan|skill|script|checker|brief|draft)\b|\b(scan|skill|script)'s\b"
                     r"|\b\w+\.(json|py|log|grib2?)\b|\bscan_era5\b|\bcheck_history\b|\bas an AI\b", re.IGNORECASE)
TIME_FIRST = re.compile(r"^\s*(by|at|from|until|on|around|near)?\s*\d{1,2}(:\d{2})?\s*(UTC|GMT|Z|h)\b", re.IGNORECASE)
NUMBER = re.compile(r"\d")

errors: list[str] = []
warnings: list[str] = []


def err(msg: str) -> None:
    errors.append(msg)


def warn(msg: str) -> None:
    warnings.append(msg)


def time_of(s, where: str) -> datetime | None:
    try:
        t = fields.parse_time(s)
    except Exception:
        err(f"{where}: not a UTC time like 1987-10-16T03:00Z: {s!r}")
        return None
    if t.year < 1940 or t > datetime.now(timezone.utc):
        err(f"{where}: {s} is outside ERA5 (1940 to a few months ago)")
    return t


def bbox_ok(b, where: str) -> bool:
    if not (isinstance(b, list) and len(b) == 4 and all(isinstance(x, (int, float)) for x in b)):
        err(f"{where}: bbox must be [west, south, east, north]")
        return False
    w, s, e, n = b
    if not (-90 <= s < n <= 90):
        err(f"{where}: bbox south must be below north, within ±90")
        return False
    if ((e - w) % 360 or 360) > 120 or n - s > 70:
        warn(f"{where}: a very large box ({(e - w) % 360:.0f}° by {n - s:.0f}°): is the storm still visible?")
    return True


GRID_POINT = re.compile(r"\d+\.\d{2}\s*°\s*[NS]\b|\d+\.\d{2}\s*°\s*[EW]\b")


def text_ok(text, where: str) -> None:
    if not isinstance(text, str) or not text.strip():
        err(f"{where}: empty text")
        return
    if PROCESS.search(text):
        err(f"{where}: mentions how it was made ({PROCESS.search(text).group(0)!r}); write for the reader")
    if GRID_POINT.search(text):
        warn(f"{where}: grid coordinates ({GRID_POINT.search(text).group(0)!r}); name the place instead")


def cited(entries, where: str, ids: dict, kinds: set | None = None, need_text: str = "text") -> set:
    used = set()
    if not isinstance(entries, list):
        err(f"{where}: must be a list")
        return used
    for k, e in enumerate(entries):
        w = f"{where}[{k}]"
        if not isinstance(e, dict):
            err(f"{w}: must be an object with {need_text} and sources")
            continue
        text_ok(e.get(need_text), w)
        src = e.get("sources") or []
        if not src:
            err(f"{w}: cites no source")
        for s in src:
            if s not in ids:
                err(f"{w}: cites unknown source {s!r}")
            used.add(s)
        if kinds and src and not any(ids.get(s, {}).get("kind") in kinds for s in src):
            text = e.get(need_text, "")
            losses = re.search(r"£|\$|€|¥|\bbillion\b|\bmillion\b|\binsur", text or "", re.IGNORECASE)
            if not (losses and any(ids.get(s, {}).get("kind") == "statistics" for s in src)):
                err(f"{w}: needs at least one {', '.join(sorted(kinds))} source")
    return used


def check_links(sources: list[dict]) -> None:
    for s in sources:
        for key in ("url", "archive_url"):
            u = s.get(key)
            if not u:
                continue
            try:
                req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0 (weather-explorer link check)"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    r.read(512)
            except urllib.error.HTTPError as e:
                (warn if e.code in (401, 403, 405, 429, 451) else err)(f"source {s.get('id')} {key}: HTTP {e.code} {u}")
            except Exception as e:
                warn(f"source {s.get('id')} {key}: no answer ({type(e).__name__}) {u}")


def scan_at(scan: dict, t: datetime) -> str:
    key = fields.iso(t)
    parts = []
    tr = next((p for p in scan.get("track", []) if p["t"] == key), None)
    if tr:
        parts.append(f"low {tr['msl']} hPa at {tr['lat']}, {tr['lon']}")
    g = (scan.get("gusts") or {}).get(key) or []
    if g:
        parts.append("gusts " + ", ".join(f"{x['gust']} m/s at {x['lat']}, {x['lon']}" for x in g[:3]))
    for name, p in (scan.get("places") or {}).items():
        h = next((x for x in p["hours"] if x["t"] == key), None)
        if h:
            parts.append(f"{name} {h['msl']} hPa, gust {h['gust']} m/s, rain {h['rain']} mm/h")
    return "; ".join(parts) or "outside the scan window"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("draft", type=Path)
    ap.add_argument("--scan", type=Path)
    ap.add_argument("--links", action="store_true")
    a = ap.parse_args()
    try:
        it = json.loads(a.draft.read_text())
    except (OSError, ValueError) as e:
        print(f"ERROR: cannot read {a.draft}: {e}\nFAILED")
        return 1
    scan = json.loads(a.scan.read_text()) if a.scan and a.scan.exists() else {}

    if it.get("kind") != "historical":
        err('kind must be "historical"')
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", str(it.get("id", ""))):
        err("id: lower-case words joined by hyphens")
    text_ok(it.get("title"), "title")
    hz = it.get("hazards") or []
    if not hz or any(h not in HAZARDS for h in hz):
        err(f"hazards: one or more of {', '.join(sorted(HAZARDS))}")
    where = it.get("where") or {}
    for k in ("country", "label"):
        text_ok(where.get(k), f"where.{k}")
    bbox_ok(where.get("bbox"), "where.bbox")
    try:
        ZoneInfo(where.get("tz") or "")
    except (ZoneInfoNotFoundError, ValueError):
        err("where.tz: an IANA time zone such as Europe/London or America/Chicago")
    when = it.get("when") or {}
    ts = {k: time_of(when.get(k), f"when.{k}") for k in ("start", "peak", "end")}
    if all(ts.values()) and not ts["start"] <= ts["peak"] <= ts["end"]:
        err("when: start <= peak <= end")
    text_ok(it.get("summary"), "summary")
    if isinstance(it.get("summary"), str) and len(NUMBER.findall(it["summary"])) < 2:
        warn("summary: give the headline numbers (deaths, strongest gust or rain, damage)")
    text_ok(it.get("reanalysis"), "reanalysis")

    sources = it.get("sources") or []
    ids = {}
    for k, s in enumerate(sources):
        w = f"sources[{k}]"
        sid = s.get("id")
        if not sid or sid in ids:
            err(f"{w}: needs a unique id")
            continue
        ids[sid] = s
        if s.get("kind") not in KINDS:
            err(f"{w} ({sid}): kind must be one of {', '.join(sorted(KINDS))}")
        for key in ("title", "publisher"):
            if not s.get(key):
                err(f"{w} ({sid}): needs a {key}")
        for key in ("url", "archive_url"):
            u = s.get(key)
            if u is None and key == "archive_url":
                continue
            if not isinstance(u, str) or not re.match(r"https?://", u):
                err(f"{w} ({sid}): {key} must be an http(s) link")
                continue
            host = re.sub(r"^https?://([^/:]+).*", r"\1", u).lower()
            if NOT_CITABLE.search(host) and key == "url":
                err(f"{w} ({sid}): {host} is not a citable source (use it only to find sources)")
            elif SECOND_HAND.search(host) and key == "url":
                warn(f"{w} ({sid}): link the publisher's or an official copy rather than {host}")
    if not sources:
        err("sources: none")

    used = cited(it.get("impacts"), "impacts", ids, IMPACT_KINDS)
    if not it.get("impacts"):
        err("impacts: none")
    elif len(it["impacts"]) > 4:
        warn(f"impacts: {len(it['impacts'])} entries; summarise them in 2-4 (the weather leads)")
    if isinstance(it.get("why_interesting"), list) and len(it["why_interesting"]) < 3:
        warn("why_interesting: 3-6 entries on the meteorology (setting, development, mechanism, records)")
    used |= cited(it.get("history") or [], "history", ids)
    wi = it.get("why_interesting")
    if isinstance(wi, list):
        used |= cited(wi, "why_interesting", ids)
    elif wi:
        err("why_interesting: a list of {text, sources}")
    rows = it.get("era5_vs_observed") or []
    if not 2 <= len(rows) <= 8:
        err("era5_vs_observed: 2 to 8 rows")
    for k, r in enumerate(rows):
        for key in ("quantity", "era5"):
            text_ok(r.get(key), f"era5_vs_observed[{k}].{key}")
        for key in ("era5", "observed"):
            if len(r.get(key) or "") > 140:
                warn(f"era5_vs_observed[{k}].{key}: {len(r[key])} characters; keep cells short, explain in reanalysis")
    used |= cited(rows, "era5_vs_observed", ids, need_text="observed")
    for key in ("quantity", "observed"):
        seen = [str(r.get(key, "")).strip().lower() for r in rows]
        for v in {v for v in seen if v and seen.count(v) > 1}:
            warn(f"era5_vs_observed: two rows with the same {key} ({v[:60]!r})")
    for k, r in enumerate(rows):
        if re.search(r"\b(anywhere|in the (event )?area|regional|domain)\b", r.get("quantity", "") + " " + r.get("era5", ""), re.IGNORECASE):
            warn(f"era5_vs_observed[{k}]: compare like with like (the same place in both columns)")
    for sid in ids:
        if sid not in used:
            warn(f"source {sid} is never cited")

    scenes = it.get("scenes") or []
    if not 1 <= len(scenes) <= 4:
        err("scenes: 1 to 4")
    print("Captions against the ERA5 scan:")
    for n, sc in enumerate(scenes):
        w = f"scenes[{n}]"
        text_ok(sc.get("title"), f"{w}.title")
        if sc.get("fill") not in LAYERS:
            err(f"{w}.fill: one of {', '.join(sorted(LAYERS))}")
        if sc.get("contours") and sc["contours"] not in CONTOURS:
            err(f"{w}.contours: one of {', '.join(sorted(CONTOURS))}")
        if sc.get("vectors") and sc["vectors"] not in VECTORS:
            err(f"{w}.vectors: one of {', '.join(sorted(VECTORS))}")
        bbox_ok(sc.get("bbox"), f"{w}.bbox")
        t0, t1 = time_of(sc.get("start"), f"{w}.start"), time_of(sc.get("end"), f"{w}.end")
        if t0 and t1 and not 3 <= (t1 - t0).total_seconds() / 3600 <= 96:
            err(f"{w}: window 3 hours to 4 days")
        focus = sc.get("focus") or []
        if len(focus) > 5:
            err(f"{w}.focus: at most 5")
        for f in focus:
            if not (isinstance(f, dict) and isinstance(f.get("lat"), (int, float)) and isinstance(f.get("lon"), (int, float)) and f.get("label")):
                err(f"{w}.focus: each {{lat, lon, label}}")
            elif re.search(r"\b(low|centre|center|eye|minimum|lowest)\b", f["label"], re.IGNORECASE):
                warn(f"{w}.focus {f['label']!r}: the viewer marks the storm centre itself")
        caps = sc.get("captions") or []
        if not 1 <= len(caps) <= 5:
            err(f"{w}.captions: 1 to 5")
        last = None
        for k, c in enumerate(caps):
            cw = f"{w}.captions[{k}]"
            text = c.get("text", "")
            text_ok(text, cw)
            if TIME_FIRST.search(text or ""):
                err(f"{cw}: starts with a time (the viewer's clock shows it)")
            if len(text or "") > 90:
                warn(f"{cw}: {len(text)} characters; aim for under 70")
            if re.match(r"\s*(watch|look|see|note|compare|follow)\b", text or "", re.IGNORECASE):
                err(f"{cw}: addresses the viewer")
            if re.search(r"\b(low|centre|center)\b.*\b\d{3,4}\s*hPa\b|\b\d{3,4}\s*hPa\b.*\b(low|centre|center)\b", text or "", re.IGNORECASE) \
                    and not re.search(r"\b(gust|wind|rain|m/s|mm|kn)\b", text or "", re.IGNORECASE):
                warn(f"{cw}: only repeats the centre's pressure, which the moving marker shows; say what else to notice")
            t = time_of(c.get("at"), f"{cw}.at")
            if t and t0 and t1 and not t0 <= t <= t1:
                err(f"{cw}: at {c['at']} is outside the scene's window")
            if t and last and (t - last).total_seconds() < 6 * 3600:
                warn(f"{cw}: under 6 hours after the previous caption, too short to read")
            if t:
                last = t
                if scan:
                    print(f"  {w} {c.get('at')}: {text}\n      ERA5: {scan_at(scan, t)}")
    if set(hz) & {"rain", "flood"} and not any(sc.get("fill") == "raintotal" for sc in scenes):
        warn("rain or flood item without a raintotal scene: show the event total building up")
    if scan and ts.get("peak"):
        lows = (scan.get("lows") or {}).get(fields.iso(ts["peak"])) or []
        track = scan.get("track") or []
        if lows and track:
            first = next((p for p in track if p["t"] == fields.iso(ts["peak"])), None)
            if not first:
                warn("when.peak is not on the scan's track of the main low: check the viewer will follow your storm")

    if a.links:
        check_links(sources)
    for m in warnings:
        print(f"WARNING: {m}")
    for m in errors:
        print(f"ERROR: {m}")
    print("OK" if not errors else f"FAILED ({len(errors)} errors)")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
