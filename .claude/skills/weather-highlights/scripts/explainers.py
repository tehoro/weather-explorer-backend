"""Explainer topics: the local winds the scan looks for, and which explainers recent sweeps showed.

An explainer should show something new: the same phenomenon (or the same place) is not explained
again within REPEAT_SWEEPS sweeps, and never alongside a main highlight that already covers that
place. The history is a small file beside the feed, which store.py keeps in Replit Object Storage.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[4]
FEED = BACKEND / "data" / "highlights" / "latest.json"
HISTORY = FEED.parent / "explainer_history.json"
REPEAT_SWEEPS = 2                                   # a topic rests this long: two days at one sweep a day
KEEP_SWEEPS = 8
SAME_SWEEP = timedelta(hours=3)                    # a republish within this is the same sweep

# name, kind, core box [w, s, e, n], wind-from sector (deg), threshold (m/s), level, note
LOCAL_WINDS = [
    ("Greenland tip jet", "tip jet", [-46, 57.5, -38, 61.5], (240, 320), 25, "10m",
     "Westerly flow forced round Cape Farewell accelerates into a narrow jet off Greenland's southern tip."),
    ("Greenland reverse tip jet", "tip jet", [-50, 57, -42, 61], (30, 120), 25, "10m",
     "Easterly flow round Cape Farewell: the 'reverse' (easterly) tip jet."),
    ("Piteraq (East Greenland)", "katabatic", [-38.5, 64.5, -35.5, 66.5], (270, 30), 20, "10m",
     "Cold air draining off the Greenland ice sheet into Ammassalik fjord: a violent katabatic wind."),
    ("Bora (Adriatic)", "fall wind", [13.5, 43.0, 16.5, 45.6], (0, 90), 15, "10m",
     "Cold north-easterly air spilling over the Dinaric Alps onto the Adriatic coast in strong gusts."),
    ("Mistral (Gulf of Lion)", "gap wind", [3.5, 41.5, 6.5, 43.3], (300, 30), 15, "10m",
     "Northerly air funnelled down the Rhône valley between the Alps and the Massif Central."),
    ("Novaya Zemlya bora", "fall wind", [52, 69.5, 58, 72.5], (45, 135), 20, "10m",
     "Cold air from the Kara Sea plunging over Novaya Zemlya's mountains onto the Barents Sea."),
    ("Tehuantepecer", "gap wind", [-96, 13.5, -94, 16], (315, 45), 15, "10m",
     "A cold surge from North America squeezed through the Chivela Pass onto the Gulf of Tehuantepec."),
    ("Papagayo jet", "gap wind", [-88.5, 10, -86, 12], (30, 120), 12, "10m",
     "Trade winds accelerated through the gap in Central America's mountains at Lake Nicaragua."),
    ("Santa Ana winds", "fall wind", [-119.5, 33.2, -117, 34.5], (0, 110), 12, "10m",
     "Dry offshore winds descending from the Great Basin through southern California's passes."),
    ("Adélie Land katabatic", "katabatic", [138, -68, 148, -65.5], (120, 210), 22, "10m",
     "Cold, dense air draining off the Antarctic plateau onto Commonwealth Bay: the windiest coast on Earth at sea level."),
    ("Terra Nova Bay katabatic", "katabatic", [162, -75.5, 166.5, -74], (230, 320), 22, "10m",
     "Katabatic outflow from the Antarctic plateau funnelled down Reeves Glacier; it blows the sea ice away and keeps a polynya open."),
    ("Great Plains low-level jet", "low-level jet", [-102, 27, -94, 40], (135, 225), 18, "850",
     "A southerly jet a kilometre up at night, feeding Gulf moisture into Plains thunderstorms."),
    ("Somali (Findlater) jet", "low-level jet", [45, 0, 60, 15], (180, 270), 18, "850",
     "The cross-equatorial jet off East Africa that carries moisture into the Indian monsoon."),
    ("South American low-level jet", "low-level jet", [-66, -26, -58, -14], (315, 45), 18, "850",
     "A northerly jet east of the Andes carrying Amazon moisture towards the storms of the La Plata basin."),
    ("Caribbean low-level jet", "low-level jet", [-80, 11, -70, 16], (45, 135), 18, "850",
     "An easterly jet over the Caribbean Sea, strongest in July and February."),
]
CORE = {name: box for name, _, box, *_ in LOCAL_WINDS}


def overlap(a: list, b: list) -> float:
    """Share of box a (west, south, east, north; may cross 180°) lying inside box b."""
    def span(w, e):
        return w, w + ((e - w) % 360 or 360)
    (aw, ae), (bw, be) = span(a[0], a[2]), span(b[0], b[2])
    ns = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    ew = max(max(0.0, min(ae, be + k) - max(aw, bw + k)) for k in (-360, 0, 360))
    area = (ae - aw) * (a[3] - a[1])
    return ew * ns / area if area > 0 else 0.0


def topic_of(item: dict) -> str | None:
    """An explainer's topic; items from before topics existed are matched to a local wind by place."""
    if item.get("topic"):
        return item["topic"]
    box = item.get("where", {}).get("bbox")
    for name, core in CORE.items():
        if box and overlap(core, box) >= 0.5:
            return name
    return None


def area_of(item: dict) -> list | None:
    """Where an explainer is about: its local wind's core box, else the item's own box."""
    return CORE.get(topic_of(item)) or item.get("where", {}).get("bbox")


def _sweep(feed: dict) -> dict:
    return {"generated_utc": feed.get("generated_utc"),
            "explainers": [{"topic": topic_of(it), "title": it.get("title"), "area": area_of(it)}
                           for it in feed.get("items", []) if it.get("kind") == "explainer"]}


def _load() -> list[dict]:
    """Past sweeps, oldest first, including the feed on show if the history doesn't have it yet."""
    try:
        sweeps = json.loads(HISTORY.read_text())
    except (OSError, ValueError):
        sweeps = []
    try:
        feed = json.loads(FEED.read_text())
        if feed.get("generated_utc") not in {s.get("generated_utc") for s in sweeps}:
            sweeps.append(_sweep(feed))
    except (OSError, ValueError):
        pass
    return sorted(sweeps, key=lambda s: s.get("generated_utc") or "")


def recent() -> list[dict]:
    """Explainers of the last REPEAT_SWEEPS sweeps before this one, newest first."""
    cutoff = datetime.now(timezone.utc) - SAME_SWEEP

    def before(s):
        try:
            return datetime.fromisoformat(s["generated_utc"].replace("Z", "+00:00")) < cutoff
        except (KeyError, AttributeError, ValueError):
            return False
    earlier = [s for s in _load() if before(s)]
    return [e | {"sweep": s["generated_utc"]} for s in reversed(earlier[-REPEAT_SWEEPS:]) for e in s["explainers"]]


def shown_recently(topic: str | None, area: list | None, shown: list[dict]) -> dict | None:
    """The recent explainer with the same topic or (mostly) the same place, if any."""
    for r in shown:
        if topic and topic == r["topic"]:
            return r
        if area and r.get("area") and max(overlap(area, r["area"]), overlap(r["area"], area)) >= 0.5:
            return r
    return None


def record(feed: dict) -> None:
    """Add a newly published feed to the history (call before it replaces latest.json)."""
    sweeps = [s for s in _load() if s.get("generated_utc") != feed.get("generated_utc")] + [_sweep(feed)]
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    tmp = HISTORY.with_suffix(".tmp")
    tmp.write_text(json.dumps(sweeps[-KEEP_SWEEPS:], indent=1, ensure_ascii=False))
    os.replace(tmp, HISTORY)
