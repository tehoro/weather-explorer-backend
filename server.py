"""Weather display backend: highlights feed, field frames and the chat agent, behind token checks.

  uv run uvicorn server:app --host 0.0.0.0 --port 8000

Environment:
  ANTHROPIC_API_KEY          Claude API key (required for the agent and refresh)
  HIGHLIGHTS_EVERY_HOURS     rebuild the highlights feed on this cadence (0 = only on request)
  SWEEP_MIN_HOURS            minimum gap between sweeps, however they are started (default 6)
  AGENT_TURNS_PER_HOUR       chat limit per device (default 60)
  DISCOVERY_GIST_ID          GitHub gist the backend keeps updated with its current tunnel address,
                             so installed apps find it with no setup (needs the gh CLI logged in)

Endpoints (Authorization: Bearer <token>; scope in brackets):
  GET  /api/health                         no token
  GET  /api/layers                  [feed] layer catalogue
  GET  /api/highlights              [feed] the highlights feed (latest.json) with its age
  GET  /api/highlights/status       [feed] whether a sweep is running, last log lines
  POST /api/highlights/refresh      [agent] start a sweep now (429 if one ran in the last 6 h)
  GET  /api/fields?layer=&bbox=w,s,e,n&start=&end=&max_px=&max_frames=   [data]
  WS   /ws/agent                    [agent] chat; the token may also be sent as ?token=
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import time
import urllib.request
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse

import agent
import fields
import scenes
import store
import tokens

# On Replit the backend runs unattended, so rebuild the feed twice a day unless told otherwise.
EVERY_H = float(os.environ.get("HIGHLIGHTS_EVERY_HOURS", "12" if store.ON_REPLIT else "0"))
TURNS_PER_HOUR = int(os.environ.get("AGENT_TURNS_PER_HOUR", "60"))
SWEEP_MIN_H = float(os.environ.get("SWEEP_MIN_HOURS", "6"))
GIST_ID = os.environ.get("DISCOVERY_GIST_ID", "112fb3a1ba5a2bb8e842f5ea99244b5f")
LAST_SWEEP = store.LAST_SWEEP


class Refresh:
    """At most one background sweep at a time; connected apps hear when the feed changes."""

    def __init__(self):
        self.task: asyncio.Task | None = None
        self.log: deque[str] = deque(maxlen=40)
        self.started: float | None = None
        self.last_report = ""
        self.listeners: set[WebSocket] = set()

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
        for ws in list(self.listeners):
            try:
                await ws.send_json({"type": "highlights_updated"})
            except Exception:
                self.listeners.discard(ws)

    def status(self) -> dict:
        return {"running": bool(self.task and not self.task.done()), "started": self.started,
                "next_allowed": fields.iso(datetime.fromtimestamp(self.next_allowed(), timezone.utc)),
                "log": list(self.log), "last_report": self.last_report}


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


async def scheduler():
    while True:
        age = feed_age_hours()
        if age is None or age >= EVERY_H:
            refresh.try_start()
        await asyncio.sleep(1800)


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
    if EVERY_H > 0:
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


@app.get("/api/layers", dependencies=[Depends(require("feed"))])
def layers():
    return fields.layer_catalogue()


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


@app.post("/api/highlights/refresh", dependencies=[Depends(require("agent"))])
async def highlights_refresh():
    ok, message = refresh.try_start()
    status = refresh.status()
    return JSONResponse({"started": ok, "message": message, "next_allowed": status["next_allowed"]},
                        status_code=202 if ok else 429)


# Wide, many-frame requests decode whole global fields; a few at a time keeps memory in bounds
# on a small server (the rest wait their turn).
_field_slots = asyncio.Semaphore(int(os.environ.get("FIELD_CONCURRENCY", "3")))


@app.get("/api/fields", dependencies=[Depends(require("data"))])
async def get_fields(layer: str, bbox: str, start: str | None = None, end: str | None = None,
                     max_px: int = Query(256, ge=32, le=512), max_frames: int = Query(40, ge=1, le=80)):
    if layer not in fields.LAYERS:
        raise HTTPException(400, f"unknown layer; choose from {list(fields.LAYERS)}")
    try:
        async with _field_slots:
            return await asyncio.to_thread(fields.get_frames, layer, bbox, start, end, max_px, max_frames)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))


_turns: dict[str, deque[float]] = defaultdict(deque)


def allow_turn(who: str) -> bool:
    q, now = _turns[who], time.time()
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= TURNS_PER_HOUR:
        return False
    q.append(now)
    return True


@app.websocket("/ws/agent")
async def ws_agent(ws: WebSocket):
    token = bearer(ws) or ws.query_params.get("token")  # type: ignore[arg-type]
    rec = tokens.validate(token, "agent")
    if not rec:
        await ws.close(code=4401)
        return
    await ws.accept()
    send_lock = asyncio.Lock()

    async def send(msg: dict):
        async with send_lock:
            await ws.send_json(msg)

    session = agent.ChatSession(send, refresh.start)
    device = (ws.headers.get("x-device-id") or "unknown")[:64]   # the shared app key is shared, so limit per device
    refresh.listeners.add(ws)
    inbox: asyncio.Queue[str] = asyncio.Queue()

    async def worker():
        await send({"type": "ready"})   # the Claude process starts with the first question
        while True:
            prompt = await inbox.get()
            if not allow_turn(f'{rec["owner"]}/{device}'):
                await send({"type": "assistant", "text": "Hourly chat limit reached on this device; try again later."})
                await send({"type": "done"})
                continue
            try:
                await session.ask(prompt)
            except Exception as e:
                await send({"type": "error", "text": str(e)})
                await send({"type": "done"})

    task = asyncio.create_task(worker())
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "user" and msg.get("text", "").strip():
                await inbox.put(msg["text"][:4000])
            elif msg.get("type") == "state":
                session.state.clear()
                session.state.update(msg.get("state", {}))
    except WebSocketDisconnect:
        pass
    finally:
        refresh.listeners.discard(ws)
        task.cancel()
        await session.close()
