# KIZUNA – Vision service (part 3 of 4)

One service for every camera option of the package. It tells the backend **what the
camera sees**; the backend's FAO-56 engine decides the water. The AI never sets litres.

| Package option | How images arrive | Best for |
|---|---|---|
| Fixed mast camera | `python -m vision.capture --camera 0 --source mast` (USB/IP camera, ESP32-CAM stream) | bed-level crop & stage, animals, rain |
| Rail carriage (per plant) | carriage camera posts each stop with `--source carriage --plant Z1-P04` | per-plant stage, close-up pests |
| Farmer's phone | open `http://<server>:8100/` on the phone, take a photo | cheapest option, close-ups |
| Sticky trap camera | `--source trap` | insect counts (*Tuta absoluta*, whiteflies) |

## What it detects

| Function | Engine (prototype) | Output kind | Becomes in the backend |
|---|---|---|---|
| Crop identification (tomato, pepper, potato, eggplant, other) | CLIP zero-shot, or your trained classifier (`CROP_MODEL`) | AI prediction + confidence; **"unknown"** when the best score is under 0.50 or less than 0.15 ahead of the second | crop identification; below 0.80, unknown, or disagreeing with the registered crop → the farmer confirms. A confirmed zone keeps irrigating with its crop meanwhile |
| Growth stage | same | AI prediction | shown next to the calendar stage |
| Leaf cover (how much ground the plant covers) | Excess Green index (classic image processing) | measured | stored per zone / plant |
| Pests and damage | your trained YOLO model (`PEST_MODEL`) + CLIP screening | AI prediction | `pest:*` alert to the farmer |
| Animals | YOLO COCO (birds, cats, dogs, livestock) | AI prediction | `animal:*` alert |
| Night intruders | motion detection between photos | measured | `motion` alert |
| Insects on sticky trap | yellow-trap segmentation + insect count | measured | `insect_trap` alert when new insects ≥ 10 |
| Rain now | rain sensor if present, otherwise CLIP on the image | measured / AI | irrigation postponed while raining |
| Rain tomorrow | Open-Meteo forecast (backend) | forecast | `rain_forecast` notification |

Crop-health alerts never block irrigation; rain does (it postpones it).

## Run
```bash
cd kizuna-vision
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # same DEVICE_API_KEY as the backend
uvicorn vision.service:app --host 0.0.0.0 --port 8100
```
- API docs: http://localhost:8100/docs · phone page: http://localhost:8100/
- One photo: `python -m vision.capture --image plant.jpg --zone 1 --source phone`
- Webcam every hour: `python -m vision.capture --camera 0 --zone 1 --source mast --every 60`
- Tests: `pytest -q` (25 tests; the end-to-end test uses `../kizuna-backend` if present)

The first run downloads the CLIP weights (~600 MB) and `yolov8n.pt` (~6 MB).
Without PyTorch installed, the service still runs the classic functions (leaf cover,
traps, motion, rain sensor) and lists the missing engines in `/health`.

## Train your own models
`training/train_models.ipynb` (Google Colab, free GPU): pest detector from a public
*Tuta absoluta* dataset and/or your photos, and a crop + stage classifier from your field
photos. `training/PHOTO_GUIDE.md` explains what to photograph and how to label.

## Honest limits of the prototype
- **CLIP zero-shot is a starting point, not a validated model.** Its accuracy on your
  crops is unknown until you measure it on your own labelled photos; low confidence goes
  to the farmer for confirmation, and the trained classifier should replace it.
- **A mast camera cannot see small insects** (whiteflies, aphids, *Tuta* larvae): use
  the sticky trap, the carriage or phone close-ups for pests.
- **The camera does not predict rain**: tomorrow's rain comes from the forecast; the
  camera only confirms rain that is falling now, and a cheap rain sensor is more reliable.
- **Rodents and wild boar are not in the COCO model**: train them into `PEST_MODEL` if
  they matter on your farms; meanwhile the motion alert covers night intrusions.
- Thresholds (alert confidences, trap count) are operating choices; set them with an
  agronomist / CRDA.
