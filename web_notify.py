"""Tell the public web viewer (GitHub repository tehoro/weather-explorer-web) that new highlights
are ready, so it syncs straight away instead of waiting for GitHub's schedule, which can run
hours late.

Sends a `repository_dispatch` event; the viewer's workflow then copies the new scenes. Does
nothing unless WEB_DISPATCH_TOKEN is set: a fine-grained GitHub token for that one repository
with "Contents: read and write" permission. Failures are logged and otherwise ignored, since the
viewer's own schedule is the fallback.

Environment:
  WEB_DISPATCH_TOKEN   the token (a deployment secret)
  WEB_DISPATCH_REPO    default tehoro/weather-explorer-web
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.request

REPO = os.environ.get("WEB_DISPATCH_REPO", "tehoro/weather-explorer-web")


def _send(reason: str) -> str:
    token = os.environ.get("WEB_DISPATCH_TOKEN", "").strip()
    if not token:
        return "skipped (no WEB_DISPATCH_TOKEN)"
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/dispatches",
        data=json.dumps({"event_type": "highlights-updated", "client_payload": {"reason": reason}}).encode(),
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "weather-explorer-backend"},
        method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return f"sent ({r.status})"


async def notify(reason: str) -> None:
    """Ask the web viewer to sync; never raises."""
    try:
        result = await asyncio.to_thread(_send, reason)
    except Exception as e:
        result = f"failed: {e}"
    print(f"[web] {reason}: {result}", flush=True)
