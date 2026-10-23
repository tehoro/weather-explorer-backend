"""Weather Explorer server: it researches the highlights and hands them out, nothing else.

The app reads the weather fields from ECMWF Open Data itself and runs its guide on the device, so
every request here is a cheap file read and the server can serve many devices.

  uv run uvicorn server:app --host 0.0.0.0 --port 8000

Environment:
  ANTHROPIC_API_KEY          Claude API key (for the highlights sweep)
  SWEEP_HOURS_UTC            hours (UTC) to rebuild the highlights, default "9,21" on Replit (after
                             the 00 and 12 UTC runs reach Open Data); empty = never
  SWEEP_MIN_HOURS            minimum gap between sweeps (default 6)
  REQUEST_HOURS              how long a requested highlight is listed (default 48)
  DISCOVERY_GIST_ID          GitHub gist the backend keeps updated with its current tunnel address,
                             so installed apps find it with no setup (needs the gh CLI logged in)

Endpoints (Authorization: Bearer <token>; scope in brackets):
  GET  /api/health                         no token
  GET  /api/highlights              [feed] the highlights feed (latest.json) with its age
  GET  /scenes/<tag>/<file>         [feed] a highlight scene's ready-made field data (gzipped JSON)
  GET  /api/highlights/status       [feed] whether a sweep is running, last log lines, next sweep
  GET  /api/requests                [feed] requested highlights (shared, anonymous, 48 h)
  POST /api/requests                [feed] {"text": ...} request an event; X-Device-ID for the limit
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import time
import urllib.request
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse

import agent
import fields
import scenes
import requests_store
import store
import tokens

# Sweeps run at fixed times, after the 00 and 12 UTC runs are on Open Data (about 08 and 20 UTC).
SWEEP_HOURS = sorted(int(h) for h in os.environ.get("SWEEP_HOURS_UTC", "9,21" if store.ON_REPLIT else "").split(",") if h.strip())
# Claude jobs (the sweep and requested highlights) run one at a time on the small server.
BUSY = asyncio.Lock()
SWEEP_MIN_H = float(os.environ.get("SWEEP_MIN_HOURS", "6"))
GIST_ID = os.environ.get("DISCOVERY_GIST_ID", "112fb3a1ba5a2bb8e842f5ea99244b5f")
LAST_SWEEP = store.LAST_SWEEP


class Refresh:
    """At most one background sweep at a time."""

    def __init__(self):
        self.task: asyncio.Task | None = None
        self.log: deque[str] = deque(maxlen=40)
        self.started: float | None = None
        self.last_report = ""

    def note(self, line: str):
        self.log.append(f"{datetime.now(timezone.utc):%H:%M:%S} {line}")
        print("[refresh]", line, flush=True)

    def last_start(self) -> float:
        try:
            return float(LAST_SWEEP.read_text())
        except (OSError, ValueError):
            return agent.FEED.stat().st_mtime if agent.FEED.exists() else 0.0

    def next_allowed(self) -> float:
        return self.last_start() + SWEEP_MIN_H * 3600

    def try_start(self) -> tuple[bool, str]:
        """Start a sweep unless one is running or one started within SWEEP_MIN_H hours."""
        if self.task and not self.task.done():
            return False, f"A sweep is already running (started {int(time.time() - self.started) // 60} min ago)."
        wait = self.next_allowed() - time.time()
        if wait > 0:
            at = datetime.fromtimestamp(self.next_allowed(), timezone.utc)
            return False, (f"The highlights can only be refreshed once every {SWEEP_MIN_H:g} hours. "
                           f"The next refresh is possible at {at:%H:%M} UTC (in {wait / 3600:.1f} h).")
        self.started = time.time()
        LAST_SWEEP.parent.mkdir(parents=True, exist_ok=True)
        LAST_SWEEP.write_text(str(self.started))
        store.save()
        self.log.clear()
        self.task = asyncio.create_task(self._run())
        return True, "Sweep started; the feed updates in a few minutes and the app is notified."

    def start(self) -> str:
        return self.try_start()[1]

    async def _run(self):
        async with BUSY:
            self.note("sweep started")
            try:
                self.last_report = await agent.run_refresh(self.note)
            except Exception as e:
                self.note(f"sweep failed: {e}")
            try:
                n = await asyncio.to_thread(scenes.build)
                self.note(f"scene data: {n} files built")
            except Exception as e:
                self.note(f"scene data failed: {e}")
            await asyncio.to_thread(store.save)

    def status(self) -> dict:
        return {"running": bool(self.task and not self.task.done()), "started": self.started,
                "next_allowed": fields.iso(datetime.fromtimestamp(self.next_allowed(), timezone.utc)),
                "log": list(self.log), "last_report": self.last_report,
                "next_sweep": fields.iso(slots(datetime.now(timezone.utc))[1]) if SWEEP_HOURS else None,
                "sweep_hours_utc": SWEEP_HOURS}


refresh = Refresh()


def tunnel_hostname() -> str | None:
    """Current quick-tunnel address, from cloudflared's local metrics endpoint."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:20241/quicktunnel", timeout=3) as r:
            return json.loads(r.read()).get("hostname") or None
    except Exception:
        return None


