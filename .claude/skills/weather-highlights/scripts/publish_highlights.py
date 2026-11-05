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
CONTOUR_LAYERS = {"mslp", "z500", "z10hpa"}
VECTOR_LAYERS = {"wind10", "jet250", "wind850", "wind10hpa"}
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
# Everything a viewer reads (titles, summary, why_interesting, talking points, captions, evidence)
# is about the weather, never about how the sweep was made: no scan, skill, app, scripts, files,
# failed fetches or source formats. Those caveats go in the feed's `gaps` or the report.
PROCESS = re.compile(
    r"\b(the|this|our) (scan|skill|publisher|validator|script)\b"
    r"|\b(scan|skill|sweep|app|script|tool)'s\b"
    r"|\b(could|can) ?not be (fetched|retrieved|downloaded|read|checked|obtained|accessed|loaded)\b"
    r"|\b(was|were|is|are) not (fetched|obtained|retrieved|downloaded|loaded)\b"
    r"|\b(CAP|PDF|JSON|RSS|XML|API)\b|\b(duplicate|separate|another) (explainer|item|highlight)\b"
    r"|\b\w+\.(json|py|log|grib2?)\b|\bfields\.\w|\bget_frames\b"
    r"|\b([Pp]revious|[Ll]ast|[Ee]arlier|[Ll]atest|[Tt]his|[Tt]oday's|[Yy]esterday's) (feed|sweep|highlights? list)\b(?! of)"
    r"|\boriginal (warning|bulletin|advisory|statement|text)\b")
# A caption sits beside the viewer's clock, which shows its time in the viewer's own zone: a time
# stamp at the start ("At 21 UTC 4 Nov:", "By 9 Nov,") repeats it. Say what happens instead.
CAPTION_TIME = re.compile(r"^\s*(at|by|from|on|until|around|early|late|on the)?\s*"
                          r"(\d{1,2}(:\d\d)?\s?UTC\b|\d{1,2}\s(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b"
                          r"|(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\b)", re.IGNORECASE)
# Confidence is about the weather. "High confidence in the warning" says only that the warning exists.
SOURCE_CONFIDENCE = re.compile(r"\bconfidence in (the |its |official |agency |national )*"
                               r"(alerts?|warnings?|outlooks?|advisor(y|ies)|bulletins?|watch(es)?)\b", re.IGNORECASE)
# Coordinates to the grid cell ("85.75°N, 55.25°E") are false precision for a presenter: round them,
# or better, name the sea, coast or region. (Evidence may quote an agency's position as given.)
GRID_COORD = re.compile(r"\b\d{1,3}\.\d+\s?°\s?[NSEW]\b")
# Disclaimers about what the text is not saying ("no flooding is asserted") read as legal hedging:
# say what is known and how sure it is instead.
# Non-standard name: it is "the polar vortex", or "the Arctic polar vortex" with the hemisphere.
VORTEX_NAME = re.compile(r"\b(Arctic|Antarctic) vortex\b", re.I)
HEDGE = re.compile(r"\b(is|are|was|were|being) (asserted|claimed|implied)\b"
                   r"|\bshould not be (read|inferred|taken|interpreted)\b", re.IGNORECASE)
# "Model rain is weaker than the warning" usually compares a rate (mm/h, smoothed over 3-6 h) with a
# warning's total over a day: compare totals (the rain24 layer) before saying so.
MODEL_VS_WARNING = re.compile(r"\bmodel\b[^.]{0,60}\b(lower|weaker|less|below)\b[^.]{0,60}\bwarn", re.IGNORECASE)
# An evidence line states what the source says (a fact, a number), not that it exists.
VACUOUS = re.compile(r"\b(provides?|gives?|offers?|adds?) (\w+ ){0,2}context\b|\bfor (more |further )?context\b"
                     r"|\b(monitors?|tracks?|follows?) (the )?(situation|vortex|event|story)\b", re.IGNORECASE)


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


