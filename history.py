"""Historical events: research a past event with Sol and build its scenes from ERA5
(see docs/historical-events-proposal.md in the project). Run in GitHub Actions or by hand, never on
the Replit server (§2 of AGENTS.md): each step is a one-off batch job per event.

  uv run --frozen --extra history python history.py research <event id> --events events.json --out items/
  uv run --frozen --extra history python history.py review items/<id>.json --run <run folder> > review.md
  uv run --frozen --extra history python history.py build items/<id>.json [--out data/history]

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
    for head, key in (("Impacts", "impacts"), ("The forecast at the time", "history"), ("Why it's interesting", "why_interesting")):
        entries = it.get(key) or []
        if isinstance(entries, list) and entries:
            out += [f"## {head}", ""] + [f"- {e['text']} {refs(e)}" for e in entries] + [""]
    out += ["## What the reanalysis shows", "", it.get("reanalysis", ""), "",
            "| | ERA5 | Observed |", "|---|---|---|"]
    out += [f"| {r['quantity']} | {r['era5']} | {r['observed']} {refs(r)} |" for r in it.get("era5_vs_observed", [])]
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
    v = sub.add_parser("review")
    v.add_argument("item", type=Path)
    v.add_argument("--run", type=Path)
    a = ap.parse_args()
    if a.cmd == "build":
        build(a.item, a.out)
    elif a.cmd == "research":
        research(a.event, a.events, a.out)
    else:
        print(review(a.item, a.run))


if __name__ == "__main__":
    main()
