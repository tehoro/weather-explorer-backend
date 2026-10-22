#!/bin/zsh
# Run the weather backend on this Mac, reachable from phones on the same Wi-Fi.
# Needs ANTHROPIC_API_KEY in the environment and ~/.ecmwfapirc for EFI.
# HIGHLIGHTS_EVERY_HOURS=12 ./run.sh   rebuilds the feed automatically (about $1.50 per sweep).
cd "$(dirname "$0")"
echo "Backend on http://$(scutil --get LocalHostName).local:8000  (LAN IP $(ipconfig getifaddr en0 2>/dev/null || echo '?'))"
exec .venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000