def public_texts(it: dict):
    """(where, text) for everything a viewer reads in an item."""
    for k in ("title", "summary", "why_interesting", "confidence"):
        if isinstance(it.get(k), str):
            yield k, it[k]
    for j, tp in enumerate(it.get("talking_points") or []):
        yield f"talking_points[{j}]", str(tp)
    for j, e in enumerate(it.get("evidence") or []):
        yield f"evidence[{j}]", str(e.get("text", ""))
    for j, sc in enumerate(it.get("scenes") or []):
        yield f"scenes[{j}].title", str(sc.get("title", ""))
        for c in sc.get("captions") or []:
            yield f"scenes[{j}].captions", str(c.get("text", ""))


def check_captions(sc: dict, sw: str, warnings: list) -> None:
    """Captions must sit on frames the scene will have: the viewer shows a caption from its time
    until the next caption (or its `until`), so one between frames first appears on a later frame
    and one after the last frame never appears. Warnings, not errors: the frames are fixed only
    when the scenes are built (the builder then moves stray captions to the nearest frame)."""
    caps = sc.get("captions") or []
    if not caps or str(sc.get("fill", "")).startswith("ens_") or sc.get("fill") not in fields.LAYERS:
        return
    try:
        times = fields.scene_times(sc["fill"], sc.get("start"), sc.get("end"))
    except Exception:
        return
    if not times:
        return
    shown = ", ".join(f"{t:%d}T{t:%H}" for t in times)
    for c in caps:
        try:
            at = fields.parse_time(c["at"])
        except Exception:
            continue
        if at > times[-1]:
            warnings.append(f"{sw}: caption at {c['at']} is after the last frame ({fields.iso(times[-1])}) and would "
                            f"never show; move it to a frame (frames: {shown})")
        elif at < times[0]:
            warnings.append(f"{sw}: caption at {c['at']} is before the first frame ({fields.iso(times[0])})")
        elif at not in times:
            warnings.append(f"{sw}: caption at {c['at']} falls between frames and would first show at the next one; "
                            f"use a frame time (frames: {shown})")
    last = max((c.get("at", "") for c in caps), default="")
    if (last and not any(c.get("until") for c in caps)
            and times[-1] - fields.parse_time(last) >= timedelta(hours=24)):
        lc = next(c for c in caps if c.get("at") == last)
        warnings.append(f"{sw}: the last caption ({last}: {str(lc.get('text', ''))[:50]!r}) stays on screen for a day or more, to "
                        f"the end ({fields.iso(times[-1])}); make sure it is still true then, or add a caption or an `until`")


# Amounts, where "the same again" says nothing new. Temperatures and pressures are left out: a heat
# wave holding at 45 °C is the story.
AMOUNT_UNITS = {"mm", "mm/h", "m/s", "kg/m²", "J/kg", "%"}


def check_caption_values(sc: dict, sw: str, warnings: list) -> None:
    """Captions that quote nearly the same value at every step ("Quibdó has 63, then 65, then 64
    kg/m²") tell no story: the viewer sees nothing change. Say what moves, grows or contrasts."""
    unit = fields.LAYERS[sc["fill"]].units if sc.get("fill") in fields.LAYERS else ""
    if unit not in AMOUNT_UNITS or fields.LAYERS[sc["fill"]].categories:
        return
    u = re.escape(unit).replace("°", "°?\\s?")
    said = []
    for c in sc.get("captions") or []:
        m = re.search(rf"([−\-]?\d+(?:\.\d+)?)\s*{u}(?![\w/])", str(c.get("text", "")))
        if m:
            said.append(abs(float(m.group(1).replace("−", "-"))))
    if len(said) >= 3 and max(said) > 0 and (max(said) - min(said)) <= 0.1 * max(said):
        warnings.append(f"{sw}: the captions quote nearly the same value each time ({', '.join(f'{x:g}' for x in said)} "
                        f"{unit}), so nothing changes for the viewer; say what moves, grows or contrasts instead")


