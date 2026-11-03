#!/usr/bin/env python3
"""Validate a highlights feed and publish it as data/highlights/latest.json (plus a dated copy).

Usage (from the backend directory):
  uv run python .claude/skills/weather-highlights/scripts/publish_highlights.py draft.json
  uv run python .claude/skills/weather-highlights/scripts/publish_highlights.py --request OUT.json draft.json

--request validates a single requested highlight (exactly one item) and writes it to OUT.json
instead of replacing the feed.

Refuses to publish when the feed breaks the schema in references/feed-schema.md and lists every
problem. Warnings (for example a scene outside the Open Data time range) do not block publishing.
The file is replaced atomically, so clients never read a half-written feed.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

BACKEND = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(BACKEND))
import fields  # noqa: E402
import explainers  # noqa: E402

OUT = BACKEND / "data" / "highlights"
SCHEMA_VERSION = 1
HAZARDS = {"wind", "rain", "flood", "snow", "ice", "heat", "cold", "tropical-cyclone", "convection",
           "drought", "fire-weather", "fog", "dust", "coastal", "other"}
STATUSES = {"observed", "ongoing", "forecast"}
CONTOUR_LAYERS = {"mslp", "z500"}
VECTOR_LAYERS = {"wind10", "jet250", "wind850"}
KINDS = {"highlight", "explainer"}
MAX_EXPLAINERS = 3
REFS = Path(__file__).resolve().parents[1] / "references"
# Sites whose links may be cited as `warning` or `official` evidence: national services and warning
# centres, the states' warning pages, and government domains (.gov, .gov.nz, .go.jp, .gob.mx, ...).
AGENCY_SITES = set(json.loads((REFS / "agencies.json").read_text())["domains"]) | {
    (urlparse(s["warnings"]).hostname or "").removeprefix("www.")
    for s in json.loads((REFS / "states.json").read_text())["states"] if s.get("warnings")}
GOVERNMENT = re.compile(r"(^|\.)(gov|mil|int)$|(^|\.)(gov|go|gob|gouv|govt|gv)\.[a-z]{2}$")


def agency_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return bool(GOVERNMENT.search(host)) or any(host == d or host.endswith("." + d) for d in AGENCY_SITES)


# Data feeds (JSON, XML, CAP, RSS, APIs) are for scripts; a reader who clicks a source should land on
# a page written for people. Known feeds map to their public page; others must be replaced by hand.
DATA_URL = re.compile(r"\.(json|geojson|xml|kml|kmz|csv|grib2?|nc|bufr)(\?|$)|/(api|cap|rss|feeds?|publicData)(/|\?|$)",
                      re.IGNORECASE)
PUBLIC_PAGES = [  # (host, path prefix, public page)
    ("metservice.com", "/publicData/webdata/national", "https://www.metservice.com/national"),
    ("metservice.com", "/publicData/webdata/warnings", "https://www.metservice.com/warnings/home"),
    ("metservice.com", "/", "https://www.metservice.com/warnings/home"),
    ("jma.go.jp", "/bosai/typhoon", "https://www.jma.go.jp/bosai/map.html#contents=typhoon"),
    ("jma.go.jp", "/", "https://www.jma.go.jp/bosai/warning/"),
] + [("meteoalarm.org", f"/api/v1/warnings/feeds-{s['meteoalarm']}", s["warnings"])
     for s in json.loads((REFS / "states.json").read_text())["states"] if s.get("meteoalarm") and s.get("warnings")]
# Evidence text is read by presenters: one plain sentence, no method notes or file names.
EVIDENCE_MAX = 200
JARGON = re.compile(r"\b\w+\.(json|py|log|grib2?)\b|\b(fields|scan)\.\w|get_frames|grid cell|resampled|\w\[\d+\]")


def data_url(url: str) -> bool:
    u = urlparse(url)
    return bool(DATA_URL.search(u.path) or (u.hostname or "").startswith(("feeds.", "api.", "alerts.")))


def public_page(url: str) -> str | None:
    """The public page for a known data feed, else None."""
    u = urlparse(url)
    host = (u.hostname or "").lower()
    for h, prefix, page in PUBLIC_PAGES:
        if (host == h or host.endswith("." + h)) and u.path.startswith(prefix):
            return page
    return None


def t(s, where, errors):
    try:
        return fields.parse_time(s)
    except Exception:
        errors.append(f"{where}: bad time {s!r} (use ISO 8601 UTC, e.g. 2026-10-23T06:00Z)")


def check_bbox(b, where, errors):
    if not (isinstance(b, list) and len(b) == 4 and all(isinstance(x, (int, float)) for x in b)):
        errors.append(f"{where}: bbox must be [west, south, east, north] numbers")
        return
    w, s, e, n = b
    if not (-90 <= s < n <= 90):
        errors.append(f"{where}: bbox south/north out of order or range")
    width = (e - w) % 360 or 360
    if width < 2 or (n - s) < 2:
        errors.append(f"{where}: bbox smaller than 2 degrees")


def check_explainers(items: list, errors: list) -> None:
    """Each explainer shows something new: not a topic from the last sweeps, not a place a highlight covers."""
    shown = explainers.recent()
    for i, it in enumerate(items):
        if it.get("kind") != "explainer":
            continue
        w = f"items[{i}] ({it.get('id', '?')})"
        topic = it.get("topic")
        if not isinstance(topic, str) or not topic.strip():
            errors.append(f"{w}: explainers need a topic: the scan's phenomenon name (e.g. 'Bora (Adriatic)'), "
                          "or a short name for anything else (e.g. 'Convection: Madagascar')")
            continue
        area = explainers.area_of(it)
        r = explainers.shown_recently(topic, area, shown)
        if r:
            errors.append(f"{w}: '{topic}' repeats the explainer in the {r['sweep']} sweep ({r['title']}); "
                          f"an explainer rests for {explainers.REPEAT_SWEEPS} sweeps: choose another or leave it out")
        for j, other in enumerate(items):
            box = other.get("where", {}).get("bbox")
            if other.get("kind") == "explainer" or not area or not box:
                continue
            if explainers.overlap(area, box) >= 0.5:
                errors.append(f"{w}: '{topic}' is where highlight items[{j}] ({other.get('id', '?')}) already is; "
                              "explain the mechanism in that highlight's why_interesting instead, or choose another explainer")


def validate(feed: dict, sweep: bool = True) -> tuple[list[str], list[str]]:
    """sweep: a full feed, whose explainers are also checked against recent sweeps and its own highlights."""
    errors, warnings = [], []
    for k in ("generated_utc", "items"):
        if k not in feed:
            errors.append(f"missing top-level {k!r}")
    items = feed.get("items", [])
    if not 1 <= len(items) <= 18:
        errors.append(f"items: expected 1-18, got {len(items)}")
    latest = fields.latest_run()
    lo, hi = latest - timedelta(hours=72), latest + timedelta(hours=360)
    ids = set()
    if sum(1 for it in items if it.get("kind") == "explainer") > MAX_EXPLAINERS:
        errors.append(f"at most {MAX_EXPLAINERS} explainer items")
    for i, it in enumerate(items):
        w = f"items[{i}] ({it.get('id', '?')})"
        for k in ("id", "title", "hazards", "status", "where", "when", "summary", "why_interesting", "evidence", "scenes"):
            if k not in it:
                errors.append(f"{w}: missing {k!r}")
        if it.get("id") in ids:
            errors.append(f"{w}: duplicate id")
        ids.add(it.get("id"))
        if not set(it.get("hazards", [])) <= HAZARDS:
            errors.append(f"{w}: unknown hazards {sorted(set(it.get('hazards', [])) - HAZARDS)}; allowed {sorted(HAZARDS)}")
        if it.get("kind", "highlight") not in KINDS:
            errors.append(f"{w}: kind must be one of {sorted(KINDS)} (or omitted for a normal highlight)")
        if it.get("status") not in STATUSES:
            errors.append(f"{w}: status must be one of {sorted(STATUSES)}")
        where = it.get("where", {})
        check_bbox(where.get("bbox"), f"{w}.where", errors)
        if not (isinstance(where.get("centre"), list) and len(where["centre"]) == 2):
            errors.append(f"{w}.where: centre must be [lat, lon]")
        for k in ("start", "end"):
            t(it.get("when", {}).get(k, ""), f"{w}.when.{k}", errors)
        ev = it.get("evidence", [])
        if not ev:
            errors.append(f"{w}: at least one evidence item required")
        for j, e in enumerate(ev):
            if e.get("kind") not in {"ensemble", "field", "warning", "news", "official", "observation"}:
                errors.append(f"{w}.evidence[{j}]: unknown kind {e.get('kind')!r}")
            text = str(e.get("text", ""))
            if len(text) > EVIDENCE_MAX:
                errors.append(f"{w}.evidence[{j}]: text is {len(text)} characters; keep it to one plain sentence "
                              f"of at most {EVIDENCE_MAX} (source, date, the number that matters)")
            if JARGON.search(text):
                errors.append(f"{w}.evidence[{j}]: text has method notes ({JARGON.search(text).group(0)!r}); write it "
                              "for a presenter, e.g. 'ECMWF model (00 UTC 2 Nov run): up to 120 mm on the ranges by 3 Nov'")
            if str(e.get("url", "")).startswith("http") and data_url(e["url"]):
                page = public_page(e["url"])
                if page:
                    warnings.append(f"{w}.evidence[{j}]: data feed {e['url']} replaced by its public page {page}")
                    e["url"] = page
                else:
                    errors.append(f"{w}.evidence[{j}]: {e['url']} is a data feed (JSON/XML/CAP/RSS/API), not a page "
                                  "people can read; link the agency's public page instead")
            if e.get("kind") in {"warning", "news", "official", "observation"} and not str(e.get("url", "")).startswith("http"):
                errors.append(f"{w}.evidence[{j}]: {e.get('kind')} evidence needs a real url")
            elif e.get("kind") in {"warning", "official"} and not agency_url(e["url"]):
                warnings.append(f"{w}.evidence[{j}]: {e['kind']} relabelled as news: {e['url']} is not an "
                                "agency's own site (references/agencies.json)")
                e["kind"] = "news"
        scenes = it.get("scenes", [])
        if not 1 <= len(scenes) <= 4:
            errors.append(f"{w}: expected 1-4 scenes")
        for j, sc in enumerate(scenes):
            sw = f"{w}.scenes[{j}]"
            if sc.get("fill") not in fields.APP_READY:
                why = " (not available in highlights yet: the iPhone app can't show it)" if sc.get("fill") in fields.LAYERS else ""
                errors.append(f"{sw}: fill {sc.get('fill')!r}{why}; use one of {sorted(fields.APP_READY)}")
            if sc.get("contours") not in (None, *CONTOUR_LAYERS):
                errors.append(f"{sw}: contours must be one of {sorted(CONTOUR_LAYERS)} or omitted")
            vectors_ok = sorted(VECTOR_LAYERS & fields.APP_READY)
            if sc.get("vectors") not in (None, *vectors_ok):
                errors.append(f"{sw}: vectors must be one of {vectors_ok} or omitted")
            check_bbox(sc.get("bbox"), sw, errors)
            s0, s1 = t(sc.get("start", ""), f"{sw}.start", errors), t(sc.get("end", ""), f"{sw}.end", errors)
            if s0 and s1:
                if s1 <= s0:
                    errors.append(f"{sw}: end must be after start")
                if not str(sc.get("fill", "")).startswith("ens_") and (s0 < lo or s1 > hi):
                    warnings.append(f"{sw}: window partly outside Open Data range {fields.iso(lo)}..{fields.iso(hi)}; frames will be clipped")
            for c in sc.get("captions", []):
                t(c.get("at", ""), f"{sw}.captions", errors)
    if sweep:
        check_explainers(items, errors)
    return errors, warnings


def main():
    args = sys.argv[1:]
    request_out = None
    if len(args) == 3 and args[0] == "--request":
        request_out, args = Path(args[1]), args[2:]
    if len(args) != 1:
        sys.exit(__doc__)
    feed = json.loads(Path(args[0]).read_text())
    feed.setdefault("schema_version", SCHEMA_VERSION)
    feed.setdefault("attribution", fields.ATTRIBUTION.format(year=datetime.now(timezone.utc).year))
    errors, warnings = validate(feed, sweep=not request_out)
    if request_out and len(feed.get("items", [])) != 1:
        errors.append("a requested highlight must have exactly one item")
    for w in warnings:
        print("WARNING", w)
    if errors:
        print("NOT PUBLISHED - fix these and run again:")
        for e in errors:
            print("  -", e)
        sys.exit(1)
    text = json.dumps(feed, indent=1, ensure_ascii=False)
    if request_out:
        request_out.parent.mkdir(parents=True, exist_ok=True)
        tmp = request_out.with_suffix(".tmp")
        tmp.write_text(text)
        os.replace(tmp, request_out)
        print(f"published the requested highlight -> {request_out}")
        return
    OUT.mkdir(parents=True, exist_ok=True)
    explainers.record(feed)                     # before latest.json, so the outgoing feed is kept too
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")
    (OUT / f"highlights-{stamp}.json").write_text(text)
    tmp = OUT / ".latest.json.tmp"
    tmp.write_text(text)
    os.replace(tmp, OUT / "latest.json")
    print(f"published {len(feed['items'])} highlights -> {OUT / 'latest.json'} (archive highlights-{stamp}.json)")


if __name__ == "__main__":
    main()
