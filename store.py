"""Keeps the few files that must survive a restart: the highlights feed, the time of the last sweep
and which explainers recent sweeps showed.

On Replit (where REPL_ID is set) a deployment's disk is reset on every restart, so these files are
mirrored to Replit Object Storage: restored at start-up, saved after each sweep. Elsewhere the
local disk is already persistent and this does nothing. If there is no feed anywhere, the copy in
seed/ is used so a fresh deployment has something to show before its first sweep finishes.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

BACKEND = Path(__file__).parent
FEED = BACKEND / "data" / "highlights" / "latest.json"
LAST_SWEEP = FEED.parent / ".last_sweep"
SEED = BACKEND / "seed" / "latest.json"
KEYS = {"highlights/latest.json": FEED, "highlights/last_sweep": LAST_SWEEP,
        "highlights/explainer_history.json": FEED.parent / "explainer_history.json"}

ON_REPLIT = bool(os.environ.get("REPL_ID"))


def _client():
    if not ON_REPLIT:
        return None
    try:
        from replit.object_storage import Client
        return Client()
    except Exception as e:  # no bucket attached to the Repl, or the package is missing
        print(f"[store] Replit Object Storage unavailable: {e}", file=sys.stderr, flush=True)
        return None


def restore() -> None:
    FEED.parent.mkdir(parents=True, exist_ok=True)
    c = _client()
    if c:
        for key, path in KEYS.items():
            # Storage wins over any local copy: a deployment can include stale files from the workspace.
            try:
                if c.exists(key):
                    path.write_text(c.download_as_text(key))
                    print(f"[store] restored {key}", flush=True)
            except Exception as e:
                print(f"[store] could not restore {key}: {e}", file=sys.stderr, flush=True)
    if not FEED.exists() and SEED.exists():
        shutil.copy(SEED, FEED)
        print("[store] no saved feed: using the seed copy", flush=True)


def save() -> None:
    c = _client()
    if not c:
        return
    for key, path in KEYS.items():
        if path.exists():
            try:
                c.upload_from_text(key, path.read_text())
            except Exception as e:
                print(f"[store] could not save {key}: {e}", file=sys.stderr, flush=True)


def save_scenes(folder: Path) -> None:
    """Copy a feed's scene files to storage (they are rebuilt only once per sweep)."""
    c = _client()
    if not c:
        return
    for p in folder.glob("*.json.gz"):
        try:
            c.upload_from_bytes(f"scenes/{folder.name}/{p.name}", p.read_bytes())
        except Exception as e:
            print(f"[store] could not save scene {p.name}: {e}", file=sys.stderr, flush=True)


def restore_scene(rel: str, path: Path) -> bool:
    """Fetch one scene file back from storage after a restart. True if it is now on disk."""
    c = _client()
    if not c:
        return False
    try:
        data = c.download_as_bytes(f"scenes/{rel}")
    except Exception:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return True


REQUESTS = FEED.parent.parent / "requests"


def save_requests() -> None:
    """Save the list of live requested highlights."""
    c = _client()
    p = REQUESTS / "active.json"
    if c and p.exists():
        try:
            c.upload_from_text("requests/active.json", p.read_text())
        except Exception as e:
            print(f"[store] could not save requests: {e}", file=sys.stderr, flush=True)


def save_request_file(path: Path) -> None:
    c = _client()
    if c:
        try:
            c.upload_from_text(f"requests/{path.name}", path.read_text())
        except Exception as e:
            print(f"[store] could not save {path.name}: {e}", file=sys.stderr, flush=True)


def restore_requests() -> None:
    """Bring back the live requested highlights after a restart."""
    import json
    c = _client()
    if not c:
        return
    try:
        if not c.exists("requests/active.json"):
            return
        REQUESTS.mkdir(parents=True, exist_ok=True)
        text = c.download_as_text("requests/active.json")
        (REQUESTS / "active.json").write_text(text)
        for e in json.loads(text):
            if e.get("status") == "ready" and c.exists(f"requests/{e['id']}.json"):
                (REQUESTS / f"{e['id']}.json").write_text(c.download_as_text(f"requests/{e['id']}.json"))
        print("[store] restored requested highlights", flush=True)
    except Exception as e:
        print(f"[store] could not restore requests: {e}", file=sys.stderr, flush=True)
