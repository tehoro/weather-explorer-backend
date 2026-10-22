"""Minimal token validation for the proof of concept.

Tokens are random strings handed to each client (Andreas's app, a TestFlight build, a script).
Only their SHA-256 hashes are stored, in data/tokens.json, each with an owner, scopes and an
optional expiry. Scopes: feed (highlights file), data (field frames), agent (chat; costs money).

  uv run python tokens.py new --owner neil-iphone --scopes feed,data,agent [--days 90]
  uv run python tokens.py list
  uv run python tokens.py revoke --owner neil-iphone
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

STORE = Path(__file__).parent / "data" / "tokens.json"
SCOPES = {"feed", "data", "agent"}


def _load() -> dict:
    return json.loads(STORE.read_text()) if STORE.exists() else {}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


_cache: tuple[float, dict] = (0.0, {})


APP_KEY = os.environ.get("APP_KEY", "")   # the key built into the app, as a deployment secret


def validate(token: str | None, scope: str) -> dict | None:
    """The client's record if the token is valid, unexpired and has the scope, else None."""
    global _cache
    if not token:
        return None
    if APP_KEY and secrets.compare_digest(token, APP_KEY):
        return {"owner": "app-shared", "scopes": sorted(SCOPES)}
    if time.time() - _cache[0] > 30:          # re-read the store every 30 s so revocations apply
        _cache = (time.time(), _load())
    rec = _cache[1].get(_hash(token))
    if not rec or scope not in rec["scopes"]:
        return None
    if rec.get("expires") and datetime.fromisoformat(rec["expires"]) < datetime.now(timezone.utc):
        return None
    return rec


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new")
    n.add_argument("--owner", required=True)
    n.add_argument("--scopes", default="feed,data,agent")
    n.add_argument("--days", type=int, default=90)
    sub.add_parser("list")
    r = sub.add_parser("revoke")
    r.add_argument("--owner", required=True)
    a = ap.parse_args()

    store = _load()
    if a.cmd == "new":
        scopes = sorted(set(a.scopes.split(",")))
        assert set(scopes) <= SCOPES, f"scopes must be from {SCOPES}"
        token = "wx_" + secrets.token_urlsafe(24)
        store[_hash(token)] = {"owner": a.owner, "scopes": scopes,
                               "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                               "expires": (datetime.now(timezone.utc) + timedelta(days=a.days)).isoformat(timespec="seconds")}
        STORE.parent.mkdir(parents=True, exist_ok=True)
        STORE.write_text(json.dumps(store, indent=1))
        STORE.chmod(0o600)
        print(token)
    elif a.cmd == "list":
        for rec in store.values():
            print(f"{rec['owner']:24} {','.join(rec['scopes']):18} expires {rec.get('expires', 'never')}")
    elif a.cmd == "revoke":
        keep = {h: r for h, r in store.items() if r["owner"] != a.owner}
        STORE.write_text(json.dumps(keep, indent=1))
        print(f"revoked {len(store) - len(keep)} token(s) for {a.owner}")


if __name__ == "__main__":
    main()
