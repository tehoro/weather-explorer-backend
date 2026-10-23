"""Requested highlights: an event someone asked for, researched once and shared with everyone.

Anyone with the app can ask for a weather situation the feed doesn't cover. Claude researches it
with the weather-highlights skill (one request at a time, never alongside a sweep), its scene data
is built, and it is listed for everyone for 48 hours, separately from the sweep's feed, so a new
sweep doesn't remove it. Requests are anonymous: the device ID is used only in memory, for the
per-device limit, and is never stored or shown.

  data/requests/active.json       every live request (preparing, ready, none or failed)
  data/requests/<id>.json         a ready request's one-item feed
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import time
import threading
from functools import wraps
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import agent
import scenes
import scene_job
import store

DIR = store.REQUESTS
ACTIVE = DIR / "active.json"
KEEP_HOURS = float(os.environ.get("REQUEST_HOURS", "48"))
PER_DEVICE_PER_DAY = int(os.environ.get("REQUESTS_PER_DEVICE_PER_DAY", "3"))
PER_DAY = int(os.environ.get("REQUESTS_PER_DAY", "12"))     # all devices together (a cost cap)
QUEUE_MAX = 3

_by_device: dict[str, deque[float]] = defaultdict(deque)     # hashed device -> request times (memory only)
_queue: asyncio.Queue | None = None
_loop: asyncio.AbstractEventLoop | None = None
_lock = threading.RLock()


def locked(fn):
    @wraps(fn)
    def call(*args, **kwargs):
        with _lock:
            return fn(*args, **kwargs)
    return call


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%MZ")


def _time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _load() -> list[dict]:
    try:
        return json.loads(ACTIVE.read_text())
    except Exception:
        return []


def _save(entries: list[dict]) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = ACTIVE.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=1))
    tmp.replace(ACTIVE)
    store.save_requests()


@locked
def listing() -> list[dict]:
    """Live requests, newest first. Expired ones are removed, with their scene files."""
    entries, keep = _load(), []
    for e in entries:
        if _time(e["expires_utc"]) > _now():
            keep.append(e)
        else:
            scenes.remove(f"req{e['id']}")
            (DIR / f"{e['id']}.json").unlink(missing_ok=True)
    if len(keep) != len(entries):
        _save(keep)
    out = []
    for e in sorted(keep, key=lambda e: e["requested_utc"], reverse=True):
        o = {k: e[k] for k in ("id", "status", "topic", "requested_utc", "expires_utc", "message") if e.get(k)}
        if e["status"] == "ready":
            try:
                o["item"] = json.loads((DIR / f"{e['id']}.json").read_text())["items"][0]
            except Exception:
                continue
        out.append(o)
    return out


def clean_topic(text: str) -> str:
    text = re.sub(r"[\x00-\x1f<>]", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:200]


@locked
def submit(text: str, device: str) -> tuple[bool, str]:
    """Queue a request. Returns (accepted, a message for the viewer)."""
    topic = clean_topic(text)
    if len(topic) < 8:
        return False, "Please describe the weather event in a few more words."
    now = time.time()
    times = _by_device[hashlib.sha256(device.encode()).hexdigest()]
    while times and now - times[0] > 86400:
        times.popleft()
    if len(times) >= PER_DEVICE_PER_DAY:
        return False, f"Each device can request {PER_DEVICE_PER_DAY} events a day. Please try again tomorrow."
    entries = _load()
    if sum(_time(e["requested_utc"]) > _now() - timedelta(days=1) for e in entries) >= PER_DAY:
        return False, "Today's limit for requested events has been reached. Please try again tomorrow."
    if _queue is None or _loop is None or sum(e["status"] == "preparing" for e in entries) >= QUEUE_MAX:
        return False, "Several requests are already being prepared. Please try again in a little while."
    entry = {"id": secrets.token_hex(4), "status": "preparing", "topic": topic, "requested_utc": _iso(_now()),
             "expires_utc": _iso(_now() + timedelta(hours=KEEP_HOURS))}
    entries.append(entry)
    _save(entries)
    times.append(now)
    _loop.call_soon_threadsafe(_queue.put_nowait, entry["id"])
    return True, ("Thanks. The event is being researched, which usually takes about 5-10 minutes. It will "
                  "appear under Requested for everyone for 48 hours.")


@locked
def _update(rid: str, **changes) -> None:
    entries = _load()
    for e in entries:
        if e["id"] == rid:
            e.update(changes)
    _save(entries)


async def worker(busy: asyncio.Lock) -> None:
    """Research queued requests one at a time. `busy` is shared with the sweep so they never overlap."""
    global _queue, _loop
    _loop = asyncio.get_running_loop()
    _queue = asyncio.Queue()
    for e in _load():                     # requests interrupted by a restart
        if e["status"] == "preparing":
            _queue.put_nowait(e["id"])
    while True:
        rid = await _queue.get()
        entry = next((e for e in _load() if e["id"] == rid), None)
        if entry:
            async with busy:
                await _research(entry)


async def _research(entry: dict) -> None:
    rid = entry["id"]
    out, draft = DIR / f"{rid}.json", DIR / f"{rid}.draft.json"

    def log(line: str) -> None:
        print(f"[request {rid}] {line}", flush=True)

    try:
        report = await agent.run_request(entry["topic"], draft, out, log)
    except Exception as e:
        log(f"failed: {e}")
        await asyncio.to_thread(_update, rid, status="failed", message="Sorry, researching this event failed. Please try again later.")
        return
    draft.unlink(missing_ok=True)
    if not out.exists():
        m = re.search(r"RESULT:\s*none\s*[-:–—]\s*(.+)", report or "")
        await asyncio.to_thread(_update, rid, status="none", message=(m.group(1).strip()[:300] if m else "No matching weather situation was found."))
        log("nothing to publish")
        return
    try:
        feed = json.loads(out.read_text())
        item = feed["items"][0]
        if not item["id"].startswith("req-"):
            item["id"] = f"req-{item['id']}"
        out.write_text(json.dumps(feed, ensure_ascii=False, indent=1))
        await scene_job.build(out, f"req{rid}")
        await asyncio.to_thread(store.save_request_file, out)
        await asyncio.to_thread(_update, rid, status="ready")
        log("ready")
    except Exception as e:
        log(f"could not finish: {e}")
        await asyncio.to_thread(_update, rid, status="failed", message="Sorry, this event couldn't be prepared. Please try again later.")