def check_variety(items: list, warnings: list) -> None:
    """A feed where many items have the same scenes, or neighbouring items share a hazard, reads as one
    story told several times. Warnings: sometimes the weather really is like that."""
    recipes: dict[tuple, list[str]] = {}
    for it in items:
        if it.get("kind") != "explainer":
            recipes.setdefault(tuple(sc.get("fill") for sc in it.get("scenes") or []), []).append(it.get("id", "?"))
    for fills, ids in recipes.items():
        if len(ids) >= 4:
            warnings.append(f"{len(ids)} items have the same scenes ({' + '.join(map(str, fills))}): {', '.join(ids)}. "
                            "Show what is different about each (how the moisture arrives, the storms, the wind, the "
                            "rain moving: see the scene recipes), or merge neighbours with one cause")
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            if "explainer" in (a.get("kind"), b.get("kind")):
                continue
            shared = (set(a.get("hazards") or []) & set(b.get("hazards") or [])) - {"other"}
            ba, bb = a.get("where", {}).get("bbox"), b.get("where", {}).get("bbox")
            if shared and isinstance(ba, list) and isinstance(bb, list) and len(ba) == len(bb) == 4 and near(ba, bb):
                warnings.append(f"{a.get('id')} and {b.get('id')} are next to each other and share {sorted(shared)}: if one "
                                "weather system causes both, make them one highlight; if not, say in each what differs")


