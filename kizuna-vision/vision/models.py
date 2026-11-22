"""AI engines, loaded lazily so the service starts even if a model is missing.

- ZeroShot (CLIP): prototype engine for crop, growth stage, leaf health and weather,
  usable on day one without training data. Replace with trained models when you have photos.
- YoloDetector: COCO model for animals (birds, cats, dogs, livestock) and your own
  trained pest/damage model (e.g. Tuta absoluta).
- YoloClassifier: your own trained crop/stage classifier (replaces CLIP for the crop).
"""
from __future__ import annotations

import logging

import cv2
import numpy as np

log = logging.getLogger(__name__)


class ModelUnavailable(RuntimeError):
    pass


# Prompt sets. Keys are what the backend understands (crop keys = backend crop profiles,
# stage keys = FAO-56 stage names).
LABELS: dict[str, dict[str, list[str]]] = {
    # Several prompts per crop, describing what tells them apart (leaf shape, flowers, fruit):
    # potato vs pepper vs tomato vs eggplant are all Solanaceae and look alike from far.
    "crop": {
        "tomato": ["a photo of a tomato plant", "a tomato plant with hairy, deeply serrated compound leaves",
                   "a tomato plant with small yellow star-shaped flowers or red round fruits", "a staked tomato plant"],
        "pepper": ["a photo of a bell pepper plant", "a pepper plant with smooth, glossy, simple oval pointed leaves",
                   "a pepper plant with small white flowers or green bell peppers", "a chili pepper plant"],
        "potato": ["a photo of a potato plant", "potato plants growing in ridged rows of soil",
                   "a potato plant with dark green compound leaves of oval leaflets",
                   "a potato plant with white or purple flowers"],
        "eggplant": ["a photo of an eggplant plant", "an aubergine plant with large fuzzy lobed grey-green leaves",
                     "an eggplant plant with purple flowers or dark purple fruits"],
        "other": ["a photo of weeds", "a photo of bare soil", "a photo of a tree", "a photo of grass",
                  "a photo of irrigation pipes", "a photo of a person"],
    },
    # Growth stage: one prompt set PER CROP. Keys are the "visual classes" of that crop's cycle in the
    # backend (app/agronomy/crop_cycles.py), which maps them to agronomic stages. Potato tuber stages are
    # underground: the camera can only see canopy, flowering or senescence.
    "stage_pepper": {
        "seedling": ["a small pepper seedling with a few leaves", "a young pepper transplant"],
        "leafy_no_flowers": ["a leafy pepper plant without flowers", "a bushy green pepper plant with no fruit"],
        "flowers": ["a pepper plant with small white flowers", "pepper flower buds and open white flowers"],
        "green_fruits": ["a pepper plant with green bell peppers", "small green pepper fruits on the plant"],
        "coloured_fruits": ["a pepper plant with red ripe bell peppers", "yellow or orange ripe peppers on the plant"],
    },
    "stage_potato": {
        "emergence": ["potato shoots emerging from the soil", "young potato sprouts breaking the ground"],
        "leafy_canopy": ["a leafy green potato plant without flowers", "dense green potato canopy covering the rows"],
        "flowering": ["a potato plant with white or purple flowers", "flowering potato plants in a field"],
        "senescence": ["potato plants with yellowing dying leaves", "dry brown potato vines at the end of the season"],
    },
    "stage_tomato": {
        "seedling": ["a small tomato seedling", "a young tomato transplant"],
        "leafy_no_flowers": ["a leafy tomato plant without flowers"],
        "flowers": ["a tomato plant with yellow star-shaped flowers"],
        "green_fruits": ["a tomato plant with green unripe tomatoes"],
        "coloured_fruits": ["a tomato plant with red ripe tomatoes"],
    },
    "stage_eggplant": {
        "seedling": ["a small eggplant seedling"],
        "leafy_no_flowers": ["a leafy eggplant plant without flowers"],
        "flowers": ["an eggplant plant with purple flowers"],
        "green_fruits": ["an eggplant plant with small young fruits"],
        "coloured_fruits": ["an eggplant plant with large glossy dark purple fruits"],
    },
    "health": {
        "healthy": ["healthy green plant leaves"],
        "leaf_miner": ["leaves with leaf miner tunnels and blotch mines", "tomato leaves damaged by Tuta absoluta"],
        "insects": ["leaves covered with small insects such as aphids or whiteflies"],
        "yellowing": ["yellowing leaves"],
        "leaf_spots": ["leaves with brown spots and blight"],
    },
    "weather": {
        "rain": ["a photo taken in the rain with falling raindrops", "raindrops on the camera lens"],
        "wet": ["wet muddy soil with puddles after rain"],
        "dry": ["dry soil in sunny weather", "a clear sunny day in a field"],
    },
}

