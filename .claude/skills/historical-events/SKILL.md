---
name: historical-events
description: Prepare one past high-impact weather event (for example a famous windstorm, Cyclone Gabrielle in 2023, Hurricane Katrina in 2005) for the Weather Explorer viewer, shown with the ERA5 reanalysis. Writes a referenced account of what happened and its impacts from official reports and published papers, compares ERA5 with what was observed, and gives scenes (layers, boxes, windows, captions) built from ERA5. Use when asked to research, draft or redo a historical or past weather event for the viewer.
---

# Historical events

One past event, prepared for people exploring weather on a screen, often in front of an audience.
The maps come from **ERA5**, the Copernicus reanalysis (hourly, 0.25°, 1940 onwards): a
reconstruction of the weather from the observations of the time, not a forecast. The text is a
**referenced account of what happened**, as trustworthy as a good encyclopedia entry, and an honest
comparison of what ERA5 shows with what was measured.

No warnings, forecasts or probabilities are needed. Accuracy and honest sourcing come first: a
shorter account where every figure is checked beats a longer one with doubtful numbers.

**The weather leads.** This is a weather explorer, so the emphasis is on the meteorology (how
the system formed and developed, why it was so wet or windy there, what was remarkable, what ERA5
shows of it). The impacts still matter and get a clear, complete summary (deaths, the main kinds
of damage, losses), concise rather than a long catalogue of every road and bridge.

Work from the backend directory (the one holding `era5.py`). `$S` below is
`.claude/skills/historical-events`. Run Python with `uv run --frozen python`.

You are given a **brief** (`data/history/brief.json`): the event's `id`, a working `title`, the
`country`, rough `start` and `end` (UTC), a rough `bbox` [west, south, east, north] and sometimes
`notes` and `places`. Dates and box are a starting point: you may move the window by up to two days
either way and change the box.

## 1. What ERA5 shows (before writing anything)

`data/history/scan.json` is an ERA5 scan of the brief's box and window, made before you started.
If you need a different box, window or more places, run the scan again (it downloads about 5 MB an
hour of window, in parallel; allow 20 minutes):

```bash
uv run --frozen python $S/scripts/scan_era5.py --bbox=W,S,E,N --start 2005-08-28T00:00Z \
    --end 2005-08-30T12:00Z --places "New Orleans:29.95:-90.07;Biloxi:30.40:-88.89" --out data/history/scan.json
```

It gives, hour by hour:
- `track`: the main low (the deepest in the box at the time of lowest pressure), followed hour by
  hour: position and central pressure. The viewer draws this same track.
- `lows`: every low below 1000 hPa in the box.
- `gusts`: the strongest gusts in the hour before (up to five places, at least 200 km apart).
- `rain`: the wettest places over the whole window (totals) and the heaviest hour.
- `places`: pressure, wind, gust and rain at each named place.
- `summary`: lowest central pressure, strongest gust and wettest place overall, with times.

Every statement about what ERA5 shows, in captions, in `reanalysis` and in the `era5` column of the
comparison, must come from the scan, with its place and time. Round sensibly (953 hPa, 40 m/s).

## 2. Research what happened

Use web search, then **open and read** each source you cite. Never cite a page you have not read,
and never give a figure from memory. If you cannot find a figure in a source you read, leave it out
(a gap is better than a guess) and say so in the report.

