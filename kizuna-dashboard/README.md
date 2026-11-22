# AGRIWISE dashboard (KIZUNA 2026)

The farmer's screen. Separate from the backend and from the photo analyzer: it only shows what they
compute, and sends the farmer's actions (confirm a stage, irrigate by hand, emergency stop).

```
kizuna-dashboard/   this folder: the pages + a tiny server (port 8080), Python standard library only
kizuna-backend/     API, FAO-56 water calculation, decisions, ESP32 link        (port 8000)
kizuna-vision/      photo analyzer: crop, stage, health, trap, rain, motion      (port 8100)
```

## Start

```powershell
cd kizuna-dashboard
python server.py
```

Open **http://localhost:8080**. The backend (8000) and the analyzer (8100) must be running too.
From a phone on the same Wi-Fi: start the backend and the analyzer with `--host 0.0.0.0`, then open
`http://<PC IP address>:8080` on the phone: the pages find the backend and the analyzer on the same
machine automatically. If they run elsewhere:

```powershell
python server.py --backend http://192.168.1.20:8000 --vision http://192.168.1.20:8100
```

## Pages

| Page | What the farmer sees |
|---|---|
| **Plan de la ferme** (`/`) | The farm as a map: tank, flow meter, main pipe, one valve per plot, the ESP32 box and every plot with its crop, stage, soil moisture (with the irrigation threshold) and irrigation status. Water flows in the pipes only to the plots being irrigated; data dots run from the ESP32 to the sensors and commands to the valves. A plot with a problem turns yellow or red and carries a badge with the number of problems; touching it opens what is wrong and **what to do**. Drag to move, wheel or pinch to zoom, "Réorganiser" to place the tank, the box and the plots like on the real farm (saved for every screen). |
| **Zone** (`zone.html?zone=N`) | One plot in detail: status banner, the system diagram, the irrigation cycle, the last camera image, the plant, the irrigation plan, the sensors, the chart, the 7-day plan. |
| **Caméra** (`camera.html`) | The cameras sending images and, for each image, the 8 steps of the analysis, with the buttons to confirm a stage. |

What counts as a problem on the map: ESP32 offline, emergency stop, invalid or missing soil sensor,
water not delivered (blocked pipe, pressure), too much water, soil not getting wetter after irrigation,
leak (flow with all valves closed), stage to confirm or to check by digging (potato tubers), possible
pest (trained detector only), not enough data to decide.

## Tests

```powershell
python -m pytest -q
```
