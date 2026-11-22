# KIZUNA – AI-Powered Dynamic Irrigation: Backend (part 1 of 4)

FastAPI backend that computes **how much** water each zone needs and **when**, using the
FAO-56 method, validates every decision with safety rules, sends commands to the ESP32,
and verifies what was actually delivered.

```
ESP32 (sensors, valves, flowmeter)  <--HTTP-->  this backend  <--HTTP-->  dashboard (part 4)
                                                     ^
                     vision module (part 3) ---------+   Open-Meteo weather (when online)
```

## Quick start

Requires **Python 3.10 or newer** (`python --version`).

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                    # set your farm coordinates + device key
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

- API docs (try every endpoint): http://localhost:8000/docs
- Tests: `pytest -q` from the `kizuna-backend` folder (78 tests, including FAO-56 worked examples).
  If the `pytest` command is not found, use `python -m pytest -q`.
- Demo without hardware: in a second terminal
  `python tools/esp32_simulator.py --api http://localhost:8000 --key change-me`
  (add `--blocked-zone 1` to simulate a blocked pipe and see the anomaly detection)

On first run the database is created with the FAO-56 tomato and pepper profiles and two demo zones.

## How a decision is made (Step 4 + 5 of the spec)

| Step | What | Source |
|---|---|---|
| 1 | Days after planting → growth stage | crop profile (FAO-56 Table 11) |
| 2 | Kc for today, adjusted for wind and humidity | FAO-56 Table 12, Eq. 62/65/66 |
| 3 | ET0 | Open-Meteo (FAO Penman-Monteith); offline: Hargreaves Eq. 52 from the ESP32 air temperature |
| 4 | ETc = Kc × ET0 | Eq. 56 |
| 5 | Root depth, TAW, p (adjusted for ETc), RAW | Table 22, Eq. 82, 83 |
| 6 | Depletion Dr from **measured** soil moisture | θFC/θWP from Table 19 (or field values) |
| 7 | Dr ≤ RAW → no irrigation (and estimated days until next one) | |
| 8 | Net = Dr, gross = net / efficiency, litres = gross × area | |
| 9 | Rain ≥ need with probability ≥ 60 % → postpone (rain < 0.2·ET0 ignored, FAO-56) | Open-Meteo |
| 10 | Inside an irrigation window → now; otherwise schedule the next window, unless waiting would exhaust the water (Dr would reach TAW) | |

The irrigation volume never comes from AI. The AI only identifies the crop (part 3).

### Safety rules (any one blocks automatic irrigation)
Emergency stop · manual mode · crop identification not confirmed · no/invalid/stale soil
reading · tank below minimum · open blocking anomaly (leak, low/over-delivery) · a command
already active · volume capped per event. Commands are re-checked at dispatch time and
expire after 2 h so the ESP32 never runs on stale data.

### Provenance labels
Every number in a decision says what it is: `measured`, `calculated`, `forecast`,
`reference`, `configured` or `ai_prediction` (spec constraint: clearly distinguish them).

## Post-irrigation verification (Step 7)
1. Delivered (flowmeter) vs target volume, ±10 % → `low_delivery` / `over_delivery` anomaly.
2. 30 min later: measured soil-moisture rise vs expected rise → `moisture_response` warning.
3. Flow while all valves are closed → `leak` (critical).

## Comparison with a fixed schedule (prototype requirement)
Set a zone to `"strategy": "fixed"` with `fixed_volume_l` and `fixed_time`: it is then
watered like a conventional timer. `GET /api/report/comparison?days=14` returns, per
zone, **measured** water delivered, delivery accuracy, response time and the soil-moisture
series. A water-saving figure is only produced when both strategies have measured data,
and it comes with caveats (single trial, same crop/soil needed, crop not stressed).

## ESP32 protocol (header `X-Device-Key`)
| Call | When |
|---|---|
| `POST /api/controller/readings` | every minute: soil raw ADC per valve channel, air T/RH, tank %, flow, open valves |
| `GET /api/controller/next-command` | every few seconds: `idle`, `irrigate` (channel, target L, timeout), `stop`, `stop_all` |
| `POST /api/controller/commands/{id}/started` | valve opened |
| `POST /api/controller/commands/{id}/report` | valve closed: `delivered_l`, `stop_reason` |

