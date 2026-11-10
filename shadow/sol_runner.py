"""Shadow sweeps: the weather-highlights skill on GPT-6.1 Sol (OpenAI Agents API), for comparison
with production. Nothing here publishes anything or touches the production server: it reads the
public web feed, runs the skill in a hosted OpenAI sandbox with the same engine production uses
(../openai_agent.py), downloads the result and re-validates it in a throwaway copy of the backend.

  uv run sol_runner.py probe      check the key and the model (no session is created, no cost)
  uv run sol_runner.py sweep      start a shadow sweep, or resume the unfinished one
                                  (--once-within 3: not if this slot already has a run)
  uv run sol_runner.py cleanup    delete hosted sessions that earlier runs left behind

Each run keeps everything in runs/<UTC stamp>/ (not in Git); see openai_agent.py for the files.
inputs/ holds production's feed and explainer history, rebuilt from the public web archive.

Environment: OPENAI_API_KEY, or the file given by --key-file (never printed or uploaded).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

try:  # the operating system's certificate store (works behind TLS-inspecting proxies)
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
sys.path.insert(0, str(BACKEND))
import openai_agent as engine  # noqa: E402

RUNS = HERE / "runs"
SKILL = engine.SKILL
WEB = "https://tehoro.github.io/weather-explorer-web/data"
KEY_FILE = Path.home() / ".config" / "weather-skill-trial" / "openai.key"
KEEP_SWEEPS = 8   # explainer history length, as explainers.KEEP_SWEEPS


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%H:%M:%S} {msg}", flush=True)


def client(key_file: Path):
    key = key_file.read_text().strip() if key_file.exists() else None
    try:
        return engine.client(key)
    except RuntimeError:
        sys.exit(f"No OpenAI key: set OPENAI_API_KEY or put it in {key_file} (mode 600)")


def get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "weather-highlights-shadow/0.1"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def production_inputs(folder: Path) -> tuple[dict, list[dict]]:
    """Production's current feed and its last KEEP_SWEEPS sweeps' explainers (explainers._sweep format)."""
    sys.path.insert(0, str(BACKEND / SKILL / "scripts"))
    import explainers
    feed = get_json(f"{WEB}/feed.json")
    archive = get_json(f"{WEB}/archive.json")
    tags = sorted({ref.split("/")[0] for ref in archive if not ref.startswith("req")})[-KEEP_SWEEPS:]
    refs = [ref for ref in archive if ref.split("/")[0] in tags]
    with ThreadPoolExecutor(8) as ex:
        items = list(ex.map(lambda ref: get_json(f"{WEB}/items/{ref}.json"), refs))
    history = []
    for tag in tags:
        sweep = {"generated_utc": f"{tag[:4]}-{tag[4:6]}-{tag[6:8]}T{tag[9:11]}:{tag[11:13]}Z",
                 "items": [it for ref, it in zip(refs, items) if ref.startswith(tag + "/")]}
        history.append(explainers._sweep(sweep))
    folder.mkdir(parents=True, exist_ok=True)
    engine.save(folder / "latest.json", feed)
    engine.save(folder / "explainer_history.json", history)
    return feed, history


def recent_run(hours: float) -> Path | None:
    """A run started within the last `hours` (run folders are named by their UTC start)."""
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    for run in sorted(RUNS.glob("*"), reverse=True):
        try:
            started = datetime.strptime(run.name, "%Y%m%dT%H%MZ").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        if started >= cutoff and (run / "session.json").exists():
            return run
    return None


def probe(a) -> None:
    c = client(a.key_file)
    m = c.models.retrieve(a.model)
    print(f"key works; model {m.id} is available to this project")
    print("spend_control: not probed (an accepted request would start a paid session); runs are capped by time instead")


def sweep(a) -> int:
    c = client(a.key_file)
    RUNS.mkdir(exist_ok=True)
    run = engine.unfinished(RUNS)
    if not run and a.once_within and recent_run(a.once_within):
        log(f"this slot already has a run ({recent_run(a.once_within).name}); nothing to do")
        return 0
    if run:
        log(f"resuming unfinished run {run.name}")
    else:
        run = RUNS / datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")
        run.mkdir()
        log(f"run {run.name}: reading production's feed and explainer history")
        feed, _ = production_inputs(run / "inputs")
        log(f"production feed {feed.get('generated_utc')}")
        engine.start(c, run, a.model, a.effort, "weather-highlights-shadow", log)
    python = str(BACKEND / ".venv" / "bin" / "python")
    result = engine.finish(c, run, a.minutes, python, log, keep_session=a.keep_session)
    return 0 if result["success"] else 1


def cleanup(a) -> None:
    c = client(a.key_file)
    for run in sorted(RUNS.glob("*")):
        rp = run / "result.json"
        if not rp.exists():
            continue
        result = json.loads(rp.read_text())
        if result.get("session_deleted") is True or not result.get("session_id"):
            continue
        try:
            c.beta.agents.sessions.delete(result["session_id"])
            result["session_deleted"] = True
            engine.save(rp, result)
            log(f"deleted session of {run.name}")
        except Exception as e:
            log(f"{run.name}: {type(e).__name__}: {e}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["probe", "sweep", "cleanup"])
    ap.add_argument("--model", default="gpt-6.1-sol")
    ap.add_argument("--effort", default="high")
    ap.add_argument("--minutes", type=float, default=45, help="wall-clock limit before the run is cancelled")
    ap.add_argument("--key-file", type=Path, default=KEY_FILE)
    ap.add_argument("--keep-session", action="store_true", help="don't delete the hosted session afterwards")
    ap.add_argument("--once-within", type=float, default=0, metavar="HOURS",
                    help="skip if a run already started within this many hours (scheduled backups)")
    a = ap.parse_args()
    if a.command == "probe":
        probe(a)
    elif a.command == "cleanup":
        cleanup(a)
    else:
        return sweep(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
