#!/usr/bin/env python3
"""Validate a highlights feed and publish it as data/highlights/latest.json (plus a dated copy).

Usage (from the backend directory):
  uv run python .claude/skills/weather-highlights/scripts/publish_highlights.py draft.json

Refuses to publish when the feed breaks the schema in references/feed-schema.md and lists every
problem. Warnings (for example a scene outside the Open Data time range) do not block publishing.
The file is replaced atomically, so clients never read a half-written feed.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(BACKEND))
import fields  # noqa: E402

OUT = BACKEND / "data" / "highlights"
SCHEMA_VERSION = 1
HAZARDS = {"wind", "rain", "flood", "snow", "ice", "heat", "cold", "tropical-cyclone", "convection",
           "drought", "fire-weather", "fog", "dust", "coastal", "other"}
STATUSES = {"observed", "ongoing", "forecast"}
CONTOUR_LAYERS = {"mslp", "z500"}
VECTOR_LAYERS = {"wind10", "jet250"}


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


def validate(feed: dict) -> tuple[list[str], list[str]]:
    errors, warnings = [], []
    for k in ("generated_utc", "items"):
        if k not in feed:
            errors.append(f"missing top-level {k!r}")
    items = feed.get("items", [])
    if not 1 <= len(items) <= 12:
        errors.append(f"items: expected 1-12, got {len(items)}")
    latest = fields.latest_run()
    lo, hi = latest - timedelta(hours=72), latest + timedelta(hours=360)
    ids = set()
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
            if e.get("kind") in {"warning", "news", "official", "observation"} and not str(e.get("url", "")).startswith("http"):
                errors.append(f"{w}.evidence[{j}]: {e.get('kind')} evidence needs a real url")
        scenes = it.get("scenes", [])
        if not 1 <= len(scenes) <= 4:
            errors.append(f"{w}: expected 1-4 scenes")
        for j, sc in enumerate(scenes):
            sw = f"{w}.scenes[{j}]"
            if sc.get("fill") not in fields.LAYERS:
                errors.append(f"{sw}: fill {sc.get('fill')!r} not in {sorted(fields.LAYERS)}")
            if sc.get("contours") not in (None, *CONTOUR_LAYERS):
                errors.append(f"{sw}: contours must be one of {sorted(CONTOUR_LAYERS)} or omitted")
            if sc.get("vectors") not in (None, *VECTOR_LAYERS):
                errors.append(f"{sw}: vectors must be one of {sorted(VECTOR_LAYERS)} or omitted")
            check_bbox(sc.get("bbox"), sw, errors)
            s0, s1 = t(sc.get("start", ""), f"{sw}.start", errors), t(sc.get("end", ""), f"{sw}.end", errors)
            if s0 and s1:
                if s1 <= s0:
                    errors.append(f"{sw}: end must be after start")
                if not str(sc.get("fill", "")).startswith("ens_") and (s0 < lo or s1 > hi):
                    warnings.append(f"{sw}: window partly outside Open Data range {fields.iso(lo)}..{fields.iso(hi)}; frames will be clipped")
            for c in sc.get("captions", []):
                t(c.get("at", ""), f"{sw}.captions", errors)
    return errors, warnings


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    feed = json.loads(Path(sys.argv[1]).read_text())
    feed.setdefault("schema_version", SCHEMA_VERSION)
    feed.setdefault("attribution", fields.ATTRIBUTION.format(year=datetime.now(timezone.utc).year))
    errors, warnings = validate(feed)
    for w in warnings:
        print("WARNING", w)
    if errors:
        print("NOT PUBLISHED - fix these and run again:")
        for e in errors:
            print("  -", e)
        sys.exit(1)
    OUT.mkdir(parents=True, exist_ok=True)
    text = json.dumps(feed, indent=1, ensure_ascii=False)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")
    (OUT / f"highlights-{stamp}.json").write_text(text)
    tmp = OUT / ".latest.json.tmp"
    tmp.write_text(text)
    os.replace(tmp, OUT / "latest.json")
    print(f"published {len(feed['items'])} highlights -> {OUT / 'latest.json'} (archive highlights-{stamp}.json)")


if __name__ == "__main__":
    main()
