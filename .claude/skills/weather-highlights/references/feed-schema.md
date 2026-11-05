# Highlights feed — schema v1

The feed is one JSON file, `data/highlights/latest.json`, that any client can fetch. Write a draft,
then publish it with `scripts/publish_highlights.py`, which validates every field below.

```json
{
  "generated_utc": "2026-10-22T14:10Z",
  "cutoff_utc": "2026-10-22T14:00Z",
  "runs": {"hres": "2026-10-22T00:00Z", "ens": "2026-10-22T00:00Z"},
  "coverage": "One sentence: what was checked (news worldwide, MeteoAlarm, GDACS, ENS probabilities days 1-10, HRES lows/rain/gusts to +120 h).",
  "gaps": ["GDACS timed out", "No news search for South Asia"],
  "items": [ ITEM, ... ]
}
```

## Item

```json
{
  "id": "2026-10-22-nw-europe-windstorm",
  "title": "Windstorm crossing Ireland and the UK",
  "hazards": ["wind", "rain"],
  "status": "forecast",
  "where": {"country": "Ireland and the UK", "label": "Ireland, UK, North Sea", "centre": [55.0, -5.0], "bbox": [-25, 45, 15, 65]},
  "when": {"start": "2026-10-23T00:00Z", "peak": "2026-10-24T06:00Z", "end": "2026-10-25T12:00Z"},
  "summary": "Two or three plain sentences: what, where, how severe, what happens next.",
  "why_interesting": "The scientific or visual hook: why someone exploring the data would find this worth a look.",
  "evidence": [
    {"kind": "ensemble", "text": "ECMWF ensemble (00 UTC 22 Oct run): near-certain gusts over 25 m/s in western Ireland, 22-25 Oct"},
    {"kind": "field", "text": "ECMWF model: the low deepens to 949 hPa north-west of Scotland by 00 UTC 25 Oct"},
    {"kind": "warning", "text": "Met Éireann: orange wind warning for Galway and Mayo, 06-18 UTC 24 Oct", "url": "https://www.met.ie/warnings"},
    {"kind": "news", "text": "Headline — Publisher, 22 Oct 2026", "url": "https://..."}
  ],
  "scenes": [ SCENE, ... ],
  "talking_points": ["What makes a low 'explosive'? Its pressure falls 24 hPa or more in a day.",
                     "Why is the ensemble so sure? Nearly all 50 members deepen the low in the same place."],
  "confidence": "Short phrase about the weather, e.g. 'High in the track; moderate in peak gusts'",
  "rank": 1
}
```

