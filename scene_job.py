"""Build scenes in a low-priority child process, separate from the web server."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys


async def build(feed: Path, tag: str | None = None) -> None:
    args = [sys.executable, str(Path(__file__).resolve()), str(feed.resolve())]
    if tag:
        args.append(tag)
    process = await asyncio.create_subprocess_exec(*args)
    try:
        code = await process.wait()
    except asyncio.CancelledError:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        raise
    if code:
        raise RuntimeError(f"Scene worker exited with status {code}")


def main():
    # Set priority before importing numerical libraries or creating their threads.
    os.nice(15)
    import json
    import scenes
    feed_path = Path(sys.argv[1])
    if len(sys.argv) == 2:
        scenes.build(feed_path)
    else:
        feed = json.loads(feed_path.read_text())
        scenes.build_items(feed["items"], sys.argv[2])
        tmp = feed_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(feed, ensure_ascii=False, indent=1))
        tmp.replace(feed_path)


if __name__ == "__main__":
    main()