The ESP32 stops on **volume** (flowmeter), with the timeout as a backup.

## Farmer / dashboard endpoints
Crops `GET/POST/PUT /api/crops` · zones `GET/POST/PUT /api/zones`, `GET /api/zones/{id}/status`,
`/readings`, `/decisions`, `POST /decide`, `POST /manual-irrigation` · identification
`POST /api/zones/{id}/identifications`, `POST /api/identifications/{id}/confirm|reject` ·
`POST /api/commands/{id}/stop` · `POST /api/system/emergency-stop`, `/api/system/mode` ·
`GET /api/anomalies`, `POST /api/anomalies/{id}/ack` · `GET /api/weather`,
`POST /api/weather/refresh` · `GET /api/reference` (FAO tables used).

## Growth stages (AGRIWISE step 1)
Each crop has its **own** development cycle (pepper, potato, tomato, eggplant): stages, BBCH codes,
visual cues and field checks, aligned with its FAO-56 periods. See [docs/CROP_CYCLES.md](docs/CROP_CYCLES.md)
and [docs/REFERENCES.md](docs/REFERENCES.md).

| Call | What |
|---|---|
| `GET /api/cycles`, `GET /api/crops/{key}/cycle` | the cycles, with sources and assumptions |
| `GET /api/zones/{id}/stage` · `GET /api/plants/{id}/stage` | current stage, days to next stage, evidence, agreement, what to confirm |
| `POST /api/zones/{id}/stage/confirm` (`stage_key`, optional `plant_id`, `observed_on`) | farmer confirms or corrects |
| `POST /api/stage-observations/{id}/confirm` / `reject` | validate or reject the stage from a photo |
| `GET /api/zones/{id}/stage/history` · `GET /api/plants/{id}/stage/history` | photos, confirmations, transitions |
| `GET/POST /api/zones/{id}/plants` | individual plants (own planting date, `has_own_valve` for PRECISION later) |

A confirmed stage shifts the FAO-56 curve: Kc and root depth in the irrigation decision follow it.

### Degree-days, stage dates and needs (message 8, steps 1–3)
Pepper and potato stages carry a description (EN/FR), their documented needs and their sensitivity
to water deficit as worded by FAO. The stage position is corrected by the **degree-days** of the
season compared with the local **climate normals** (5 years of Open-Meteo archive): a warmer season
moves the crop ahead of the calendar. Without normals or with too few real temperatures (< 80 % of
the days), the calendar is used and the reason is shown. Tomato and eggplant: calendar only for now.

| Call | What |
|---|---|
| `POST /api/climate/refresh` · `GET /api/climate/status` | download / check the climate normals (also refreshed daily by the scheduler) |
| `GET /api/zones/{id}/stage` | + `stage_start_date`, `next_stage_date`, `cycle_progress_pct`, `stage_progress_pct`, `timing` (calendar vs degree-days, possible stages), `timeline` (estimated dates of every stage), `certainty` (confirmed/high/medium/low + reasons) |
| `GET /api/zones/{id}/thermal` | day-by-day degree-days of the season vs normal, with the source of each day (measured / forecast / reanalysis / missing) |

### Irrigation plan (message 8, step 4)
`GET /api/zones/{id}/irrigation-plan` (optional `?days=7`) – frequency ("every N days"), irrigations
per week, volume and duration per irrigation, next irrigation (date and time), water per week, status
(irrigation needed / waiting / postponed for rain / blocked), the next stage and its date, and a
day-by-day table (stage, Kc, ET0, ETc, root depth, TAW, p, RAW, rain, depletion).
It replays the decision engine's rule every 15 minutes over the 7-day forecast, starting from the
measured soil moisture: nothing is a fixed frequency. Missing inputs are listed in `missing`.
The answer to a photo (`POST /api/vision/observations`) now includes the same plan in `irrigation`;
the phone page and `python -m vision.capture --image photo.jpg --zone 2` print it.

