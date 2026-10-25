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
  "where": {"label": "Ireland, UK, North Sea", "centre": [55.0, -5.0], "bbox": [-25, 45, 15, 65]},
  "when": {"start": "2026-10-23T00:00Z", "peak": "2026-10-24T06:00Z", "end": "2026-10-25T12:00Z"},
  "summary": "Two or three plain sentences: what, where, how severe, what happens next.",
  "why_interesting": "The scientific or visual hook: why someone exploring the data would find this worth a look.",
  "evidence": [
    {"kind": "ensemble", "text": "ENS: 100 % chance of gusts >25 m/s over western Ireland, days 1-4 (00 UTC 22 Oct run)"},
    {"kind": "field", "text": "HRES low 949 hPa near 59N 5W at 00 UTC 25 Oct"},
    {"kind": "warning", "text": "Met Éireann orange wind warning", "url": "https://..."},
    {"kind": "news", "text": "Headline — Publisher, 22 Oct 2026", "url": "https://..."}
  ],
  "scenes": [ SCENE, ... ],
  "talking_points": ["What makes a low 'explosive'?", "Why is the ensemble so confident?"],
  "confidence": "Short phrase, e.g. 'high: warnings issued, ENS and HRES agree'",
  "rank": 1
}
```

- `hazards`: any of wind, rain, flood, snow, ice, heat, cold, tropical-cyclone, convection, drought,
  fire-weather, fog, dust, coastal, other.
- `status`: `observed` (already happened), `ongoing`, or `forecast`.
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
    {"at": "2026-10-24T06:00Z", "text": "Pressure has fallen ~30 hPa in 24 h: a 'weather bomb'"}
  ],
  "hints_3d": {"cross_section": [[50, -25], [58, 0]], "camera": "from the south-west, low angle", "emphasise": "sting-jet region south of the centre"}
}
```

- `fill` (required): one of `mslp`, `wind10`, `gust`, `t2m`, `precip`, `tcwv`, `jet250`, `z500`,
  `ens_rain20`, `ens_rain50`, `ens_gust15`, `ens_gust25` (ENS probabilities, %).
- `contours` (optional): `mslp` (isobars every 4 hPa) or `z500` (every 6 dam).
- `vectors` (optional): `wind10` or `jet250` (direction arrows).
- Playback speed is the viewer's choice in the app (default 1 frame per second); scenes don't set it.
- `focus`: up to 5 labelled points to mark (cities, the storm centre at peak).
- `captions`: up to 6 short narration lines, each shown from its time onwards.
- `hints_3d`: free-form guidance for 3D clients (Vision Pro). The 2D app ignores it.
- Layers cover roughly the last 72 h and up to +360 h from the latest run. `ens_*` layers are
  24 h windows every 12 h from the latest 00/12 UTC ensemble; start/end pick the windows shown.

## Choosing scenes that look good

| Situation | Scene 1 (setting) | Scene 2 (hazard) | Scene 3 (signal) |
|---|---|---|---|
| Extratropical windstorm | `z500` fill + `mslp` contours, ocean-basin box, 3-4 days | `gust` or `wind10` fill + `mslp` contours + `wind10` vectors, 20-30° box, 3-hourly | `ens_gust25` |
| Tropical cyclone | `tcwv` fill + `mslp` contours, basin-scale box (track context) | `wind10` fill + `mslp` + vectors, 12-15° box on the core | `precip` fill on landfall area |
| Atmospheric river / heavy rain | `tcwv` fill + `mslp` contours, long thin plume in a wide box | `precip` fill, regional box | `ens_rain50` |
| Heatwave / cold spell | `z500` fill + contours (ridge/trough), continental box | `t2m` fill, 5-7 days so the diurnal cycle pulses | — |
| Jet-stream story | `jet250` fill + `jet250` vectors, hemispheric box | `mslp` fill + contours under the jet exit | — |

Rules of thumb:
- Boxes narrower than ~50° are drawn from the 0.1° data (~10 km), wider ones from 0.25°: zoom
  in on hazards to show detail.
- Wider boxes need longer windows; small boxes look best at 3-hourly frames over 1-3 days.
- Aspect ratio near the phone's portrait or landscape view (about 1:1 to 2:1) avoids empty margins.
- Start the window shortly before the interesting development, so the viewer sees it happen.
- Precipitation and gusts are interval fields: they need a step after the run start.
- Captions are the narration: one idea each, under ~70 characters, tied to what is visible.
