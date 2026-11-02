# One highlight, end to end

This follows one real highlight from the forecast files to the pixels on screen: **"Moist
north-westerlies keep New Zealand's western ranges wet"**, from the sweep of 15:10 UTC on
2 November 2026. It touches every part of the system, in this repository and in the
[web viewer](https://github.com/tehoro/weather-explorer-web).

Terms in **bold** on first use are in the [glossary](glossary.md).

```
Open Data ─▶ scan ─▶ agent ─▶ validator ─▶ feed ─▶ scenes.py ─▶ server ─▶ sync.py ─▶ WebP ─▶ browser
 (GRIB2)     signals  writes    checks      JSON     field data   files    (GitHub)   frames   shader
```

## 1. The forecast arrives

ECMWF's model produces a new **run** four times a day. The 06 UTC run's 0.1° files are complete on
**Open Data** at about 14:27 UTC. Each **step** (3 h, 6 h, ...) is a **GRIB2** file of a few hundred
megabytes, with an **`.index`** file beside it listing where each field sits in the file.

At 15:00 UTC the server's scheduler (`server.py`) starts a **sweep**.

## 2. The scan finds a signal

The agent first runs [`scan_fields.py`](../.claude/skills/weather-highlights/scripts/scan_fields.py).
Among its signals is `rain24`: places where the model's 24 h precipitation total is at least
100 mm. This sweep's scan included:

> `rain24`: 123.05 mm near 43°S, 171°E for the 24 h ending 00 UTC 3 Nov (00 UTC 2 Nov run)

That point is on the Southern Alps, where moist north-westerly air is forced up the mountains
(**orographic rain**). The scan only downloads the fields it needs: `fields.py` reads the
`.index` file, finds the byte range of `tp` (total precipitation) at the right step, and fetches
just those bytes with an **HTTP Range request**.

## 3. Warnings and news

The agent then runs
[`fetch_warnings.py`](../.claude/skills/weather-highlights/scripts/fetch_warnings.py) and searches
the news. At the time, the script read only European (MeteoAlarm) and global disaster (GDACS)
alerts, so New Zealand had no official warning in it. The agent found a MetService forecast
release, but on a news site that republishes press releases, and cited it as `official`. It also
noted that MetService's warnings page "exposed no readable bulletin" (that page is built by
JavaScript, which the agent's tools can't run).

**This exposed a weakness that was fixed the next day:**
- `fetch_warnings.py` now reads MetService's **CAP** feed directly. It held two orange Heavy Rain
  Warnings for exactly this event, e.g. "130 to 180 mm of rain ... The ranges of the Westland
  District", with a permanent link on MetService's site.
- The validator would now relabel that news-site citation from `official` to `news`.

It is a good example of why the AI's output is checked by ordinary code, and why evidence rules
are written down.

## 4. The agent writes the highlight

The agent writes a draft feed following
[`feed-schema.md`](../.claude/skills/weather-highlights/references/feed-schema.md). This item,
shortened:

```json
{
  "id": "2026-11-02-new-zealand-alpine-rain",
  "title": "Moist north-westerlies keep New Zealand’s western ranges wet",
  "hazards": ["rain", "flood"],
  "status": "ongoing",
  "where": {"label": "New Zealand South Island and the Southern Alps",
            "centre": [-43, 171], "bbox": [165, -47, 177, -39]},
  "when": {"start": "2026-11-02T00:00Z", "peak": "2026-11-02T18:00Z", "end": "2026-11-04T12:00Z"},
  "summary": "... HRES produces a local 123 mm in the 24 h ending 00 UTC 3 Nov near the Southern Alps ...",
  "why_interesting": "Humid north-westerly flow is forced up the Southern Alps, concentrating rain near the divide while the eastern lowlands can be warmer and much drier. ...",
  "evidence": [
    {"kind": "official", "text": "MetService forecast release ...", "url": "https://www.scoop.co.nz/..."},
    {"kind": "field", "text": "scan.rain24[21], 00 UTC 2 Nov run: 123.05 mm near 43°S, 171°E ..."}
  ],
  "scenes": [
    {"title": "Moist flow reaches New Zealand from the Tasman", "fill": "tcwv", "contours": "mslp",
     "bbox": [153, -50, 180, -33], "start": "2026-11-02T00:00Z", "end": "2026-11-04T12:00Z",
     "captions": [{"at": "2026-11-02T12:00Z", "text": "Humid flow reaches the western South Island"}],
     "focus": [{"lat": -44.67, "lon": 167.92, "label": "Milford Sound"}]},
    {"title": "Rain concentrates along the Southern Alps", "fill": "precip",
     "bbox": [165, -47, 177, -39], "start": "2026-11-02T03:00Z", "end": "2026-11-04T12:00Z"}
  ],
  "talking_points": ["Why can the Alps be very wet while eastern New Zealand is warm?"],
  "confidence": "High for the agency-described wet western pattern; medium for local model totals. ..."
}
```