### Dashboard
The dashboard is in its own folder, **kizuna-dashboard** (`python server.py` → http://localhost:8080).
The backend only provides the data: `GET /api/dashboard/overview`, `/api/dashboard/zones/{id}/history`,
`/api/dashboard/camera`, and the farm-map layout `GET/PUT/DELETE /api/dashboard/layout`.
The page descriptions below are kept for reference.

* **Irrigation** – the water circuit of a zone as six stations on a pipe: ESP32 sensors → FAO-56
  calculation → decision → solenoid valve → flow meter → post-irrigation check. Water moves along the pipe
  only while a valve is really open. Below: the plant (stage, cycle progress, next stage, needs), the
  irrigation plan (frequency, next irrigation, volume, duration, weekly water), the 9 sensors (those not
  wired are shown as "non installé"), a chart of soil moisture / irrigation threshold / ETc / irrigations
  (24 h, 3 days, 7 days), the 7-day plan, the stage timeline and the alerts. Buttons: recompute,
  manual irrigation, stop, emergency stop.
* **Caméra** – send a photo (it goes to the vision service, then to the backend) and follow the
  procedure step by step: photo → crop → visual stage → health hint → ESP32 sensors at the same moment →
  stage kept (with confirm / keep-calendar buttons when photo and calendar disagree) → irrigation plan →
  farmer action. History of analysed photos with thumbnails.

Every value is labelled *mesuré*, *calculé*, *prévision*, *IA*, *indice IA* or *manquant*. The pages refresh
every 10–15 s. Data come from `GET /api/dashboard/overview`, `/api/dashboard/zones/{id}/history` and
`/api/dashboard/camera`. Photos are read from the vision service (`VISION_PUBLIC_URL`, default
`http://localhost:8100`; set it to the PC's IP to open the dashboard from a phone).

First run online: `POST /api/climate/refresh` once (the scheduler does it too), then `GET /api/zones/2/stage`.
`python tools/make_crop_cycles_doc.py` regenerates docs/CROP_CYCLES.md from the code.

## Vision and rain (part 3)
| Call | What |
|---|---|
| `POST /api/vision/observations` (device key) | result of one analysed image: crop + confidence, stage, leaf cover, pests, animals, motion, trap count, rain now |
| `GET /api/vision/observations?zone_id=` | history for the dashboard |

- Crop results go through the identification rules (≥ 0.80 and consistent → confirmed, otherwise the farmer confirms).
- Pests, animals, motion and sticky-trap counts create alerts (`pest:*`, `animal:*`, `motion`, `insect_trap`) that **never block irrigation**.
- **Rain now** (rain sensor `rain_sensor_wet` in the controller readings, or camera with confidence ≥ 0.80 in the last 60 min) postpones irrigation; the sensor wins over the camera.
- **Rain forecast**: after each weather refresh, a `rain_forecast` notification is raised when tomorrow's rain is likely (probability ≥ 60 % and ≥ 1 mm) and cleared when it no longer is.
- Existing SQLite databases get the new columns automatically at start-up.

## Assumptions to check before field tests
- Tomato Kc end = 0.80 (FAO range 0.70–0.90); root depths use the low end of FAO ranges; initial root depth 0.2 m.
- Soil θFC/θWP are FAO Table 19 mid-range values – measure your soil if possible (`theta_fc`, `theta_wp` per zone).
- Irrigation efficiencies (drip 0.90, sprinkler 0.75, surface 0.60) are typical values – measure on site.
- Runoff and capillary rise are not modelled (flat, drip-irrigated plots).
- Soil sensors need a two-point calibration per zone (`sensor_raw_dry/theta_dry/raw_wet/theta_wet`).
- Irrigation windows, rain probability threshold and tolerances are operating choices in `.env`.

## Next parts
2. ESP32 firmware (Wokwi simulation + real board): sensors, valves, flowmeter, local safe mode offline.
3. Vision module: plant identification with confidence + farmer confirmation.
4. Web dashboard.
