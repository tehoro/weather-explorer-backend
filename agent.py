"""The highlights sweep: Claude runs the weather-highlights skill (shell, web, ECMWF Open Data) with a
fixed prompt and publishes data/highlights/latest.json. This is the only thing the server uses
Claude for; the app's guide runs on the device.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, ToolUseBlock, query

BACKEND = Path(__file__).parent
FEED = BACKEND / "data" / "highlights" / "latest.json"   # same file as store.FEED
REFRESH_MODEL = os.environ.get("WEATHER_REFRESH_MODEL", "claude-opus-5-5")


REFRESH_PROMPT = ("Run the weather-highlights skill now and publish a fresh highlights feed. "
                  "Work autonomously: there is no user to answer questions. Finish with the short report.")


async def run_refresh(log: Callable[[str], None]) -> str:
    """Run the weather-highlights skill headless. Returns the agent's final report."""
    tools = ["Skill", "Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebSearch", "WebFetch"]
    options = ClaudeAgentOptions(
        model=REFRESH_MODEL,
        cwd=str(BACKEND),
        tools=tools,
        allowed_tools=tools,
        permission_mode="bypassPermissions",
        setting_sources=["project"],      # loads .claude/skills/weather-highlights
        max_turns=80,
    )
    report = ""
    async for msg in query(prompt=REFRESH_PROMPT, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    log(f"tool {block.name}: {json.dumps(block.input)[:160]}")
                elif isinstance(block, TextBlock):
                    report = block.text
        elif isinstance(msg, ResultMessage):
            log(f"refresh finished: error={msg.is_error} turns={msg.num_turns} cost=${msg.total_cost_usd or 0:.2f}")
    return report