The two scenes tell a short story: first the moist air (**TCWV**, with **isobars**) arriving from
the Tasman Sea, then the rain it produces on the mountains. Note the habits the skill asks for:
times in UTC with the date, a number with its unit (`123 mm`, which viewers convert to inches if
they want), and the model point clearly separated from the observation.

## 5. The validator checks it

The agent runs
[`publish_highlights.py`](../.claude/skills/weather-highlights/scripts/publish_highlights.py). It
checks, among other things, that:
- every required field is there and hazards, status and layers are known;
- boxes are valid and time windows fall inside what Open Data holds;
- `warning`, `news`, `official` and `observation` evidence has a real link (and, since 3 Nov,
  that `warning` and `official` links are on an agency's own site);
- explainers aren't repeated from the last two sweeps.

If anything fails, it prints the problems and publishes nothing. When it passes, it replaces
`data/highlights/latest.json` in one atomic step, so a client never reads a half-written file.

## 6. The scene data is built

[`scenes.py`](../scenes.py) (in a low-priority child process) asks
[`fields.py`](../fields.py) for each scene's frames. For the rain scene:
- **Resolution:** the box is small, so 0.1° data is used.
- **Box:** widened to 158.8°E–183.2°E, 50°S–35.7°S, so the equal-area map has no empty edges when
  the viewer zooms out a little. The grid is 244 × 196, regular in **Web Mercator**.
- **Frames:** every 3 h from 03 UTC 2 Nov to 12 UTC 4 Nov (20 frames). For times already past, the
  newest run that covers each time is used, so early frames come from the 00 UTC run and later
  ones from the 06 UTC run.
- **Rate from totals:** the model stores precipitation accumulated since the run began. The rate
  in mm/h is the difference between two steps divided by the hours between them.
- **Packing:** values are scaled to 16-bit integers between the scene's minimum and maximum
  (65535 = missing), base64-encoded and saved as gzipped JSON:
  `data/scenes/20261102T1510Z/2026-11-02-new-zealand-alpine-rain-1-fill.json.gz`.

The server then serves the feed at `/api/highlights` and the files at `/scenes/...`, and
[`web_notify.py`](../web_notify.py) sends a **`repository_dispatch`** to the web viewer's
repository.

## 7. The web viewer converts it

In the web viewer repository, a **GitHub Actions** workflow runs `sync.py`. It downloads the feed
and each new scene once, and converts each frame to a lossless 8-bit **WebP** image in
`data/p/20261102T1510Z/2026-11-02-new-zealand-alpine-rain-1/`:
- `meta.json` describes the scene: layer `precip`, units mm/h, `lo` 0 and `hi` 11.4 mm/h, the box,
  `nx`/`ny`, `scale: "sqrt"`, the colour stops and each frame's valid time, run and step.
- `fill/000.webp`, `fill/001.webp`, ... one per frame, 7–9 KB each. Each pixel holds
  `round(254 × sqrt((value − lo) / (hi − lo)))`, with 255 for missing. The square root keeps detail
  in light rain.

Then the workflow publishes the site to **GitHub Pages**. You can fetch these files yourself:
<https://tehoro.github.io/weather-explorer-web/data/feed.json> lists every scene's `package`
folder.

## 8. The browser draws it

In the viewer's [`site/app.js`](https://github.com/tehoro/weather-explorer-web/blob/main/site/app.js)
(see the viewer's [code map](https://github.com/tehoro/weather-explorer-web/blob/main/docs/code-map.md)):
1. `loadFeed` and `route` read the feed and the link (`#h=<tag>/<id>&s=1`), and `loadScene` loads
   `meta.json`.
2. A **Web Worker** (`worker.js`) decodes each WebP frame back into numbers, off the main thread.
   For the first scene it also traces isobars with **marching squares** and finds highs and lows.
3. `frameTexture` uploads each frame to the graphics card. `makeLUT` turns the colour stops into a
   **look-up table**.
4. `drawField` runs a **WebGL2 fragment shader** for every pixel on screen. It takes the pixel's
   position back through the **Lambert azimuthal equal-area** projection to longitude and latitude,
   then to Web Mercator y, then to a place in the 244 × 196 grid. It reads the value from the two
   frames either side of the current time and blends them (`mixT`), undoes the square-root scale
   (`t * t`), and looks up the colour.
5. `drawOverlay` draws the isobars, H and L, and the `focus` places (Milford Sound, Hokitika).
6. The timeline plays the frames and shows each caption at its time.
7. `units.js` rewrites "123 mm" and "00 UTC 3 Nov" in the text into the viewer's units and time
   zone (for example "4.8 in" and "13:00 NZDT 3 Nov").

Nothing in step 8 touches the backend: the browser only downloads static files.

## Try it

- Open the live viewer and pick a highlight, then open the browser's developer tools and watch
  the Network tab: `feed.json`, `meta.json`, then the WebP frames.
- Run the free scripts locally (see the [README](../README.md#running-it-yourself)) and compare
  `scan.json` and `warnings.json` with the next feed.
- Change a colour stop in `fields.py` or the shader in `app.js` and see the effect.