- `kind` (optional): `"explainer"` for an educational item about a textbook phenomenon (see the
  skill's explainer section); omit it for a normal highlight. At most 3 explainers per feed.
- `topic` (explainers only, required): the scan's `phenomenon` name, or a short name such as
  "Convection: Congo Basin". The publish script refuses an explainer whose topic or place was an
  explainer in either of the last two sweeps, or whose place lies inside a main highlight's `where.bbox`.
- `hazards`: any of wind, rain, flood, snow, ice, heat, cold, tropical-cyclone, convection, drought,
  fire-weather, fog, dust, coastal, other.
- `status`: `observed` (already happened), `ongoing`, or `forecast`.
- `where.country` (required): the country, or two, a reader would recognise ("Bulgaria", "India",
  "Australia", "Brazil and Paraguay"); for open sea or polar items the ocean or region ("North
  Pacific", "Arctic", "Antarctica"). At most 30 characters. The viewer puts it before the title
  ("Bulgaria: Burgas moves to red…") unless the title already names it. `where.label` gives the
  finer detail ("Eastern Bulgaria and Romania's Black Sea coast").
- `bbox` is `[west, south, east, north]` in degrees. For a box crossing the dateline, `east` may be
  greater than 180 (e.g. `[150, -10, 210, 30]`).
- Times are ISO 8601 UTC with a `Z`.
- **Measurements in text** (titles, summaries, why_interesting, evidence, captions): write a number
  followed by a unit, preferably SI: `60 m/s`, `250 mm`, `50 mm/h`, `35 °C`, with ranges as
  `60–80 m/s`. `km/h`, `mph`, `kt` and `°F` are also recognised. Give each value once (or as
  `60 m/s (134 mph)`), not as prose like "sixty metres per second". The app rewrites recognised
  measurements in each viewer's units (mph, km/h, °F, inches...). Pressure stays in hPa.
- **Times in text**: write every specific time in UTC **with its date**, hour first: `18 UTC 24 Oct`,
  `06:30 UTC 26 Oct`, ranges as `06-12 UTC 26 Oct` (a range past midnight is dated by its start:
  `21-03 UTC 27 Oct`). Never give a bare `06 UTC` or a weekday alone ("Sunday 21:00"), and convert
  warning times quoted in local time (BST, CET, JST...) to UTC with the date. The web viewer rewrites
  dated times in each viewer's own time zone. A model run is named with the word `run` after it,
  `00 UTC 22 Oct run`, so it is left as written. Loose periods ("overnight", "early next week")
  are fine where no exact time is meant.
- `evidence`: `warning`, `news`, `official` and `observation` items **must** carry a URL that came
  back from your own searches or fetches. `ensemble` and `field` items cite the scan output.
  `warning` and `official` links must be on an agency's own site (`references/agencies.json`,
  the sites in `states.json`, or a government domain such as `.gov`, `.gov.xx`, `.go.xx`,
  `.gob.xx`, `.gouv.xx`, `.govt.xx`, `.mil` or `.int`). The publisher relabels any other one as
  `news` and says so: a news story or a reposted press release is news, whatever it quotes.
  Every `url` must be a page people can read, never a data feed (JSON, XML, CAP, RSS, API): the
  publisher swaps a few known feeds for their public page and rejects the rest.
  Each `text` is one plain sentence of at most 200 characters: the source first, then the date and
  the number that matters. No script, file or variable names and no method notes: the publisher
  rejects them.

## Scene

A scene is one view the app can show: a fill layer, optional contours and arrows, a box and a
time window that plays as an animation. Give each highlight 1-4 scenes that tell a short story:
setting → hazard → why the forecast system flags it.

```json
{
  "title": "The low deepens as it approaches Ireland",
  "fill": "wind10",
  "contours": "mslp",
  "vectors": "wind10",
  "bbox": [-30, 42, 12, 66],
  "start": "2026-10-23T00:00Z",
  "end": "2026-10-25T12:00Z",
  "focus": [{"lat": 53.3, "lon": -6.3, "label": "Dublin"}],
  "captions": [
    {"at": "2026-10-23T12:00Z", "text": "A shallow wave on the polar front"},
    {"at": "2026-10-24T06:00Z", "text": "Pressure has fallen ~30 hPa in 24 h: a 'weather bomb'", "until": "2026-10-25T00:00Z"}
  ],
  "hints_3d": {"cross_section": [[50, -25], [58, 0]], "camera": "from the south-west, low angle", "emphasise": "sting-jet region south of the centre"}
}
```

- `fill` (required): one of `mslp`, `wind10`, `gust`, `t2m`, `precip`, `tcwv`, `jet250`, `z500`,
  `ens_rain20`, `ens_rain50`, `ens_gust15`, `ens_gust25` (ENS probabilities, %), and:
  - `rain24`: rain in the 24 h ending at each frame (mm), the form most rain warnings use; use it
    to show and check a rain warning (web viewer only). `precip` is a rate (mm/h), averaged over
    the 3 or 6 h before each frame.
  - `ptype`: precipitation type (rain, snow, freezing rain, wet snow, rain-and-snow, ice pellets,
    freezing drizzle; light/moderate/heavy by rate). Instantaneous, so start the window at a run time.
  - `wind850`: wind at 850 hPa (~1.5 km): low-level jets, warm conveyor belts, monsoon flow.
  - `t850`: temperature at 850 hPa: air masses, cold-air outbreaks, warm sectors (no daily cycle).
  - `cape`: storm energy (most-unstable CAPE, J/kg): where thunderstorms can be strong.
  - `olr`: cloud tops from outgoing longwave radiation, a satellite-like view (white = cold, high cloud).
  - Stratosphere, about 30 km up (web viewer only): `z10hpa` (10 hPa height: the polar vortex's
    shape, displacement or split), `t10hpa` (10 hPa temperature: a sudden stratospheric warming)
    and `wind10hpa` (10 hPa wind: the polar night jet round the vortex). Use them for polar vortex
    stories, not for weather at the ground.
- `contours` (optional): `mslp` (isobars every 4 hPa), `z500` (every 6 dam) or `z10hpa` (every 16 dam).
- `vectors` (optional): `wind10`, `wind850`, `jet250` or `wind10hpa` (moving streaks).
- Boxes reaching past 75°N or 75°S are drawn on a polar grid centred on the pole, so the pole
  itself is covered. For a hemispheric polar view use a box such as `[-180, 40, 180, 90]`.
- Playback speed is the viewer's choice in the app (default 1 frame per second); scenes don't set it.
- `focus`: up to 5 labelled points to mark (cities, the storm centre at peak).
- `captions`: up to 6 short narration lines. Each shows from its `at` until the next caption, or
  until its optional `until` if that is sooner; the last one stays to the end of the scene. Every
  `at` must be a frame time (see the skill: 3-hourly to +144 h, then 6-hourly, thinned to an even
  6, 12 or 24 h on 00/12 UTC for long windows); `check_scenes.py` lists them. The scene builder moves a
  caption that misses to the nearest frame. Write each as an event at that moment, with numbers
  from the frame shown, and without a time: the viewer's clock beside it shows that.
- `parallels` (optional): up to 3 latitudes drawn as labelled circles, e.g. `[60]` for the 60°N
  circle round which the polar vortex wind is averaged.
- `hints_3d`: free-form guidance for 3D clients (Vision Pro). The 2D app ignores it.
- Layers cover roughly the last 72 h and up to +360 h from the latest run. `ens_*` layers are
  24 h windows every 12 h from the latest 00/12 UTC ensemble; start/end pick the windows shown.

## Choosing scenes that look good

| Situation | Scene 1 (setting) | Scene 2 (hazard) | Scene 3 (signal) |
|---|---|---|---|
| Extratropical windstorm | `z500` fill + `mslp` contours, ocean-basin box, 3-4 days | `gust` or `wind10` fill + `mslp` contours + `wind10` vectors, 20-30° box, 3-hourly | `ens_gust25` |
| Tropical cyclone | `tcwv` fill + `mslp` contours, basin-scale box (track context) | `wind10` fill + `mslp` + vectors, 12-15° box on the core | `precip` fill on landfall area |
| Atmospheric river / heavy rain | `tcwv` fill + `mslp` contours, long thin plume in a wide box | `rain24` fill on the warned area (totals, as the warning gives them), or `precip` for how the rain moves | `ens_rain50` |
| Heatwave / cold spell | `z500` fill + contours (ridge/trough), continental box | `t2m` fill, 5-7 days so the diurnal cycle pulses | — |
| Jet-stream story | `jet250` fill + `jet250` vectors, hemispheric box | `mslp` fill + contours under the jet exit | — |
| Tip jet, fall or gap wind (explainer) | `mslp` fill + contours, synoptic box showing the pressure pattern that drives it | `wind10` fill + `wind10` vectors + `mslp` contours on the scan's `scene_bbox`, 1-3 days, 3-hourly | `gust` fill, same box |
| Organised tropical or summer convection | `olr` (cloud tops), wide box: the storm clusters as a satellite would see them | `cape` fill + `mslp` contours: the fuel, 1-2 days | `precip` fill on the cluster, 3-hourly so the cells pulse |
| Freezing rain / ice, snow events | `t850` fill + `mslp` contours (warm air over cold: the setting) | `ptype` fill + `mslp` contours on the area, 1-2 days | — |
| Low-level jet (explainer) | `wind850` fill + `wind850` vectors, regional box, 2 nights so the night-time peaks show | `cape` or `precip` fill where the jet's moisture feeds storms | — |
| Tropical cyclone, alternative opener | `olr` fill + `mslp` contours: the eye and spiral bands like a satellite loop | — | — |
| Polar low | `t850` fill + `mslp` contours, regional box: cold air flowing off the ice over warmer sea | `wind10` fill + `mslp` contours + `wind10` vectors on the track, under 50° wide, 6-hourly over 1-2 days | `olr`: the small comma or spiral cloud |
| Warm intrusion into the Arctic or Antarctic | `tcwv` fill + `mslp` contours, polar box (e.g. `[-180, 60, 180, 90]`): the moisture plume drawn poleward | `t2m` fill, same box: the warm air over the ice | — |
| Arctic cyclone or extreme Southern Ocean low | `mslp` fill + contours, polar box, 3-5 days | `wind10` fill + `wind10` vectors + `mslp` contours | — (sea ice is not in Open Data: describe it in words) |
| Katabatic wind (explainer) | `t2m` fill + `wind10` vectors, the ice sheet and coast (e.g. `[120, -80, 170, -62]` for Adélie Land): cold air draining off the plateau | `wind10` fill + `wind10` vectors on the scan's `scene_bbox`, 2-3 days, 3-hourly | — |
| Polar vortex weakening, split or sudden stratospheric warming | `z10hpa` fill + contours, `[-180, 40, 180, 90]`, 7-10 days (6-hourly beyond +144 h is fine): the vortex stretching, moving off the pole or splitting | `t10hpa` fill + `z10hpa` contours, same box: the warming over the pole | `wind10hpa` fill + vectors + `"parallels": [60]`: the jet pushed off the pole, so its 60° average weakens or reverses; or `z500` / `t850` for the weather below, saying the link to the surface is weeks away and uncertain |

Rules of thumb:
- Boxes narrower than ~50° are drawn from the 0.1° data (~10 km), wider ones from 0.25°: zoom
  in on hazards to show detail.
- Wider boxes need longer windows; small boxes look best at 3-hourly frames over 1-3 days.
- Aspect ratio near the phone's portrait or landscape view (about 1:1 to 2:1) avoids empty margins.
- Start the window shortly before the interesting development, so the viewer sees it happen.
- Precipitation and gusts are interval fields: they need a step after the run start.
- Captions are the narration: one idea each, under ~70 characters, tied to what is visible.
  No leading time stamp ("At 21 UTC 4 Nov:"): the publisher rejects it.