Sources, in this order of preference (`kind` in the item):
1. `official`: national weather services' event reports and case studies, government inquiries,
   courts of inquiry, official tropical cyclone reports (for example the US National Hurricane
   Center's Tropical Cyclone Reports), emergency management reports.
2. `peer-reviewed`: papers in journals (*Weather*, *QJRMS*, *Monthly Weather Review*, *Weather and
   Forecasting*, and others). An abstract you read counts for what the abstract says.
3. `statistics`: official statistics, insurers' associations and reinsurers' reports, for loss
   figures only.
4. `archive`: national history and archive sites (for example NZ History, Te Ara, national
   libraries and archives).

Wikipedia, news and blogs may help you **find** sources, but are never cited. A newspaper of the
time held by a national archive counts as `archive`. Link each source at its publisher, or at an
official or educational copy (a national weather service, a learned society, a university), not at
ResearchGate, Academia.edu or a file-sharing site.

Rules for figures:
- **Deaths:** say how they are counted (direct or indirect, which country, whose count). If
  official counts differ, give the range and say whose is whose.
- **Money:** give the currency, the year of the prices if the source says, and whether it is insured
  or total loss. If the source does not say, write "in [year] prices not stated" or leave it out.
- **Measurements:** station name, time (with its zone: UTC or GMT as the source gives it), and
  whether the value was measured or estimated.
- **Disagreement:** when sources disagree (a different highest gust, a different death toll), say
  so and give both.
- **Maximums, with care:** the strongest wind or heaviest rain of an event is the highest
  reported anywhere, not the value at the best-known station: search for it (exposed headlands,
  hill sites, ships, signal stations), and never call one station's value the event's maximum
  unless a source says so. Extreme readings are often doubtful (instruments failing, exposure,
  a single gust on a hilltop): give them as reported, say who reported them and any doubt, and
  set them beside the best-established figures. Prefer a careful range ("gusts of about 190-200
  km/h in Wellington, with higher readings reported on exposed coasts") to one precise number.
- **Corrections:** records are sometimes corrected or withdrawn later (a faulty instrument, a
  re-analysis of the data). For every record value you use, search for later corrections. Give
  the corrected value, say what changed and cite the source of the correction; if you find a
  correction only in a secondary source, say so.
- **Times in text** that should change with the reader's time zone are written as "03 UTC 29 Aug"
  or "06-12 UTC 29 Aug". Times quoted from a source stay as the source gives them ("11:10 CDT").

Find, where the sources allow (meteorological reports, case studies and papers first):
- the synoptic setting: the air masses, the jet, what steered and fed the system
- the development: formation, deepening rates, transition (for example tropical to extratropical),
  re-intensification, landfall or crossing, stalling, decay
- the mechanism of the hazard: the moisture supply, uplift over hills, slow movement, a sting jet,
  storm surge with onshore wind
- the lowest central pressure observed or analysed, and where
- the highest winds, gusts and rain measured, with stations, and records or return periods
- the impacts: deaths, the main kinds of damage, losses
- the forecasts and warnings at the time, and what changed afterwards

## 3. Write the item

Write `data/history/draft.json` in this form. All text is plain British English: short sentences,
concrete numbers, no drama, respectful where lives were lost, for presenters and non-specialists.
Use everyday words: say "the recorder reached its limit, so the true peak was higher", not "an
uncensored peak". Name places ("off Brittany", "near Shoreham"), never grid coordinates such as
"48.13°N, 6.06°W"; round ERA5 values (951 hPa, 42 m/s).

```json
{
 "id": "<the brief's id>",
 "kind": "historical",
 "title": "<what people call it, e.g. 'Cyclone Gabrielle' or 'Hurricane Katrina'>",
 "hazards": ["wind", "rain", "flood", "coastal", "tropical-cyclone", "snow", "ice", "convection", "other"],
 "where": {"country": "...", "label": "the area, for a list line", "centre": [lat, lon],
           "bbox": [W, S, E, N], "tz": "<IANA time zone in the middle of the event, e.g. America/Chicago>"},
 "when": {"start": "...Z", "peak": "...Z", "end": "...Z"},
 "summary": "3-4 sentences for a presenter, mostly about the weather: what the system was, where and when, what made it extreme, with the headline measurements (lowest pressure, strongest gust or heaviest rain); then the impacts in a sentence or two (deaths and the scale of damage). Each figure from a source cited below. No measurement caveats here.",
 "impacts": [{"text": "2-4 entries in all, each a concise paragraph: deaths; the main kinds of damage; losses", "sources": ["s1", "s3"]}],
 "history": [{"text": "the forecasts and warnings at the time, and what changed afterwards", "sources": ["s2"]}],
 "why_interesting": [{"text": "The weather, 3-6 entries, the heart of the item: the setting, the development, the mechanism of the hazard, records and return periods, what it taught meteorologists. Not the history of individual measurements: a corrected record gets one sentence at most, and detailed caveats belong in the report", "sources": ["s4"]}],
 "reanalysis": "3-4 sentences: how well ERA5 captures it, from the scan, and why it falls short where it does.",
 "era5_vs_observed": [{"quantity": "Lowest central pressure", "era5": "<value, time, place from the scan>",
                       "observed": "<value, time, place from a source>", "sources": ["s4"]}],
 "sources": [{"id": "s1", "title": "...", "publisher": "...", "year": 2006, "url": "https://...",
              "archive_url": "https://web.archive.org/...", "kind": "official"}],
 "scenes": []
}
```

- `where.tz` is the IANA zone people there used (the viewer shows times in it, with the clock
  changes of that year). For an event over the sea, the nearest affected country's zone.
- `when.peak` is the hour of the worst impacts, within the scan window. The track the viewer
  draws starts from the deepest low inside `where.bbox` at that hour, so make sure the storm you are
  writing about is the deepest low in that box then (check `lows` in the scan).
- **Every** entry in `impacts`, `history`, `why_interesting` and `era5_vs_observed` cites at least
  one source, and every figure in its text is in one of those sources. Impacts cite at least one
  `official`, `peer-reviewed` or `archive` source (`statistics` alone is enough only for losses).
- `era5_vs_observed`: 3-7 rows, each comparing like with like: the same quantity at the same
  place (a named station, using the scan's `places`), or the storm's lowest central pressure in
  both. Never set ERA5's highest value anywhere against a measurement somewhere else, and never
  repeat a row. Include the central pressure, the stations with the strongest measured gusts (or
  rain), and one or two places people know. The `era5` cell comes from the scan; the
  `observed` cell from a source. Each cell is short, under about 100 characters: the value, the
  time and the place ("98 kn at 03:10 GMT; probably 100 kn later"). Caveats and explanations go in
  `reanalysis`, not in the table.
- The forecast story (`history`) includes what forecasters and broadcasters said at the time,
  where a source you read gives it, and what changed afterwards (new warning services,
  observations, models), each in its own entry. Take it from the weather service, official reviews
  and inquiries; a news report or a broadcasting complaint decision is not a source for what the
  forecasters said.
- `archive_url`: a Wayback Machine copy (search `https://web.archive.org/web/*/<url>`) for any
  page that is old or likely to move. Leave it out if none exists; never invent one.
- Do not mention this process, the scan, files or tools in any text people read.

### Scenes

Three scenes that tell the story (two only when a third would repeat one of them), each built
from ERA5 for its own box and window:

| Event | Scene 1 | Scene 2 | Scene 3 |
|---|---|---|---|
| Windstorm (extratropical) | `mslp` fill + `mslp` contours + `wind10` vectors: the low from formation to decay, ocean-basin box | `gust` fill + `mslp` contours + `wind10` vectors: the worst hours, 10-20° box on the land hit | `olr` fill + `mslp` contours: the cloud head and dry slot |
| Tropical cyclone | `tcwv` or `olr` fill + `mslp` contours: the track across the basin | `wind10` fill + `mslp` contours + `wind10` vectors: landfall, 10-15° box | `raintotal` fill + `mslp` contours: the rain total building up (when rain or floods matter) |
| Rain and floods (including ex-tropical cyclones) | `tcwv` fill + `mslp` contours + `wind850` vectors: the moisture plume | `raintotal` fill + `mslp` contours: the event total building up over 2-4 days, on the flooded areas | `precip` fill: where and when it rained hardest |
| Snow, cold | `t850` fill + `mslp` contours | `ptype` fill + `mslp` contours | `wind10` fill + vectors |

Each scene:

```json
{"title": "Landfall on the Gulf coast", "fill": "wind10", "contours": "mslp", "vectors": "wind10",
 "bbox": [W, S, E, N], "start": "...Z", "end": "...Z",
 "focus": [{"lat": 29.95, "lon": -90.07, "label": "New Orleans"}],
 "captions": [{"at": "...Z", "text": "..."}]}
```

- Layers: `mslp`, `wind10`, `gust`, `t2m`, `precip`, `rain24`, `raintotal`, `tcwv`, `jet250`, `z500`,
  `wind850`, `t850`, `cape`, `olr`, `ptype`. Vectors: `wind10`, `wind850`, `jet250`. Contours: `mslp`, `z500`.
- **Rain.** When rain or flooding is the story, one scene shows `raintotal`: the rain since the
  scene began, building up frame by frame. Start it just before the rain sets in and end it after
  the rain stops (up to 4 days), so the last frame is the event total; compare that with the
  measured totals in `era5_vs_observed` (the scan's `rain.totals` and `places` give ERA5's).
  `precip` is a rate (mm/h) and `rain24` a 24 h total: use them for timing, not for the total.
- Windows: at most 4 days; frames are hourly, and the viewer plays 3 hours of weather a second at
  1× (6 at its usual 2×), so a 2-day scene lasts 8 seconds. Start a few hours before the action.
- Boxes hold the whole feature through the window, plus a coast or place people know. A scene
  box reaching past 75° latitude is drawn on a polar grid; the track is not drawn there.
- `focus`: up to 5 places named in the text. Do not add a point for the storm centre: the viewer
  draws the track, a moving marker and the central pressure itself.
- Titles in everyday words: "Cloud tops", "The strongest gusts", not "infrared cloud radiation".
- Captions: 2-4 per scene, at least 6 hours apart, each under about 70 characters, saying what is
  happening at that moment with places and values from the scan ("Gusts near 45 m/s on the
  Mississippi coast"). Describe what the scene's own map shows at its own scale: on a pressure map
  the low deepening and the isobars tightening; on a gust map the swathe of strongest gusts crossing
  the land; on a cloud-top map the cloud spreading, hooking round the centre, or clearing. Use a
  place's value only for the layer on screen (a gust on the gust map, never rain or pressure at a
  town on the cloud map), and only where it is the point of the moment. The marker already shows
  the centre's position and pressure, so a caption that only repeats them ("Low near Brittany:
  953 hPa") adds nothing; vague ones ("The low approaches") and lists of station readings neither.
  Keep captions on the places the story is about: an ERA5 maximum somewhere else ("36 m/s on the
  Taranaki coast" in a story about the east coast floods) distracts, however true. They never start with a time (the viewer's clock shows it), never address the viewer
  ("Watch", "Look"), and never carry caveats. A caption stays until the next one, so the last one
  must still be true at the end of the scene. `until` ends one early.

## 4. Check, correct, repeat

```bash
uv run --frozen python $S/scripts/check_history.py data/history/draft.json --scan data/history/scan.json --links
```

It rejects a draft that breaks the rules above (missing or unknown sources, uncited entries,
Wikipedia or news cited, bad times or boxes, captions starting with a time, process language) and
warns about links that do not answer, sources never cited and captions that look off the scan.
For each caption it prints what the scan says at that hour, so you can match the numbers. Correct
the draft and run it again until it prints `OK` with no warnings you can fix. Some sites refuse
automated checks: a link you have read that answers as refused is fine.

## 5. Report

Write `report.txt`:
- For each source: what you read (whole page, PDF pages, abstract only) and what you took from it.
- Figures you looked for but could not verify, and why they are left out.
- Where sources disagree.
- Where ERA5 differs most from what was observed, and the likely reason (resolution, few
  observations before satellites, intensity of small storms).
- The final check output.
