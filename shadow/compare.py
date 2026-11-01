"""Compare a shadow run's candidate feed with production's feed for the same sweep slot.

  uv run compare.py runs/<stamp>            writes runs/<stamp>/compare.json and compare.html
  uv run compare.py runs/<stamp> --production FEED.json    use a given production feed
  uv run compare.py --pending               every run not yet compared whose production sweep is out

Production's feed is its sweep nearest the shadow run's start (3 h before to 5 h after), read from
the public web archive (data/archive.json and data/items/). The same automatic checks run on both:

  caption_window   caption time outside its scene's start..end
  day_night        caption says night/day/morning/evening while the local solar time at the scene
                   (UTC + longitude/15 at the box centre) says otherwise
  peak_time        a caption quotes the item's peak value ("up to 31 m/s" in the summary or evidence)
                   more than 3 h from the item's peak time (the Great Plains jet case of 31 October)
  regional_prob    a probability stated as covering an area ("100% ... over an area of"), rather than
                   a local maximum
  evidence_kind    'field' (HRES) evidence quoting ensemble probabilities, or 'ensemble' evidence without one
  past_window      an item whose window ended before the feed was generated but isn't marked observed
  imperative       captions that read as instructions ("Inspect…", "Compare…") rather than narration;
                   counted, not an error

These are screens, not a fact-check: each flag needs a human look.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sol_runner import RUNS, WEB, get_json

NIGHT = re.compile(r"\b(night|overnight|nocturnal|after dark)\b", re.I)
DAY = re.compile(r"\b(daytime|afternoon|midday|by day)\b", re.I)
MORNING = re.compile(r"\b(morning|dawn|sunrise)\b", re.I)
EVENING = re.compile(r"\b(evening|dusk|sunset)\b", re.I)
REGIONAL = re.compile(r"\d+\s?%[^.]{0,80}\b(over|across|covering)\s+(an?\s+)?(area|region)\b", re.I)
ENS_WORDS = re.compile(r"\b(ENS|ensemble|probabilit|\d+\s?%\s+chance)", re.I)
IMPERATIVE = re.compile(r"^(inspect|compare|follow|look|watch|note|see|check|track|examine|observe|consider)\b", re.I)
# Wind and rain amounts, not thresholds (">25 m/s", "above 50 mm") or pressures (levels, central pressure).
VALUE = re.compile(r"(?<![>\d.])(?<!above )(?<!over )(?<!exceeding )(?<!more than )(?<!at least )(?<!than )"
                   r"(\d+(?:\.\d+)?)\s?(m/s|mm|km/h|mph|kt)\b")
PEAK_WORDS = re.compile(r"\b(up to|peak|maximum|max|reaches|reaching|highest|strongest)\b", re.I)


def peak_values(it: dict) -> set[tuple[float, str]]:
    """Amounts the item presents as its peak: number+unit in a sentence that says 'up to', 'peak'…"""
    text = " ".join([it.get("summary", "")] + [e.get("text", "") for e in it.get("evidence", [])])
    return {(float(v), u) for s in re.split(r"(?<=[.;])\s", text) if PEAK_WORDS.search(s) for v, u in VALUE.findall(s)}


def t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def solar_hour(when: datetime, lon: float) -> float:
    return (when.hour + when.minute / 60 + lon / 15) % 24


def centre_lon(box) -> float:
    w, e = box[0], box[2]
    return (w + ((e - w) % 360) / 2 + 180) % 360 - 180


def checks(feed: dict) -> list[dict]:
    flags = []
    gen = t(feed["generated_utc"]) if feed.get("generated_utc") else None

    def flag(kind, item, text):
        flags.append({"check": kind, "item": item.get("title"), "detail": text})

    for it in feed.get("items", []):
        peaks = peak_values(it)
        peak = it.get("when", {}).get("peak")
        for sc in it.get("scenes", []):
            s0, s1 = t(sc["start"]), t(sc["end"])
            lon = centre_lon(sc["bbox"])
            for c in sc.get("captions", []):
                at = t(c["at"])
                if not s0 <= at <= s1:
                    flag("caption_window", it, f"{c['at']} '{c['text']}' outside {sc['start']}..{sc['end']}")
                h = solar_hour(at, lon)
                says = ("night" if NIGHT.search(c["text"]) else "day" if DAY.search(c["text"])
                        else "morning" if MORNING.search(c["text"]) else "evening" if EVENING.search(c["text"]) else None)
                ok = {"night": h >= 19 or h < 6, "day": 9 <= h < 17, "morning": 4 <= h < 11, "evening": 16 <= h < 22}
                if says and not ok[says]:
                    flag("day_night", it, f"'{c['text']}' at {c['at']} is about {h:.0f}:00 local solar time")
                quoted = {(float(v), u) for v, u in VALUE.findall(c["text"])} & peaks
                if quoted and peak and abs(at - t(peak)) > timedelta(hours=3):
                    v, u = sorted(quoted)[-1]
                    flag("peak_time", it, f"'{c['text']}' at {c['at']} quotes the peak {v:g} {u}, but the item's peak is {peak}")
        text = " ".join([it.get("summary", ""), it.get("why_interesting", "")])
        if REGIONAL.search(text):
            flag("regional_prob", it, REGIONAL.search(text).group(0))
        for e in it.get("evidence", []):
            if e.get("kind") == "field" and re.search(r"\d+\s?%\s+(chance|probability)|\bENS\b", e.get("text", "")):
                flag("evidence_kind", it, f"field evidence quotes the ensemble: {e['text'][:120]}")
            if e.get("kind") == "ensemble" and not ENS_WORDS.search(e.get("text", "")):
                flag("evidence_kind", it, f"ensemble evidence without a probability: {e['text'][:120]}")
        end = it.get("when", {}).get("end")
        if gen and end and t(end) < gen and it.get("status") != "observed":
            flag("past_window", it, f"window ended {end}, before the feed ({feed['generated_utc']}), status {it.get('status')}")
    return flags


def stats(feed: dict) -> dict:
    items = feed.get("items", [])
    caps = [c["text"] for i in items for s in i.get("scenes", []) for c in s.get("captions", [])]
    ev = {}
    for i in items:
        for e in i.get("evidence", []):
            ev[e.get("kind")] = ev.get(e.get("kind"), 0) + 1
    return {"generated": feed.get("generated_utc"), "runs": feed.get("runs"), "items": len(items),
            "explainers": sum(1 for i in items if i.get("kind") == "explainer"),
            "scenes": sum(len(i.get("scenes", [])) for i in items), "captions": len(caps),
            "imperative_captions": sum(1 for c in caps if IMPERATIVE.match(c)), "evidence": ev}


def overlap(a, b) -> float:
    """Share of the smaller box covered by the other (longitudes on a circle)."""
    def span(w, e):
        return w, w + ((e - w) % 360 or 360)
    (aw, ae), (bw, be) = span(a[0], a[2]), span(b[0], b[2])
    ns = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    ew = max(max(0.0, min(ae, be + k) - max(aw, bw + k)) for k in (-360, 0, 360))
    small = min((ae - aw) * (a[3] - a[1]), (be - bw) * (b[3] - b[1]))
    return ew * ns / small if small > 0 else 0.0


def pair(prod: dict, cand: dict) -> list[dict]:
    """Match items by place: same event if their boxes overlap by half or more."""
    rows, used = [], set()
    for p in prod.get("items", []):
        best = max(((overlap(p["where"]["bbox"], c["where"]["bbox"]), k) for k, c in enumerate(cand.get("items", []))
                    if k not in used), default=(0, None))
        if best[0] >= 0.5:
            used.add(best[1])
            rows.append({"production": p, "shadow": cand["items"][best[1]]})
        else:
            rows.append({"production": p, "shadow": None})
    rows += [{"production": None, "shadow": c} for k, c in enumerate(cand.get("items", [])) if k not in used]
    return rows


def production_for(started: datetime) -> dict | None:
    """Production's sweep for the shadow run's slot: the one published nearest the run's start, from
    3 h before (a run that started late) to 5 h after. None if it isn't out yet."""
    archive = get_json(f"{WEB}/archive.json")
    near = []
    for tag in {r.split("/")[0] for r in archive if not r.startswith("req")}:
        when = datetime.strptime(tag, "%Y%m%dT%H%MZ").replace(tzinfo=timezone.utc)
        if started - timedelta(hours=3) <= when <= started + timedelta(hours=5):
            near.append((abs(when - started), tag, when))
    if not near:
        return None
    _, tag, when = min(near)
    refs = [r for r in archive if r.startswith(tag + "/")]
    items = sorted((get_json(f"{WEB}/items/{r}.json") for r in refs), key=lambda i: i.get("rank", 99))
    feed = get_json(f"{WEB}/feed.json")
    runs = feed.get("runs") if feed.get("generated_utc") == f"{when:%Y-%m-%dT%H:%MZ}" else None
    return {"generated_utc": f"{when:%Y-%m-%dT%H:%MZ}", "runs": runs, "items": items}