ANIMAL_CLASSES = {"bird", "cat", "dog", "horse", "sheep", "cow", "bear", "elephant", "zebra", "giraffe"}


class ZeroShot:
    name = "clip-zero-shot"

    def __init__(self, model: str, pretrained: str):
        self.model_name, self.pretrained = model, pretrained
        self._model = None

    def _load(self):
        if self._model is not None:
            return
        try:
            import open_clip
            import torch
        except ImportError as exc:
            raise ModelUnavailable("open_clip_torch / torch not installed") from exc
        model, _, preprocess = open_clip.create_model_and_transforms(self.model_name, pretrained=self.pretrained)
        model.eval()
        tok = open_clip.get_tokenizer(self.model_name)
        self._torch, self._model, self._prep, self._tok = torch, model, preprocess, tok
        self._text_cache: dict[str, tuple[list[str], object]] = {}
        self.name = f"clip-zero-shot {self.model_name}/{self.pretrained}"

    def _text(self, group: str):
        """One embedding per label: the average of its prompts (prompt ensembling), so a label
        with more prompts does not get more probability than one with fewer."""
        if group not in self._text_cache:
            keys, embs = [], []
            with self._torch.no_grad():
                for k, prompts in LABELS[group].items():
                    t = self._model.encode_text(self._tok(prompts))
                    t = t / t.norm(dim=-1, keepdim=True)
                    m = t.mean(dim=0)
                    embs.append(m / m.norm())
                    keys.append(k)
            self._text_cache[group] = (keys, self._torch.stack(embs))
        return self._text_cache[group]

    def classify(self, img_bgr: np.ndarray, group: str) -> list[tuple[str, float]]:
        """Probability per label, best first."""
        self._load()
        from PIL import Image
        image = self._prep(Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))).unsqueeze(0)
        keys, text = self._text(group)
        with self._torch.no_grad():
            f = self._model.encode_image(image)
            f = f / f.norm(dim=-1, keepdim=True)
            probs = (100.0 * f @ text.T).softmax(dim=-1)[0].tolist()
        return sorted(zip(keys, probs), key=lambda kv: kv[1], reverse=True)


class YoloDetector:
    def __init__(self, path: str, keep: set[str] | None = None):
        self.path, self.keep = path, keep
        self._model = None
        self.name = f"yolo {path}"

    def _load(self):
        if self._model is not None:
            return
        if not self.path:
            raise ModelUnavailable("no model path configured")
        try:
            from ultralytics import YOLO
        except ImportError as exc:
            raise ModelUnavailable("ultralytics not installed") from exc
        self._model = YOLO(self.path)

    def detect(self, img_bgr: np.ndarray, min_conf: float) -> list[dict]:
        self._load()
        res = self._model.predict(img_bgr, conf=min_conf, verbose=False)[0]
        out = []
        for box in res.boxes:
            label = res.names[int(box.cls)]
            if self.keep is not None and label not in self.keep:
                continue
            out.append({"label": label, "confidence": round(float(box.conf), 3),
                        "box": [round(v) for v in box.xyxy[0].tolist()], "engine": self.name})
        return out


class YoloClassifier(YoloDetector):
    def classify(self, img_bgr: np.ndarray) -> list[tuple[str, float]]:
        self._load()
        res = self._model.predict(img_bgr, verbose=False)[0]
        probs = res.probs.data.tolist()
        return sorted(((res.names[i], p) for i, p in enumerate(probs)), key=lambda kv: kv[1], reverse=True)