async def publish_address():
    """Keep the discovery gist pointing at the current tunnel, so apps need no setup."""
    published = None
    while True:
        host = await asyncio.to_thread(tunnel_hostname)
        if host and host != published:
            body = {"files": {"weather-explorer-backend.json": {"content": json.dumps(
                {"server": f"https://{host}", "updated": fields.iso(datetime.now(timezone.utc))}) + "\n"}}}
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                json.dump(body, f)
            r = await asyncio.to_thread(subprocess.run, ["gh", "api", "-X", "PATCH", f"/gists/{GIST_ID}", "--input", f.name],
                                        capture_output=True, text=True)
            os.unlink(f.name)
            if r.returncode == 0:
                published = host
                print(f"[discovery] published https://{host}", flush=True)
            else:
                print(f"[discovery] gist update failed: {r.stderr.strip()[:200]}", flush=True)
        await asyncio.sleep(60)


async def prune_cache():
    """Delete downloaded fields from runs older than two days, hourly (keeps a cloud disk small)."""
    while True:
        removed = await asyncio.to_thread(fields.prune_cache, 48)
        if removed:
            print(f"[cache] removed {removed} old run folders", flush=True)
        await asyncio.sleep(3600)


def slots(now: datetime) -> tuple[datetime, datetime]:
    """The most recent and the next scheduled sweep times."""
    day = now.replace(minute=0, second=0, microsecond=0)
    times = sorted(day.replace(hour=h) + timedelta(days=d) for d in (-1, 0, 1) for h in SWEEP_HOURS)
    return max(t for t in times if t <= now), min(t for t in times if t > now)


async def scheduler():
    """Sweep at each scheduled hour; after a restart, catch up on a slot missed in the last 3 hours."""
    while True:
        now = datetime.now(timezone.utc)
        last, nxt = slots(now)
        started = refresh.last_start()
        if started < last.timestamp() and now - last < timedelta(hours=3):
            refresh.try_start()
        await asyncio.sleep(min(600, max(5, (nxt - now).total_seconds() + 5)))


async def build_scenes_now():
    try:
        await asyncio.to_thread(scenes.build)
        await asyncio.to_thread(store.save)
    except Exception as e:
        print(f"[scenes] build failed: {e}", flush=True)


@asynccontextmanager
async def lifespan(app):
    await asyncio.to_thread(store.restore)
    tasks = [asyncio.create_task(publish_address()), asyncio.create_task(prune_cache())]
    if await asyncio.to_thread(scenes.missing):
        tasks.append(asyncio.create_task(build_scenes_now()))   # a feed from before scene files existed
    await asyncio.to_thread(store.restore_requests)
    tasks.append(asyncio.create_task(requests_store.worker(BUSY)))
    if SWEEP_HOURS:
        tasks.append(asyncio.create_task(scheduler()))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Weather display backend", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=2048)


def bearer(request: Request) -> str | None:
    h = request.headers.get("authorization", "")
    return h[7:] if h.lower().startswith("bearer ") else None


def require(scope: str):
    def check(request: Request):
        rec = tokens.validate(bearer(request), scope)
        if not rec:
            raise HTTPException(401, f"missing or invalid token for scope '{scope}'")
        return rec
    return check


def feed_age_hours() -> float | None:
    if not agent.FEED.exists():
        return None
    return (time.time() - agent.FEED.stat().st_mtime) / 3600


@app.get("/")
@app.get("/api/health")
def health():
    # "/" too: hosts such as Replit health-check the root path before switching to a new version.
    return {"ok": True, "time": fields.iso(datetime.now(timezone.utc))}


@app.get("/scenes/{tag}/{name}", dependencies=[Depends(require("feed"))])
def scene_file(tag: str, name: str):
    """A highlight scene's ready-made field data (gzipped JSON; never changes once built)."""
    path = scenes.path_for(f"{tag}/{name}")
    if not path:
        raise HTTPException(404, "no such scene file")
    return FileResponse(path, media_type="application/json",
                        headers={"Content-Encoding": "gzip", "Cache-Control": "public, max-age=604800, immutable"})


@app.get("/api/highlights", dependencies=[Depends(require("feed"))])
def highlights():
    if not agent.FEED.exists():
        return JSONResponse({"items": [], "message": "No feed yet"}, status_code=404)
    feed = json.loads(agent.FEED.read_text())
    feed["age_hours"] = round(feed_age_hours(), 1)
    return feed


@app.get("/api/highlights/status", dependencies=[Depends(require("feed"))])
def highlights_status():
    return refresh.status()


@app.get("/api/requests", dependencies=[Depends(require("feed"))])
def requests_list():
    return {"requests": requests_store.listing(), "keep_hours": requests_store.KEEP_HOURS}


@app.post("/api/requests", dependencies=[Depends(require("feed"))])
async def requests_submit(request: Request):
    try:
        text = str((await request.json()).get("text", ""))
    except Exception:
        raise HTTPException(400, 'send {"text": "..."}')
    device = (request.headers.get("x-device-id") or request.client.host if request.client else "unknown")[:64]
    ok, message = requests_store.submit(text, device)
    return JSONResponse({"accepted": ok, "message": message}, status_code=202 if ok else 429)
