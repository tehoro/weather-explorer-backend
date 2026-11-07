"""Historical events: research a past event with Sol and build its scenes from ERA5
(see docs/historical-events-proposal.md in the project). Run in GitHub Actions or by hand, never on
the Replit server (§2 of AGENTS.md): each step is a one-off batch job per event.

  uv run --frozen --extra history python history.py research <event id> --events events.json --out items/
  uv run --frozen --extra history python history.py review items/<id>.json --run <run folder> > review.md
  uv run --frozen --extra history python history.py build items/<id>.json [--out data/history]
  uv run --frozen --extra history python history.py build3d <out>/<id> --web ../web

research  scans ERA5 for the brief in events.json ({"events": [{id, title, country, start, end, bbox,
          notes?, places?}]}), has Sol (openai_agent.history, the historical-events skill) write the
          item, and copies the checked draft to <out>/<id>.json. Its run folder (report, check log,
          scan) goes in data/openai-runs/history/.
review    the draft as Markdown for a person to read before it is published: text, comparison
          table, sources, captions against the scan, and Sol's report and check log.
build     the item's scenes from ERA5 (era5.py), with the same layers, boxes and frame counts as live
          scenes, and the main low's track (tracks.py), into

  <out>/<item id>/item.json                       the item, scene "data" naming the files below, and
                                                  scene "track"
  <out>/<item id>/<item id>-<n>-<role>.json.gz    role: fill, contours or vectors

build3d   for a tropical cyclone, the 3D view's bundle from ERA5 (the web repository's build3d.py
          packs it): <web>/history/3d/<id>-<key>/, and item["view3d"] in <out>/<id>/item.json.
          About 100 downloads of 85 MB (all 37 levels of a variable each hour), so 10-30 minutes.

The web repository's history.py turns that folder into the viewer's packages.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

HERE = Path(__file__).resolve().parent
SKILL = HERE / ".claude/skills/historical-events"
RUNS = HERE / "data" / "openai-runs" / "history"


def build(item_path: Path, out: Path) -> Path:
    import era5
    import scenes
    import tracks
    item = json.loads(item_path.read_text())
    if item.get("kind") != "historical":
        raise SystemExit(f"{item_path}: kind must be 'historical'")
    try:   # the event's own time zone, which the viewer offers alongside UTC
        ZoneInfo(item["where"]["tz"])
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
        print(f"[history] {item['id']}: no valid where.tz (an IANA zone such as Europe/London); "
              "the viewer will show UTC only", file=sys.stderr)
    folder = out / item["id"]
    folder.mkdir(parents=True, exist_ok=True)
    written = scenes.build_items([item], "history", source="era5", out_dir=folder)
    points = tracks.add_to_scenes(item, lambda t: (era5.field("msl", t), era5.GRID))
    print(f"[history] {item['id']}: track of {points} hourly points", flush=True)
    for scene in item.get("scenes", []):
        if not scene.get("data") and not scene.get("web_data"):
            print(f"[history] {item['id']}: scene '{scene.get('title')}' has no data", file=sys.stderr)
    (folder / "item.json").write_text(json.dumps(item, ensure_ascii=False, indent=1))
    print(f"[history] {item['id']}: {written} files built in {folder}", flush=True)
    return folder


def build3d(folder: Path, web: Path) -> str | None:
    """The 3D view of a past tropical cyclone, from ERA5, 3-hourly over the item's window (at most
    32 frames, centred on the peak)."""
    import hashlib
    import era5
    import numpy as np
    from concurrent.futures import ThreadPoolExecutor
    sys.path.insert(0, str(web))
    import build3d as b3
    path = folder / "item.json"
    item = json.loads(path.read_text())
    if b3.HAZARD not in (item.get("hazards") or []):
        print(f"[3d] {item['id']}: not a tropical cyclone", flush=True)
        return None
    bbox, res, box = b3.box_for(item)
    when = item["when"]
    t0, t1 = b3.parse_time(when["start"]), b3.parse_time(when["end"])
    peak = b3.parse_time(when.get("peak") or when["start"])
    times = [t0 + 3 * k * b3.H for k in range(int((t1 - t0) / b3.H) // 3 + 1)]
    if len(times) > b3.MAX_FRAMES:
        i = max(0, min(len(times) - b3.MAX_FRAMES, min(range(len(times)), key=lambda k: abs(times[k] - peak)) - b3.MAX_FRAMES // 2))
        times = times[i:i + b3.MAX_FRAMES]
    key = hashlib.sha1(json.dumps([bbox, [b3.iso(t) for t in times], b3.FORMAT]).encode()).hexdigest()[:10]
    rel = f"history/3d/{item['id']}-{key}"
    out = web / rel
    ix = np.rint((box["lons"] + 180) / 0.25).astype(int) % 1440
    iy = np.clip(np.rint((90 - box["lats"]) / 0.25).astype(int), 0, 720)
    cut = lambda a: a[np.ix_(iy, ix)]
    print(f"[3d] {item['id']}: box {bbox} at {res} deg, {len(times)} frames {b3.iso(times[0])} to {b3.iso(times[-1])}", flush=True)

    def frame(t):
        tl, ql = era5.levels("t", t, b3.LEVELS), era5.levels("q", t, b3.LEVELS)
        ul, vl = era5.levels("u", t, b3.WIND_LEVELS), era5.levels("v", t, b3.WIND_LEVELS)
        return {"t": [cut(tl[l]) for l in b3.LEVELS],
                "r": [cut(era5.relative_humidity(ql[l], tl[l], l)) for l in b3.LEVELS],
                "uv": [cut(x[l]) for l in b3.WIND_LEVELS for x in (ul, vl)],
                "uv10": [cut(era5.field("10u", t)), cut(era5.field("10v", t))],
                "msl": cut(era5.field("msl", t)) / 100,
                "rain": cut(era5.interval("tp", t, 3)) * 1000 / 3}

    if not (out / "meta.json").exists():
        with ThreadPoolExecutor(4) as ex:
            frames = dict(enumerate(ex.map(frame, times)))
        b3.write_bundle(item, out, bbox, res, box, times, frames, cut(era5.field("lsm", times[0])),
                        {"source": "ERA5 reanalysis",
                         "attribution": "Contains modified Copernicus Climate Change Service information. "
                                        "ERA5: Hersbach et al. (2020). Regridded."})
    for old in (web / "history" / "3d").glob(f"{item['id']}-*"):
        if old.name != out.name:
            import shutil as sh
            sh.rmtree(old, ignore_errors=True)
    item["view3d"] = rel
    path.write_text(json.dumps(item, ensure_ascii=False, indent=1))
    print(f"[3d] {item['id']}: {rel}", flush=True)
    return rel


def research(event_id: str, events: Path, out: Path) -> Path:
    import openai_agent
    brief = next((e for e in json.loads(events.read_text())["events"] if e["id"] == event_id), None)
    if not brief:
        raise SystemExit(f"{event_id} is not in {events}")
    RUNS.mkdir(parents=True, exist_ok=True)
    scan = RUNS / f"{event_id}-scan.json"
    places = ";".join(f"{p['name']}:{p['lat']}:{p['lon']}" for p in brief.get("places", []))
    subprocess.run([sys.executable, str(SKILL / "scripts/scan_era5.py"), "--bbox=" + ",".join(map(str, brief["bbox"])),
                    "--start", brief["start"], "--end", brief["end"], "--places", places, "--out", str(scan)],
                   cwd=HERE, check=True)
    run = openai_agent.history(brief, scan, RUNS, lambda m: print(f"[history] {m}", flush=True))
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy(run / "artifacts" / "history" / "draft.json", out / f"{event_id}.json")
    print(f"[history] drafted {out / f'{event_id}.json'} (run {run})", flush=True)
    print(f"RUN={run}")
    return run


def review(item_path: Path, run: Path | None) -> str:
    """The draft as Markdown, for the pull request."""
    it = json.loads(item_path.read_text())
    ids = {s["id"]: n + 1 for n, s in enumerate(it.get("sources", []))}
    refs = lambda e: "".join(f"[{ids[s]}]" for s in e.get("sources", []) if s in ids)
    out = [f"# {it.get('title')}", "",
           f"**{it['where'].get('country')}** · {it['where'].get('label')} · time zone `{it['where'].get('tz')}`  ",
           f"{it['when'].get('start')} → peak {it['when'].get('peak')} → {it['when'].get('end')} · "
           f"hazards: {', '.join(it.get('hazards', []))}", "", "## What happened", "", it.get("summary", ""), ""]
    section = lambda head, key: ([f"## {head}", ""] + [f"- {e['text']} {refs(e)}" for e in it[key]] + [""]
                                 if isinstance(it.get(key), list) and it[key] else [])
    out += section("The weather", "why_interesting")
    out += ["## What the reanalysis shows", "", it.get("reanalysis", ""), "",
            "| | ERA5 | Observed |", "|---|---|---|"]
    out += [f"| {r['quantity']} | {r['era5']} | {r['observed']} {refs(r)} |" for r in it.get("era5_vs_observed", [])]
    out += [""] + section("Impacts", "impacts") + section("The forecast at the time", "history")
    out += ["", "## Sources", ""]
    out += [f"{n + 1}. [{s['title']}]({s['url']}). {', '.join(str(x) for x in (s.get('publisher'), s.get('year')) if x)}. *{s.get('kind')}*"
            + (f" ([archived]({s['archive_url']}))" if s.get("archive_url") else "") for n, s in enumerate(it.get("sources", []))]
    out += ["", "## Scenes", ""]
    for n, sc in enumerate(it.get("scenes", [])):
        layers = " + ".join(x for x in (sc.get("fill"), sc.get("contours") and f"{sc['contours']} lines",
                                         sc.get("vectors") and f"{sc['vectors']} streaks") if x)
        out += [f"**{n + 1}. {sc.get('title')}**: {layers}; box {sc.get('bbox')}; {sc.get('start')} → {sc.get('end')}", ""]
        out += [f"- `{c['at']}` {c['text']}" for c in sc.get("captions", [])] + [""]
    if run:
        for name, title in (("check.log", "Check"), ("artifacts/report.txt", "Report from the model")):
            f = run / name
            if f.exists():
                out += [f"<details><summary>{title}</summary>", "", "```", f.read_text(errors="replace")[-30000:], "```",
                        "", "</details>", ""]
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("item", type=Path)
    b.add_argument("--out", type=Path, default=HERE / "data" / "history")
    r = sub.add_parser("research")
    r.add_argument("event")
    r.add_argument("--events", type=Path, required=True)
    r.add_argument("--out", type=Path, required=True)
    d = sub.add_parser("build3d")
    d.add_argument("folder", type=Path)
    d.add_argument("--web", type=Path, required=True)
    v = sub.add_parser("review")
    v.add_argument("item", type=Path)
    v.add_argument("--run", type=Path)
    a = ap.parse_args()
    if a.cmd == "build":
        build(a.item, a.out)
    elif a.cmd == "build3d":
        build3d(a.folder, a.web)
    elif a.cmd == "research":
        research(a.event, a.events, a.out)
    else:
        print(review(a.item, a.run))


if __name__ == "__main__":
    main()
