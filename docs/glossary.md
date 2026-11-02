# Glossary

Terms used across the Weather Explorer code and documentation (this backend and the
[web viewer](https://github.com/tehoro/weather-explorer-web)), in plain words.

## Forecasts and data

| Term | Meaning |
|---|---|
| **ECMWF** | European Centre for Medium-Range Weather Forecasts, an intergovernmental organisation that runs one of the world's leading global forecast models. |
| **IFS** | Integrated Forecasting System: ECMWF's global weather model. |
| **HRES** | The single high-resolution IFS forecast (in this project, the "deterministic" run: one best guess). |
| **ENS** | The IFS ensemble: about 50 forecasts started from slightly different conditions. Agreement between members shows how confident the forecast is. |
| **Ensemble probability** | The share of ENS members that exceed a threshold, e.g. "70 % chance of more than 50 mm of rain in 24 h". Used here as the open stand-in for the Extreme Forecast Index. |
| **EFI** | Extreme Forecast Index: an ECMWF product comparing a forecast with climate. Not used, because it is not in Open Data. |
| **Open Data** | ECMWF's free real-time forecast files (licence CC BY 4.0). The only data this project uses, so anyone can reproduce it. |
| **MARS** | ECMWF's full archive. Not used: most users can't access it. |
| **Run** | One forecast, named by its start time: 00, 06, 12 or 18 UTC. "The 00 UTC 2 Nov run". |
| **Step** | Hours since the run started. Open Data has 3-hourly steps to 144 h, then 6-hourly to 360 h for 00 and 12 UTC runs. |
| **Valid time** | The moment a forecast field describes: run time + step. |
| **UTC** | Coordinated Universal Time, the time zone all forecast data uses. The feed writes every time in UTC; viewers convert to local time. |
| **GRIB2** | The standard binary file format for gridded weather data. Each file holds many *messages* (one field each). |
| **`.index` file** | A text file beside each Open Data GRIB2 file listing every message's parameter, level, step, byte offset and length. |
| **HTTP Range request** | Asking a web server for only some bytes of a file. With the `.index` file, the code downloads a single field from a file of hundreds of megabytes. |
| **0.1° and 0.25° grids** | Open Data resolutions (about 11 km and 28 km at the equator). 0.1° is used for zoomed views, 0.25° for wide ones. |
| **Regridding** | Resampling a field from one grid to another, e.g. from the model's latitude/longitude grid to one that is regular in Web Mercator. |
| **ecCodes / earthkit** | ECMWF's open-source libraries for reading GRIB and working with weather data. |

## Weather quantities and layers

| Term | Meaning |
|---|---|
| **hPa** | Hectopascal, the unit of air pressure. Sea-level pressure is about 1013 hPa. Upper levels are named by pressure: 850 hPa is about 1.5 km up, 500 hPa about 5.5 km, 250 hPa about 10 km. |
| **MSLP and isobars** | Mean sea level pressure, drawn as lines of equal pressure (isobars, every 4 hPa here). Tightly packed isobars mean strong wind. |
| **Highs and lows** | Centres of high and low pressure, marked H and L. |
| **10 m wind and gusts** | Wind at the standard height of 10 m; a gust is the strongest short burst. |
| **Geopotential height (z500)** | The height of the 500 hPa surface, in decametres (dam). Its ridges and troughs show the mid-level flow. |
| **Jet stream (jet250)** | The fast river of wind near 250 hPa that steers weather systems. |
| **850 hPa wind and temperature** | Low-level flow above the friction of the ground; shows low-level jets and warm or cold air masses. |
| **TCWV** | Total column water vapour: all the water vapour above a point, in kg/m². Moist plumes and atmospheric rivers stand out. |
| **CAPE** | Convective available potential energy, in J/kg: how much energy a rising air parcel could gain. High values mean thunderstorm potential. |
| **Cloud tops (OLR)** | Outgoing long-wave radiation (Open Data `ttr`). Cold, high cloud tops give little, so the field looks like an infrared satellite image. |
| **Precipitation rate** | Derived from the model's accumulated total precipitation (`tp`): the difference between two steps divided by the hours between them. |
| **Precipitation type (ptype)** | The model's category of falling precipitation: rain, snow, freezing rain, sleet and so on. |

## Weather situations

| Term | Meaning |
|---|---|
| **Tropical cyclone** | A warm-cored storm over tropical seas: hurricane, typhoon or cyclone depending on the ocean. Officially named and tracked by regional centres. |
| **Warm core** | Tropical cyclones are warmer than their surroundings in the middle and upper atmosphere; the 3D view shows this in orange. |
| **Explosive deepening** | A low whose central pressure falls very fast (a "bomb"). The scan's `bergeron` value measures it. |
| **Atmospheric river** | A long, narrow band of very moist air that can bring heavy rain where it meets mountains. |
| **Orographic rain** | Rain produced as moist air is forced up over mountains, e.g. the Southern Alps of New Zealand. The far side is often dry (a *rain shadow*). |
| **Local winds** | Named winds caused by terrain, e.g. *bora*, *mistral*, *Santa Ana*, Greenland *tip jets*, *katabatic* winds draining off ice sheets. The skill's *explainers* describe them. |
| **Low-level jet** | A band of strong wind about 1 km up, often feeding thunderstorms with moisture. |

## Warnings and sources

| Term | Meaning |
|---|---|
| **NMHS** | National Meteorological (and Hydrological) Service, e.g. MetService in New Zealand. The authority for official forecasts and warnings in its country. |
| **WMO** | World Meteorological Organization, the UN agency that coordinates national weather services. |
| **CAP** | Common Alerting Protocol: an international XML format for warnings, with severity, area, times and text. |
| **MeteoAlarm** | The European warning portal, combining national services' warnings (yellow, orange, red). |
| **GDACS** | Global Disaster Alert and Coordination System (UN and European Commission): alerts for cyclones, floods, wildfires and droughts. |
| **Model guidance** | What a forecast model says. It is not a warning: national services issue warnings after weighing models, observations and local knowledge. |

## Software and AI

| Term | Meaning |
|---|---|
| **Highlight** | One interesting weather situation in the feed, with evidence and one to four scenes. |
| **Scene** | One animated view: a fill layer, optional contours and arrows, a box and a time window, with captions. |
| **Sweep** | The twice-daily research run that produces a new feed. |
| **Agent** | An AI model that works through a task in steps, using tools (running scripts, searching the web) rather than answering once. |
| **Skill** | A folder with written instructions, references and scripts that an agent follows (here `.claude/skills/weather-highlights/`). |
| **Validator** | Ordinary code (`publish_highlights.py`) that checks the agent's output and refuses to publish an invalid feed. |
| **Sandbox** | An isolated machine where the OpenAI agent runs, with only the files it needs and no keys. |
| **Shadow trial** | Running a second AI model alongside production, publishing nothing, to compare the results before switching. |
| **FastAPI / Uvicorn** | A Python web framework and the server that runs it. |
| **uv** | A fast Python package and project manager; `uv.lock` pins exact versions. |
| **Replit** | The hosting service the backend runs on (a small always-on virtual machine). |
| **GitHub Actions / Pages** | GitHub's automation (runs `sync.py` and `build3d.py`) and its static website hosting (serves the viewer). |
| **`repository_dispatch`** | A GitHub API call that starts another repository's workflow: how the backend tells the viewer to sync. |
| **Web Mercator** | The map projection used by most web maps. Scene grids are regular in it. |
| **Lambert azimuthal equal-area** | The viewer's projection: areas are true and shapes are fair near the centre of the view. |
| **WebGL2 and shaders** | The browser's interface to the graphics card. A *fragment shader* is a small program run for every pixel. |
| **LUT** | Look-up table: a strip of colours the shader samples to turn a value into a colour. |
| **Web Worker** | A background thread in the browser, used here for decoding frames and tracing isobars. |
| **Marching squares** | A simple algorithm for tracing contour lines through a grid of values. |
| **Lossless WebP** | An image format; here each pixel stores an 8-bit weather value, not a colour. |
| **Ray marching** | Rendering a volume (cloud) by stepping along each viewing ray and adding up density and light. |
| **WebCodecs** | Browser API for encoding video frames, used with `mp4-muxer` to make MP4 files in the browser. |
