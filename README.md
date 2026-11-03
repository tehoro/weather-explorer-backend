# Weather Explorer: backend

The server half of **Weather Explorer**, a prototype for exploring animated ECMWF weather
forecasts. Twice a day an AI research agent picks about fifteen interesting weather situations
around the world ("highlights"), backs each one with evidence, and describes how to show it. This
backend runs that research, prepares the data for each animation, and serves the result as a JSON
feed.

- Public web viewer (plays the feed in any browser): https://tehoro.github.io/weather-explorer-web/
- Web viewer source: https://github.com/tehoro/weather-explorer-web
- An iPhone/iPad/Mac app (SwiftUI, private TestFlight only) reads the same feed.

> **Please read [Important caveats](#important-caveats) before using anything here.** This is a
> prototype. It is not an ECMWF service, its weather content is model guidance chosen and written
> by an AI model, and its code was written by an AI model.

---

## Contents

- [Who made this, and how](#who-made-this-and-how)
- [Important caveats](#important-caveats)
- [How it works](#how-it-works)
- [A reading guide to the code](#a-reading-guide-to-the-code) (walkthrough, glossary, design decisions)
- [Running it yourself](#running-it-yourself)
- [Configuration](#configuration)
- [Endpoints and feed format](#endpoints-and-feed-format)
- [Tests](#tests)
- [Data sources and acknowledgements](#data-sources-and-acknowledgements)
- [Licence](#licence)

---

## Who made this, and how

All of the code and documentation in this repository was written by **Claude Opus 5.5**, an AI
model made by [Anthropic](https://www.anthropic.com), working in
[Claude Code](https://claude.com/claude-code) at the direction of **Neil Gordon**. Neil set the
goals, the design rules and the priorities, tested the results and decided what to keep. Claude
wrote the code, ran the tests and wrote the commit messages.

Neil developed Weather Explorer as an **ECMWF Visiting Scientist**. ECMWF supported
the work by providing the Claude Opus 5.5 usage, through its enterprise account, with which the
code was written. That support does not make it an ECMWF product or service (see below). Commits it made end with a
`Co-Authored-By: Claude` line.

The rules Neil set are worth knowing because they explain much of the design:

- **ECMWF Open Data only.** Nothing restricted (no MARS, no Extreme Forecast Index), so anyone can
  reproduce every field shown.
- **The server only researches and serves highlights.** It does no work per viewer. Decoding,
  colouring, contouring and animation all happen in the browser or on the phone. (An earlier
  version did per-request work on a 0.5 vCPU server and froze.)
- **Honest sourcing.** Model output is guidance, not a warning. Official warnings belong to
  national weather services, and the feed cites them.
- **Low running cost.** It runs on a small Replit virtual machine, and each research sweep costs
  about US$1.35–1.70 in AI model usage.

## Important caveats

**Not an official forecast or warning.** Everything shown is model guidance from ECMWF's
forecasts, chosen and described by an AI model. It can be wrong, out of date or misleading. For
forecasts and warnings, use your national weather service. Nobody should rely on this for safety,
travel, operations or any decision.

**Not an ECMWF service.** It uses ECMWF's free Open Data and was developed with ECMWF's support
(see above), but it is not an official or operational ECMWF product or service. ECMWF does not
run it, maintain it or vouch for its content.

**AI-chosen and AI-written content.** The highlights, their summaries, captions and citations are
written by an AI model (currently GPT-6.1 Sol from OpenAI; Claude Opus 5.5 is the alternative).
The pipeline checks the feed's structure, and the instructions require evidence with real links,
but no person reviews each highlight before it is published. Expect occasional errors in names,
places, numbers or timing.

**AI-written code.** The code was written by an AI model and tested against the cases described
in this README, not audited line by line. It is a working prototype, not production software. It
may contain bugs, security weaknesses or poor practice. There is **no warranty** (see the
[licence](LICENSE)) and no support.

**Running it costs money.** A research sweep calls a paid AI API (Anthropic or OpenAI) with web
search. If you run your own copy, you pay for your own usage, and you are responsible for keeping
your own keys private.

**Third-party data and services.** The software downloads data from ECMWF, MeteoAlarm, GDACS, the
WMO and national weather services. Each has its own licence and terms (see
[Data sources](#data-sources-and-acknowledgements)). Respect them, keep the attribution, and
don't hammer their servers: the code fetches only what it needs, and so should any changes.

## How it works

```
                   ┌──────────────────────── backend (this repository, on Replit) ───────────────────────┐
 ECMWF Open Data ─▶│ scan_fields.py   objective scan for unusual weather (rain, gusts, lows, local winds)  │
 warning feeds ───▶│ fetch_warnings.py  MeteoAlarm, GDACS, WMO and national CAP warnings                   │
 web search ──────▶│ AI agent (agent.py / openai_agent.py) runs the weather-highlights skill:              │
                   │   weighs the evidence, writes ~15 highlights with 1–4 scenes each                     │
                   │ publish_highlights.py  validates the feed (schema, links, times) before publishing   │
                   │ scenes.py        cuts Open Data to each scene's box and time window (gzipped JSON)    │
                   │ server.py        serves the feed and scene files; schedules sweeps at 03 and 15 UTC   │
                   └───────────────┬──────────────────────────────────────────────┬────────────────────────┘
                                   │ web_notify.py: "new highlights"              │ /api/highlights, /scenes
                                   ▼                                              ▼
                   web viewer repository (GitHub Actions → GitHub Pages)      iPhone app
```

1. **Sweep.** At 03 and 15 UTC, just after each new forecast run is complete on Open Data, the
   server starts a sweep. The AI agent follows a written procedure, the *skill* in
   [`.claude/skills/weather-highlights/SKILL.md`](.claude/skills/weather-highlights/SKILL.md):
   - run an objective scan of the latest forecast fields
     ([`scan_fields.py`](.claude/skills/weather-highlights/scripts/scan_fields.py)),
   - collect official warnings
     ([`fetch_warnings.py`](.claude/skills/weather-highlights/scripts/fetch_warnings.py)),
   - search the news to confirm what is happening and name the systems,
   - choose about fifteen highlights and write one to four *scenes* for each (a map layer, a box,
     a time window and captions), following
     [`feed-schema.md`](.claude/skills/weather-highlights/references/feed-schema.md),
   - publish through the validator
     ([`publish_highlights.py`](.claude/skills/weather-highlights/scripts/publish_highlights.py)),
     which refuses a feed with errors and lists them so the agent can fix them.
2. **Scenes.** [`scenes.py`](scenes.py) then fetches the Open Data fields for each scene, in a
   separate low-priority process ([`scene_job.py`](scene_job.py)) so the web server stays
   responsive.
3. **Serving.** [`server.py`](server.py) hands out the feed and the scene files. Each request is a
   file read. [`store.py`](store.py) keeps the feed in Replit Object Storage so it survives
   restarts. A failed sweep keeps the previous feed.
4. **Notifying.** [`web_notify.py`](web_notify.py) tells the web viewer's repository to sync, and
   its GitHub Action converts the scenes into small image files for browsers.
5. **Requested events.** Anyone with the app can ask for a situation the feed doesn't cover.
   [`requests_store.py`](requests_store.py) queues it, the same agent researches it, and the result
   is listed for 48 hours. Requests are anonymous and rate-limited.

**Two AI providers.** The sweep can run on Claude (Anthropic's
[Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python), [`agent.py`](agent.py))
or on GPT-6.1 Sol (OpenAI's Agents API, [`openai_agent.py`](openai_agent.py)). The OpenAI route
runs the skill in a hosted sandbox that receives only the files the skill needs (no keys). The
backend then re-checks the result with the original validator in a throwaway copy before
publishing it. The [`shadow/`](shadow/) folder holds the trial used to compare the two models
side by side before switching.

## A reading guide to the code

Start with these three short documents:

- [**One highlight, end to end**](docs/walkthrough.md): a real highlight (rain on New Zealand's
  Southern Alps) followed from the forecast files to the pixels on screen.
- [**Glossary**](docs/glossary.md): forecast, weather and software terms in plain words.
- [**Design decisions**](docs/design-decisions.md): why it is built this way, and what was tried
  and dropped.

The web viewer has its own [code map](https://github.com/tehoro/weather-explorer-web/blob/main/docs/code-map.md).

Then read the code in roughly this order:

| Read | To see |
|---|---|
| [`SKILL.md`](.claude/skills/weather-highlights/SKILL.md) | How an AI agent is given a procedure in plain English: evidence first, honest sourcing, explicit rules. |
| [`feed-schema.md`](.claude/skills/weather-highlights/references/feed-schema.md) | The contract between the agent, the server and the viewers. |
| [`publish_highlights.py`](.claude/skills/weather-highlights/scripts/publish_highlights.py) | Checking AI output with ordinary code before anyone sees it: schema, times, layers, and relabelling citations that aren't from an official source. |
| [`fields.py`](fields.py) | Fetching only the needed parts of large GRIB2 files with `.index` files and HTTP Range requests; regridding; the layer catalogue (colours, units, contour intervals). |
| [`scan_fields.py`](.claude/skills/weather-highlights/scripts/scan_fields.py) | Simple objective detection of heavy rain, strong gusts, deep lows, local winds, convection, freezing rain and polar vortex breakdowns (10 hPa winds at 60°). |
| [`fetch_warnings.py`](.claude/skills/weather-highlights/scripts/fetch_warnings.py) | Reading official warnings: MeteoAlarm's API, GDACS, and CAP (Common Alerting Protocol) feeds. |
| [`server.py`](server.py) | A small FastAPI server with a scheduler, a shared lock so jobs never overlap, and catch-up after restarts. |
| [`openai_agent.py`](openai_agent.py) | Running an agent in an isolated sandbox, then validating its output again outside the sandbox. |
| [`tests/`](tests/) | Tests that use fakes, so they make no paid calls. |

Some ideas for projects: add a map layer (it must exist in the Open Data `.index` files); add a
national CAP warning feed to `fetch_warnings.py`; improve the low tracking in `scan_fields.py`;
compare the highlights two AI models choose for the same forecast.

## Running it yourself

You need Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --frozen
APP_KEY=choose-a-long-random-string uv run --frozen uvicorn server:app --port 8000
```

```bash
curl -H "Authorization: Bearer choose-a-long-random-string" http://localhost:8000/api/highlights
```

With no AI key, the server serves the sample feed in [`seed/latest.json`](seed/latest.json) and
runs no sweeps. Sweeps are off by default outside Replit (`SWEEP_HOURS_UTC` empty). To run one, set
`ANTHROPIC_API_KEY` (or `WEATHER_AGENT_PROVIDER=openai` and `OPENAI_API_KEY`) and `SWEEP_HOURS_UTC`.
A sweep takes 10–30 minutes and costs real money.

The scripts can also be run on their own, which costs nothing (they only read public data):

```bash
uv run --frozen python .claude/skills/weather-highlights/scripts/scan_fields.py --out scan.json
python3 .claude/skills/weather-highlights/scripts/fetch_warnings.py --worldwide --hours-ahead 48 --out warnings.json
```

The scan downloads a few hundred global fields (several minutes, cached afterwards).

**Deployment.** The live copy runs on a Replit Reserved VM (0.5 vCPU, 2 GiB; [`.replit`](.replit)).
A [`Dockerfile`](Dockerfile) is included for other hosts; mount `/app/data` on persistent storage.

## Configuration

Set as environment variables (secrets as host secrets, never in the code):

| Variable | Purpose |
|---|---|
| `APP_KEY` | The shared bearer key clients send. Choose your own. |
| `ANTHROPIC_API_KEY` | Claude, for sweeps and requests on the Claude route. |
| `OPENAI_API_KEY` | OpenAI, for the OpenAI route. |
| `WEATHER_AGENT_PROVIDER` | `claude` (default) or `openai`. |
| `WEATHER_REFRESH_MODEL` | Claude model for the sweep (see `agent.py`). |
| `WEATHER_OPENAI_MODEL`, `WEATHER_OPENAI_EFFORT`, `WEATHER_OPENAI_MINUTES`, `WEATHER_OPENAI_RUNS_PER_DAY` | OpenAI model, effort, time limit and daily cap. |
| `SWEEP_HOURS_UTC` | Sweep hours, e.g. `3,15`. Empty disables sweeps. |
| `SWEEP_MIN_HOURS` | Minimum gap between sweeps (default 6). |
| `REQUEST_HOURS`, `REQUESTS_PER_DEVICE_PER_DAY`, `REQUESTS_PER_DAY` | Requested events: listing time (48 h) and limits (3 per device, 12 in all, per day). |
| `WEB_DISPATCH_TOKEN`, `WEB_DISPATCH_REPO` | Optional: notify a web viewer repository after each sweep. |
| `WEATHER_CACHE` | Where downloaded Open Data is cached. |

## Endpoints and feed format

All endpoints except health need `Authorization: Bearer <APP_KEY>`.

| Method and path | Returns |
|---|---|
| `GET /api/health` | `{"ok": true}` (no key) |
| `GET /api/highlights` | The highlights feed |
| `GET /api/highlights/status` | Whether a sweep is running, and the next one |
| `GET /api/requests` | Requested events from the last 48 hours |
| `POST /api/requests` | `{"text": "..."}` asks for an event; `X-Device-ID` for the per-device limit |
| `GET /scenes/{tag}/{file}` | A scene's field data (gzipped JSON) |

Each feed item has a title, hazards, place and time window, a summary, why it is interesting,
evidence (each with its kind and, for warnings, news and official sources, a link), and one to
four scenes. The full schema, with examples, is in
[`feed-schema.md`](.claude/skills/weather-highlights/references/feed-schema.md).

Map layers (`fields.py`): mean sea level pressure, 10 m wind, gusts, 2 m temperature,
precipitation, precipitation type, total column water vapour, 250 hPa jet, 500 hPa height,
850 hPa wind and temperature, CAPE, simulated cloud tops, and IFS ENS probabilities of heavy rain
and strong gusts.

## Tests

```bash
uv sync --frozen
uv run --frozen python -m unittest discover -s tests -v
```

The tests use fake research and scene inputs, so they make no paid calls.

## Data sources and acknowledgements

**Weather data**
- **ECMWF Open Data**: IFS HRES and IFS ENS forecasts, © ECMWF, licensed under
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Read from the
  [AWS mirror](https://registry.opendata.aws/ecmwf-forecasts/) and
  [data.ecmwf.int](https://www.ecmwf.int/en/forecasts/datasets/open-data). Fields are regridded and
  unit-converted. Required attribution: *"Data: © ECMWF, CC BY 4.0. Regridded and unit-converted."*

**Warnings** (read at each sweep, cited with links; each provider's terms apply)
- [MeteoAlarm](https://meteoalarm.org), warnings from Europe's national weather services.
- [GDACS](https://www.gdacs.org), the Global Disaster Alert and Coordination System (UN and
  European Commission).
- [WMO Severe Weather Information Centre](https://severeweather.wmo.int), CAP warnings from
  national services, and the WMO member directory used for `references/agencies.json`.
- [MetService](https://www.metservice.com) (New Zealand) public CAP alerts, licensed under
  CC BY 4.0.
- Each national weather service, as the author of its own warnings.

**AI models and tools**
- [Claude](https://www.anthropic.com/claude) (Anthropic): wrote the code; optional sweep model.
- GPT-6.1 Sol ([OpenAI](https://openai.com)): current sweep model.

**Software** (each under its own licence): [FastAPI](https://fastapi.tiangolo.com),
[Uvicorn](https://www.uvicorn.org), [NumPy](https://numpy.org),
[earthkit-data](https://github.com/ecmwf/earthkit-data) and [ecCodes](https://github.com/ecmwf/eccodes)
(ECMWF, Apache 2.0), [truststore](https://github.com/sethmlarson/truststore),
[replit-object-storage](https://pypi.org/project/replit-object-storage/),
[claude-agent-sdk](https://github.com/anthropics/claude-agent-sdk-python) and the
[OpenAI Python SDK](https://github.com/openai/openai-python). The full list, with versions, is in
[`uv.lock`](uv.lock).

**ECMWF** supported the development, as part of Neil Gordon's role as an ECMWF Visiting Scientist,
by providing Claude Opus 5.5 usage through its enterprise account.

**People.** Neil Gordon directed the project. Andreas Müller's Vision Pro weather display is the
intended future partner for the app (not built yet). Thanks to the early testers.

## Licence

The code is released under the [MIT Licence](LICENSE), © 2026 Neil Gordon. You may use, copy,
change and share it, including commercially, as long as you keep the copyright and licence
notice. It comes with no warranty.

The licence covers the code only. Data, warnings and other material the software downloads or
displays keep their own licences, listed above. Because the code was written by an AI model, the
extent of copyright in it may be limited in some countries; the licence grants what rights exist.