def item_html(it: dict | None) -> str:
    if not it:
        return '<td class="none">—</td>'
    caps = "".join(f"<li><code>{html.escape(c['at'][5:16])}</code> {html.escape(c['text'])}</li>"
                   for s in it.get("scenes", []) for c in s.get("captions", []))
    ev = "".join(f"<li><b>{html.escape(str(e.get('kind')))}</b> {html.escape(e.get('text', '')[:220])}</li>"
                 for e in it.get("evidence", []))
    return (f"<td><div class=t>{it.get('rank')}. {html.escape(it.get('title', ''))}"
            f"{' <span class=k>explainer</span>' if it.get('kind') == 'explainer' else ''}</div>"
            f"<p>{html.escape(it.get('summary', ''))}</p><p class=c>Confidence: {html.escape(str(it.get('confidence', '')))}</p>"
            f"<details><summary>Captions ({sum(len(s.get('captions', [])) for s in it.get('scenes', []))})</summary><ul>{caps}</ul></details>"
            f"<details><summary>Evidence ({len(it.get('evidence', []))})</summary><ul>{ev}</ul></details></td>")


def page(run: Path, report: dict, rows: list[dict]) -> str:
    r = report
    def side(name):
        s, f, res = r[name]["stats"], r[name]["flags"], r[name].get("run")
        lines = [f"{s['items']} items ({s['explainers']} explainers), {s['scenes']} scenes, generated {s['generated']}, "
                 f"model runs {s['runs']}", f"Imperative captions: {s['imperative_captions']} of {s['captions']}",
                 f"Evidence: {s['evidence']}"]
        if res:
            lines.append(f"{res}")
        fl = "".join(f"<li><b>{html.escape(x['check'])}</b> — {html.escape(str(x['item']))}: {html.escape(x['detail'])}</li>" for x in f)
        return "<br>".join(html.escape(l) for l in lines) + f"<h4>Flags ({len(f)})</h4><ul>{fl or '<li>none</li>'}</ul>"
    body = "".join(f"<tr>{item_html(x['production'])}{item_html(x['shadow'])}</tr>" for x in rows)
    return f"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Shadow comparison {run.name}</title><style>
