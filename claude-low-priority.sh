#!/bin/sh
# Runs Claude Code (and everything it starts: scans, scripts) at low CPU priority, so the web
# server keeps answering while a sweep or requested highlight is being researched.
exec nice -n 15 "$CLAUDE_REAL_CLI" "$@"