def near(a: list, b: list, gap: float = 1.0) -> bool:
    """Boxes [w, s, e, n] that overlap or come within `gap` degrees of each other, across the date line too."""
    if b[3] + gap < a[1] or a[3] + gap < b[1]:
        return False
    def span(x):
        w = x[0] % 360
        return w, w + ((x[2] - x[0]) % 360 or 360)
    (aw, ae), (bw, be) = span(a), span(b)
    return any(bw + k <= ae + gap and aw <= be + k + gap for k in (-360, 0, 360))


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
        country = where.get("country")
        if not (isinstance(country, str) and 2 <= len(country.strip()) <= 30):
            errors.append(f"{w}.where: country is required (at most 30 characters): the country or region a reader "
                          "knows, e.g. 'Bulgaria', 'India', 'North Pacific'. The viewer shows it before the title")
        for k in ("start", "end"):
            t(it.get("when", {}).get(k, ""), f"{w}.when.{k}", errors)
        for k, text in public_texts(it):
            m = PROCESS.search(text)
            if m:
                errors.append(f"{w}.{k}: {m.group(0)!r} is about how the sweep was made, not the weather; viewers read "
                              "this. Rewrite it for them, and put method caveats in the feed's `gaps` or your report")
            m = VORTEX_NAME.search(text)
            if m:
                errors.append(f"{w}.{k}: {m.group(0)!r} is not standard; write 'the polar vortex' or "
                              f"'the {m.group(1).capitalize()} polar vortex'")
            m = HEDGE.search(text)
            if m:
                errors.append(f"{w}.{k}: {m.group(0)!r} is a disclaimer about what the text doesn't say; state what is "
                              "known and how certain it is in plain words instead")
            if k == "confidence" and SOURCE_CONFIDENCE.search(text):
                warnings.append(f"{w}.confidence: {SOURCE_CONFIDENCE.search(text).group(0)!r} says the warning exists, "
                                "not how sure the weather is; say how confident the forecast is (e.g. 'High that the "
                                "heaviest rain falls on the eastern slopes; moderate in local totals')")
            m = None if k.startswith("evidence") else GRID_COORD.search(text)
            if m:
                warnings.append(f"{w}.{k}: {m.group(0)!r} is a grid point, not a place; name the sea, coast or region, "
                                "or round to whole degrees")
            m = MODEL_VS_WARNING.search(text)
            if m:
                warnings.append(f"{w}.{k}: {m.group(0)[:80]!r}: if this compares a rate (mm/h) with a warning's total, "
                                "compare totals instead (a `rain24` scene, 24 h totals) before saying the model is weaker")
        why = " ".join(str(it.get(k, "")) for k in ("summary", "why_interesting")).lower()
        for j, tp in enumerate(it.get("talking_points") or []):
            if len(str(tp)) > 20 and str(tp).strip().rstrip(".").lower() in why:
                warnings.append(f"{w}.talking_points[{j}]: repeats the summary or why_interesting; give a question or "
                                "point a presenter can pick up (e.g. 'Why does the rain stall over the coast?')")
        try:
            peak = fields.parse_time(it.get("when", {}).get("peak", ""))
            gen = fields.parse_time(feed.get("generated_utc", ""))
            if it.get("status") == "forecast" and peak < gen - timedelta(hours=6):
                warnings.append(f"{w}: status is forecast but the peak ({fields.iso(peak)}) is before publication; "
                                "is the event already fading? Say so, or choose a newer one")
            end = fields.parse_time(it.get("when", {}).get("end", ""))
            if end < gen + timedelta(hours=12) and it.get("kind") != "explainer":
                warnings.append(f"{w}: the event ends at {fields.iso(end)}, within 12 h of publication, so most people "
                                "will see it after it is over; prefer an event with more ahead of it, or a later stage")
        except Exception:
            pass
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
            if VACUOUS.search(text):
                errors.append(f"{w}.evidence[{j}]: {VACUOUS.search(text).group(0)!r} says nothing; state what the source "
                              "shows, with its number (e.g. 'NASA Ozone Watch: 9 m/s at 60°N on 1 Nov, the lowest for "
                              "the date since 1979')")
            elif e.get("kind") in {"field", "ensemble"} and not re.search(r"\d", text.split(":", 1)[-1]):
                warnings.append(f"{w}.evidence[{j}]: {e['kind']} evidence with no number; give the value that matters")
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
            # Layers with app_ready=False are drawn by the web viewer only; the iPhone app is not
            # kept up to date (since Nov 2026), so they are allowed.
            if sc.get("fill") not in fields.LAYERS:
                errors.append(f"{sw}: fill {sc.get('fill')!r}; use one of {sorted(fields.LAYERS)}")
            if sc.get("contours") not in (None, *CONTOUR_LAYERS):
                errors.append(f"{sw}: contours must be one of {sorted(CONTOUR_LAYERS)} or omitted")
            vectors_ok = sorted(VECTOR_LAYERS)
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
                m = CAPTION_TIME.search(str(c.get("text", "")))
                if m:
                    errors.append(f"{sw}: caption {str(c.get('text', ''))[:50]!r} starts with a time; the viewer's clock "
                                  "beside it already shows the time, in the viewer's own zone. Say what happens "
                                  "('Winds reach 38 m/s west of Chichijima')")
                ca = t(c.get("at", ""), f"{sw}.captions", errors)
                if c.get("until"):
                    cu = t(c["until"], f"{sw}.captions.until", errors)
                    if ca and cu and cu <= ca:
                        errors.append(f"{sw}: caption until {c['until']} must be after its at {c['at']}")
            if len(sc.get("captions", [])) > 6:
                errors.append(f"{sw}: at most 6 captions")
            par = sc.get("parallels")
            if par is not None and not (isinstance(par, list) and len(par) <= 3
                                        and all(isinstance(x, (int, float)) and -89 <= x <= 89 for x in par)):
                errors.append(f"{sw}: parallels must be a list of up to 3 latitudes, e.g. [60]")
            box = sc.get("bbox")
            if isinstance(box, list) and len(box) == 4:
                bw, bs, be, bn = box
                for f in sc.get("focus") or []:
                    lon, lat = f.get("lon", 0), f.get("lat", 0)
                    inside = bs <= lat <= bn and ((lon - bw) % 360) <= ((be - bw) % 360 or 360)
                    if not inside:
                        warnings.append(f"{sw}: focus {f.get('label')!r} lies outside the scene box, so it isn't on the map")
            check_captions(sc, sw, warnings)
            check_caption_values(sc, sw, warnings)
    check_variety(items, warnings)
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