body{{font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;margin:16px;background:#fff;color:#1b1f24}}
table{{border-collapse:collapse;width:100%;table-layout:fixed}}td,th{{border:1px solid #d5dae0;padding:8px;vertical-align:top}}
th{{background:#f2f4f7;text-align:left}}.t{{font-weight:650}}.k{{font-size:11px;background:#e8eefc;padding:1px 6px;border-radius:8px}}
.c{{color:#5b6573;font-size:12.5px}}.none{{color:#9aa3ad;text-align:center}}code{{font-size:12px;color:#5b6573}}ul{{padding-left:18px;margin:4px 0}}
@media (prefers-color-scheme:dark){{body{{background:#14171c;color:#e6e9ee}}td,th{{border-color:#2c323b}}th{{background:#1c2128}}.k{{background:#24314d}}}}
</style></head><body><h2>Production (Claude) vs shadow ({html.escape(r['shadow'].get('model', 'Sol'))}) — {run.name}</h2>
<p>Automatic screens only, not a fact-check: each flag needs a look. Items are paired when their boxes overlap by half.</p>
<table><tr><th>Production</th><th>Shadow</th></tr><tr><td>{side('production')}</td><td>{side('shadow')}</td></tr>{body}</table></body></html>"""


def compare(run: Path, production: Path | None = None) -> bool:
    """Write compare.json and compare.html for one run. False if production's sweep isn't out yet."""
    result = json.loads((run / "result.json").read_text())
    cand = json.loads((run / "candidate.json").read_text())
    prod = json.loads(production.read_text()) if production else production_for(t(result["started"]))
    if prod is None:
        if datetime.now(timezone.utc) - t(result["started"]) > timedelta(hours=6):
            (run / "compare.json").write_text(json.dumps({"production": None, "note": "no production sweep in this slot"}))
            print(f"{run.name}: no production sweep in this slot")
        else:
            print(f"{run.name}: production's sweep for this slot isn't out yet")
        return False
    (run / "production.json").write_text(json.dumps(prod, ensure_ascii=False))
    run_note = (f"{result['minutes']} min, usage {json.dumps(result.get('usage'))[:200]}, "
                f"validator {'ok' if result['validation'].get('ok') else 'failed'}")
    report = {"production": {"stats": stats(prod), "flags": checks(prod)},
              "shadow": {"model": result.get("model"), "stats": stats(cand), "flags": checks(cand), "run": run_note}}
    rows = pair(prod, cand)
    report["pairs"] = {"both": sum(1 for x in rows if x["production"] and x["shadow"]),
                       "production_only": sum(1 for x in rows if not x["shadow"]),
                       "shadow_only": sum(1 for x in rows if not x["production"])}
    (run / "compare.json").write_text(json.dumps(report, indent=1, ensure_ascii=False))
    (run / "compare.html").write_text(page(run, report, rows))
    print(run.name)
    for side in ("production", "shadow"):
        s = report[side]
        print(f"{side:>10}: {s['stats']['items']} items, {s['stats']['scenes']} scenes, "
              f"{len(s['flags'])} flags {sorted({f['check'] for f in s['flags']})}, "
              f"imperative captions {s['stats']['imperative_captions']}/{s['stats']['captions']}")
    print(f"     pairs: {report['pairs']}  -> {run / 'compare.html'}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", type=Path, nargs="?")
    ap.add_argument("--production", type=Path)
    ap.add_argument("--pending", action="store_true")
    a = ap.parse_args()
    if a.pending:
        for run in sorted(RUNS.glob("*")):
            if (run / "candidate.json").exists() and not (run / "compare.json").exists():
                compare(run)
        return 0
    if not a.run:
        ap.error("give a run folder or --pending")
    return 0 if compare(a.run, a.production) else 1


if __name__ == "__main__":
    sys.exit(main())
