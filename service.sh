#!/bin/zsh
# Started by launchd at login (~/Library/LaunchAgents/me.neilgordon.weatherexplorer.backend.plist).
# Takes the Anthropic key from ~/.zshrc, so swapping the key there is still the only step needed.
eval "$(grep -E '^export ANTHROPIC_API_KEY=' ~/.zshrc)"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin"   # gh for the address gist
cd "$(dirname "$0")"
exec .venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000
