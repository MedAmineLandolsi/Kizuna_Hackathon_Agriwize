"""One image in, one observation out: what the backend receives from the vision service.

Every result says which engine produced it, and AI results carry a confidence.
Water quantities are never decided here: the backend's FAO-56 engine does that.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import numpy as np

from . import classic
from .config import Settings, settings as default_settings
from .models import ANIMAL_CLASSES, LABELS, ModelUnavailable, YoloClassifier, YoloDetector, ZeroShot

log = logging.getLogger(__name__)
SOURCES = ("mast", "carriage", "phone", "trap")
CROP_LABELS = {"tomato": "Tomato", "pepper": "Sweet pepper (bell)", "potato": "Potato", "eggplant": "Eggplant",
               "other": "Not a crop", "unknown": "Not sure - farmer to confirm"}


class Analyzer:
    def __init__(self, cfg: Settings = default_settings, zero_shot=None, animal_detector=None,
                 pest_detector=None, crop_classifier=None):
        self.cfg = cfg
        self.zero_shot = zero_shot if zero_shot is not None else (
            ZeroShot(cfg.clip_model, cfg.clip_pretrained) if cfg.use_clip else None)
        self.animals = animal_detector if animal_detector is not None else (
            YoloDetector(cfg.animal_model, keep=ANIMAL_CLASSES) if cfg.animal_model else None)
        self.pests = pest_detector if pest_detector is not None else (
            YoloDetector(cfg.pest_model) if cfg.pest_model else None)
        self.crop_cls = crop_classifier if crop_classifier is not None else (
            YoloClassifier(cfg.crop_model) if cfg.crop_model else None)
        self.unavailable: dict[str, str] = {}

    # -------------------------------------------------------------- helpers
    def _safe(self, key: str, fn):
        try:
            return fn()
        except ModelUnavailable as exc:
            self.unavailable[key] = str(exc)
        except Exception as exc:  # a model failure must never stop the other analyses
            log.exception("%s failed", key)
            self.unavailable[key] = f"error: {exc}"
        return None

    def engines_status(self) -> dict:
        return {
            "crop": (self.crop_cls.name if self.crop_cls else None) or (self.zero_shot.name if self.zero_shot else None),
            "health": self.zero_shot.name if self.zero_shot else None,
            "pests": self.pests.name if self.pests else None,
            "animals": self.animals.name if self.animals else None,
            "weather": self.zero_shot.name if self.zero_shot else None,
            "canopy": "classic CV", "trap": "classic CV", "motion": "classic CV",
            "unavailable": dict(self.unavailable),
        }

    def _abstain(self, crop: dict) -> dict:
        """Refuse to name a crop when the model is not clearly sure (low score or two close answers)."""
        alts = crop.get("alternatives") or []
        second = alts[0][1] if alts else 0.0
        if crop["key"] != "other" and (crop["confidence"] < self.cfg.crop_min_conf
                                       or crop["confidence"] - second < self.cfg.crop_min_margin):
            return {**crop, "key": "unknown", "label": CROP_LABELS["unknown"], "best_guess": crop["key"],
                    "reason": f"top {crop['key']} {crop['confidence']:.2f} vs "
                              f"{alts[0][0] if alts else '-'} {second:.2f}"}
        return crop

    def _crop_and_stage(self, img: np.ndarray) -> tuple[dict | None, dict | None]:
        if self.crop_cls:
            ranked = self._safe("crop", lambda: self.crop_cls.classify(img))
            if ranked:  # class names "<crop>-<stage>", e.g. tomato-mid_season
                crops: dict[str, float] = {}
                for name, p in ranked:
                    c = name.split("-", 1)[0]
                    crops[c] = crops.get(c, 0.0) + p
                best_crop = max(crops, key=crops.get)
                stages = [(n.split("-", 1)[1], p) for n, p in ranked if n.startswith(best_crop + "-") and "-" in n]
                stage = None
                if stages:
                    tot = sum(p for _, p in stages) or 1.0
                    k, p = max(stages, key=lambda kv: kv[1])
                    stage = {"crop": best_crop, "key": k, "confidence": round(p / tot, 3), "kind": "ai_prediction",
                             "visual_cues": [], "engine": self.crop_cls.name}
                alts = sorted(((k, round(v, 3)) for k, v in crops.items() if k != best_crop), key=lambda kv: -kv[1])[:3]
                crop = self._abstain({"key": best_crop, "label": CROP_LABELS.get(best_crop, best_crop),
                                      "confidence": round(crops[best_crop], 3), "kind": "ai_prediction",
                                      "alternatives": alts})
                return crop, (stage if crop["key"] not in ("unknown", "other") else None)
        if not self.zero_shot:
            return None, None
        ranked = self._safe("crop", lambda: self.zero_shot.classify(img, "crop"))
        if not ranked:
            return None, None
        crop = self._abstain({"key": ranked[0][0], "label": CROP_LABELS.get(ranked[0][0], ranked[0][0]),
                              "confidence": round(ranked[0][1], 3), "kind": "ai_prediction",
                              "alternatives": [(k, round(p, 3)) for k, p in ranked[1:3]]})
        stage = None
        group = f"stage_{crop['key']}"
        if crop["key"] not in ("other", "unknown") and group in LABELS:
            s = self._safe("stage", lambda: self.zero_shot.classify(img, group))
            if s:
                stage = {"crop": crop["key"], "key": s[0][0], "confidence": round(s[0][1], 3),
                         "kind": "ai_prediction", "visual_cues": LABELS[group][s[0][0]],
                         "alternatives": [(k, round(p, 3)) for k, p in s[1:3]], "engine": self.zero_shot.name}
        return crop, stage

    # -------------------------------------------------------------- main entry
    def analyze(self, img: np.ndarray, *, source: str = "mast", zone_id: int | None = None,
                plant_ref: str | None = None, image_ref: str | None = None,
                prev_frame: np.ndarray | None = None, rain_sensor_wet: bool | None = None) -> dict:
        if source not in SOURCES:
            raise ValueError(f"source must be one of {SOURCES}")
        self.unavailable = {}
        obs: dict = {"zone_id": zone_id, "plant_ref": plant_ref, "source": source, "image_ref": image_ref,
                     "analyzed_at": datetime.now(timezone.utc).isoformat(), "pests": [], "animals": []}

        if source == "trap":
            obs["trap"] = classic.count_trap_insects(img)
            obs["engines"] = self.engines_status()
            return obs

        obs["canopy"] = classic.canopy_cover(img)
        obs["crop"], obs["stage"] = self._crop_and_stage(img)

        # leaf health / pests.
        # Only a TRAINED detector (PEST_MODEL) reports pests. The zero-shot model is not trained or
        # validated for pests: its score only ranks 5 text descriptions against each other (it is
        # always high for one of them), so it gives an uncertain hint, never a detection or an alert.
        if self.pests:
            found = self._safe("pests", lambda: self.pests.detect(img, self.cfg.detect_min_conf))
            obs["pests"].extend(found or [])
        if self.zero_shot and source in ("carriage", "phone", "mast"):
            h = self._safe("health", lambda: self.zero_shot.classify(img, "health"))
            if h:
                top, p = h[0]
                obs["health"] = {
                    "ranking": [(k, round(v, 3)) for k, v in h], "kind": "ai_hint_untrained",
                    "engine": self.zero_shot.name, "hint": top,
                    "message_en": ("No visible problem suggested (untrained model, not a guarantee)." if top == "healthy"
                                   else f"Uncertain diagnosis — inspection needed (untrained model suggests: "
                                        f"{top.replace('_', ' ')})."),
                    "message_fr": ("Aucun problème visible suggéré (modèle non entraîné, pas une garantie)."
                                   if top == "healthy" else
                                   f"Diagnostic incertain — inspection nécessaire (le modèle non entraîné suggère : "
                                   f"{top.replace('_', ' ')})."),
                    "note": "relative score between 5 descriptions, not a probability of infestation",
                }

        if self.animals:
            obs["animals"] = self._safe("animals", lambda: self.animals.detect(img, self.cfg.detect_min_conf)) or []
        obs["motion"] = classic.motion(prev_frame, img, self.cfg.motion_min_changed_pct)

        # rain now: the sensor is a measurement, the camera is a prediction
        rain = {"raining_now": None, "confidence": None, "source": None}
        if rain_sensor_wet is not None:
            rain = {"raining_now": bool(rain_sensor_wet), "confidence": 1.0, "source": "rain sensor", "kind": "measured"}
        elif self.zero_shot and source in ("mast", "carriage"):
            w = self._safe("weather", lambda: self.zero_shot.classify(img, "weather"))
            if w:
                probs = dict(w)
                p_rain = probs.get("rain", 0.0) + probs.get("wet", 0.0)
                rain = {"raining_now": p_rain >= self.cfg.rain_min_conf, "confidence": round(max(p_rain, 1 - p_rain), 3),
                        "source": "camera", "kind": "ai_prediction",
                        "ranking": [(k, round(v, 3)) for k, v in w]}
        obs["rain"] = rain
        obs["engines"] = self.engines_status()
        return obs


__all__ = ["Analyzer", "SOURCES", "LABELS"]
