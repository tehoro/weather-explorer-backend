# Design decisions

Why Weather Explorer is built the way it is. Each decision was made by Neil Gordon, usually after
trying something and seeing what went wrong. The code makes more sense once you know them. Dates
are when the decision took effect (2026).

## The rules that shape everything

**1. ECMWF Open Data only.** Every field comes from ECMWF's free real-time Open Data, never from
restricted archives (MARS) or products (the Extreme Forecast Index). The audience only has Open
Data, so everything shown can be reproduced by anyone. Consequences in the code:
- `fields.py` checks a parameter exists in the Open Data `.index` files before a layer uses it.
- Ensemble *probabilities* (share of ENS members over a threshold) stand in for the EFI.
- Files are read from the AWS mirror first (fastest) with ECMWF's own server as fallback, and only
  the needed bytes are fetched with HTTP Range requests.

**2. The server only researches and serves highlights.** An early version computed map data for
each request on a 0.5 vCPU server, and it froze under load. Now the server does work once a day
(the sweep and its scenes) and every viewer request is a file read. Everything that grows with
the number of viewers runs on the viewer's own device: decoding, colouring, contours, particles,
the 3D view. Consequences:
- There are no per-request computing endpoints, no chat sessions and no websockets.
- Scene data is built once per sweep and cached forever (file names include the sweep time).
- The web viewer is static files on GitHub Pages; GitHub's network carries the traffic.

**3. Model guidance is not a warning.** Official warnings belong to national weather services.
The feed says what the model shows, cites official warnings where they exist, and says what a
visual is and isn't (clouds "inferred from humidity", heights "exaggerated").

**4. Cheap to run.** A small Replit virtual machine (about US$15 a month), static hosting for the
viewer, and a few US dollars a day in AI usage (one sweep a day since November 2026). Choices are weighed against that.

## The research sweep

**An AI agent with a written procedure, checked by ordinary code.** The agent follows `SKILL.md`,
plain-English instructions with explicit rules (evidence first, dates checked, times in UTC with
the date, no invented storm names). Its output is never trusted directly: `publish_highlights.py`
checks the schema, layers, boxes, time windows and citations, and refuses to publish until the
feed is valid. The agent reads the errors and fixes them.

**Objective evidence before the news.** `scan_fields.py` looks for signals in the forecast fields
(ensemble probabilities of heavy rain or strong gusts, deep lows, 24 h rain over 100 mm, strong
gusts, local winds, convection, freezing rain) before the agent searches the news. The best
highlights have both a field signal and real-world confirmation.

**Official sources first** (3 Nov). The agent used to find warnings mostly through news searches,
and once cited a news site's copy of a MetService press release as "official". Now
`fetch_warnings.py` reads official warning feeds directly (MeteoAlarm, GDACS, MetService's CAP
feed, and the WMO's combined CAP feed from about 45 national services). The validator relabels
any "warning" or "official" citation as "news" unless it links to an agency's own site
(`references/agencies.json`, built from the WMO's directories) or a government domain.

**Sources written for people** (3 Nov). Sources are shown to viewers, so each one links a page a
person can read (never a JSON, XML, CAP or RSS feed: the validator swaps known feeds for their
public page and rejects the rest), and its text is one plain sentence of at most 200 characters,
with no script or file names. The agent keeps its working notes in its own logs.

**Choosing the AI model.** On 25 October 2026 cheaper Claude models were tested blind against
Claude Opus 5.5: they missed or misnamed storms, so Opus stayed. On 1 November GPT-6.1 Sol was run
as a *shadow* alongside production for three sweeps (publishing nothing): 13 of 15 events matched
Opus, with no real problems flagged, at a lower cost. Production switched to Sol. One setting
(`WEATHER_AGENT_PROVIDER`) switches back. There is deliberately no automatic fallback between
providers: a person decides.

**Isolation for the OpenAI route.** The agent runs in a hosted sandbox that receives only the
skill's files and the current feed, never keys or data. The backend downloads the draft, checks it
with the original validator in a throwaway copy of itself, and only then publishes it for real.

**A failed sweep changes nothing.** If a sweep fails, the previous feed stays up and the viewer is
not told to sync. Sweeps and requested-event research share a lock, so they never run at once on
the small server.

**Timing.** Sweeps run at 03 and 15 UTC, just after the 18 and 06 UTC runs' 0.1° files are
complete on Open Data (about 02:28 and 14:27 UTC). After a restart, the server only catches up a
slot missed within the last three hours.

## The web viewer

**Static site, data converted once.** A GitHub Action (`sync.py`) converts each scene once into
lossless 8-bit WebP images: each pixel holds a weather value (0–254 between the scene's minimum
and maximum, 255 for missing), not a colour. That is about a third of the size of the backend's
JSON. Precipitation uses a square-root scale so light rain keeps detail.

**Colour in the graphics card.** The browser draws the field with a WebGL2 shader and a colour
look-up table, so changing units or layers never needs new data.

**Equal-area projection** (2 Nov). The map used Web Mercator, which hugely stretches high
latitudes. It now uses a Lambert azimuthal equal-area projection centred on the view, like
turning a globe. The data stay on Web Mercator grids; the shader converts each screen pixel back
to longitude and latitude to look up its value. The backend sizes each scene's data box for this
projection so there are no gaps at the edges.

**Units and times from the time zone** (3 Nov). Browsers report only the language, and many
people outside the US use "English (US)", so units used to come out wrong (°F in New Zealand).
Automatic units now follow the browser's time zone, and times are shown in the viewer's own zone
by default.

**3D only where it helps, and never blocking the map.** Tropical cyclones get a 3D view because
their structure (eye, warm core, inflow and outflow) is three-dimensional. The bundles take 6–15
minutes each to build, so they are built in separate jobs *after* the map is deployed.

**Shareable and offline.** Share links encode exactly what is on screen. Save makes a single HTML
file that plays with no connection, which is why `app.js` and `units.js` are plain scripts rather
than modules (they can be inlined).

## The iPhone app

A SwiftUI app reads the same feed and decodes Open Data itself on the device (including its own
GRIB2 decoder), with an on-device Apple Intelligence guide. It is in private testing only and not
under active development; the web viewer is the priority. Backend changes are kept compatible with
it.

## Considered and not done

- **Automatic weather fronts** on the map: dropped (2 Nov).
- **A 3D terrain view** for rain over mountains: prototyped, then parked. About 2 GB of Open Data
  per highlight at 0.1°, and the first designs were too busy.
- **Smaller 3D bundles** (6-hourly frames, loading on demand), **better particle seeding** towards
  the storm, and **3D for other storms** (deep lows, atmospheric rivers): ideas for later.
