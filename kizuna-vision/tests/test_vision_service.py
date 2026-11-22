"""Vision service tests: classic CV on synthetic images, pipeline with stub models, HTTP endpoint."""
import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from vision import classic
from vision.config import Settings
from vision.models import ModelUnavailable
from vision.pipeline import Analyzer

GREEN, SOIL, YELLOW = (40, 160, 50), (60, 90, 130), (0, 220, 240)   # BGR


def field_image(green_frac=0.5, size=(240, 320)):
    img = np.full((*size, 3), SOIL, np.uint8)
    img[:, : int(size[1] * green_frac)] = GREEN
    noise = np.random.default_rng(1).integers(-10, 10, img.shape)
    return np.clip(img.astype(int) + noise, 0, 255).astype(np.uint8)


def trap_image(n_insects):
    img = np.full((480, 640, 3), (200, 200, 200), np.uint8)
    cv2.rectangle(img, (80, 60), (560, 420), YELLOW, -1)
    rng = np.random.default_rng(3)
    for _ in range(n_insects):
        x, y = int(rng.integers(110, 530)), int(rng.integers(90, 390))
        cv2.ellipse(img, (x, y), (5, 3), float(rng.integers(0, 180)), 0, 360, (20, 20, 20), -1)
    return img


# ------------------------------------------------------------------ classic CV
@pytest.mark.parametrize("frac", [0.0, 0.25, 0.5, 0.9])
def test_canopy_cover_matches_green_fraction(frac):
    pct = classic.canopy_cover(field_image(frac))["canopy_cover_pct"]
    assert abs(pct - frac * 100) < 3


@pytest.mark.parametrize("n", [0, 7, 25])
def test_trap_insect_count(n):
    r = classic.count_trap_insects(trap_image(n))
    assert r["trap_found"] is True
    assert abs(r["insect_count"] - n) <= max(1, n // 10)   # touching insects may merge


def test_no_trap_in_image():
    assert classic.count_trap_insects(field_image(0.5))["trap_found"] is False


def test_motion_detection_and_brightness_change():
    a = field_image(0.5)
    assert classic.motion(None, a)["compared"] is False
    darker = np.clip(a.astype(int) - 40, 0, 255).astype(np.uint8)     # cloud passing
    assert classic.motion(a, darker)["intrusion"] is False
    b = a.copy()
    cv2.rectangle(b, (100, 80), (200, 180), (30, 30, 30), -1)          # an animal enters
    m = classic.motion(a, b)
    assert m["intrusion"] is True and m["boxes"]


# ------------------------------------------------------------------ pipeline with stub engines
class StubZeroShot:
    name = "stub-clip"

    def __init__(self, answers):
        self.answers = answers

    def classify(self, img, group):
        return self.answers[group]


class StubDetector:
    name = "stub-yolo"

    def __init__(self, found):
        self.found = found

    def detect(self, img, min_conf):
        return [f for f in self.found if f["confidence"] >= min_conf]


class Missing:
    name = "missing"

    def classify(self, *a):
        raise ModelUnavailable("open_clip_torch / torch not installed")

    detect = classify


ANSWERS = {
    "crop": [("tomato", 0.88), ("pepper", 0.09), ("other", 0.03)],
    "stage_tomato": [("flowers", 0.61), ("green_fruits", 0.3), ("leafy_no_flowers", 0.05), ("seedling", 0.04)],
    "stage_pepper": [("green_fruits", 0.7), ("flowers", 0.2), ("coloured_fruits", 0.1)],
    "stage_potato": [("flowering", 0.66), ("leafy_canopy", 0.3), ("senescence", 0.04)],
    "health": [("leaf_miner", 0.66), ("healthy", 0.2), ("yellowing", 0.14)],
    "weather": [("rain", 0.62), ("wet", 0.25), ("dry", 0.13)],
}


def test_pipeline_full_observation():
    a = Analyzer(Settings(), zero_shot=StubZeroShot(ANSWERS),
                 animal_detector=StubDetector([{"label": "bird", "confidence": 0.8}, {"label": "cat", "confidence": 0.2}]),
                 pest_detector=StubDetector([{"label": "tuta_absoluta_mine", "confidence": 0.71}]))
    o = a.analyze(field_image(0.4), source="carriage", zone_id=1, plant_ref="Z1-P04")
    assert o["crop"]["key"] == "tomato" and o["crop"]["kind"] == "ai_prediction"
    assert o["stage"] == {**o["stage"], "crop": "tomato", "key": "flowers", "kind": "ai_prediction"}
    assert o["stage"]["visual_cues"]                      # the prompts that matched, shown to the farmer
    # only the trained detector reports a pest; the zero-shot model gives an uncertain hint
    assert {p["label"] for p in o["pests"]} == {"tuta_absoluta_mine"}
    assert o["health"]["hint"] == "leaf_miner" and o["health"]["kind"] == "ai_hint_untrained"
    assert o["health"]["message_fr"].startswith("Diagnostic incertain — inspection nécessaire")
    assert [x["label"] for x in o["animals"]] == ["bird"]
    assert o["rain"]["raining_now"] is True and o["rain"]["source"] == "camera"
    assert 35 < o["canopy"]["canopy_cover_pct"] < 45 and o["canopy"]["kind"] == "measured"
    assert o["engines"]["unavailable"] == {}


def test_rain_sensor_overrides_camera():
    a = Analyzer(Settings(), zero_shot=StubZeroShot(ANSWERS), animal_detector=StubDetector([]))
    o = a.analyze(field_image(), source="mast", rain_sensor_wet=False)
    assert o["rain"] == {"raining_now": False, "confidence": 1.0, "source": "rain sensor", "kind": "measured"}


def test_healthy_plant_gives_no_pest():
    ans = dict(ANSWERS, health=[("healthy", 0.9), ("yellowing", 0.1)])
    o = Analyzer(Settings(), zero_shot=StubZeroShot(ans), animal_detector=StubDetector([])).analyze(field_image())
    assert o["pests"] == []


def test_missing_models_do_not_stop_classic_cv():
    a = Analyzer(Settings(), zero_shot=Missing(), animal_detector=Missing())
    o = a.analyze(field_image(0.5), source="mast")
    assert o["crop"] is None and o["rain"]["raining_now"] is None
    assert "crop" in o["engines"]["unavailable"] and "animals" in o["engines"]["unavailable"]
    assert 45 < o["canopy"]["canopy_cover_pct"] < 55


def test_trap_source_only_counts_insects():
    o = Analyzer(Settings(), zero_shot=StubZeroShot(ANSWERS), animal_detector=StubDetector([])).analyze(
        trap_image(12), source="trap", zone_id=1)
    assert o["trap"]["insect_count"] in range(11, 14) and "crop" not in o


def test_trained_crop_classifier_parsing():
    class Cls:
        name = "yolo-cls"

        def classify(self, img):
            return [("pepper-mid_season", 0.5), ("pepper-development", 0.3), ("tomato-mid_season", 0.2)]
    o = Analyzer(Settings(use_clip=False, animal_model=""), crop_classifier=Cls()).analyze(field_image())
    assert o["crop"]["key"] == "pepper" and abs(o["crop"]["confidence"] - 0.8) < 1e-6
    assert o["stage"]["key"] == "mid_season" and abs(o["stage"]["confidence"] - 0.625) < 1e-6
    assert o["stage"]["crop"] == "pepper"


# ------------------------------------------------------------------ HTTP service
def test_analyze_endpoint(monkeypatch, tmp_path):
    from vision import service
    monkeypatch.setattr(service, "analyzer", Analyzer(Settings(), zero_shot=StubZeroShot(ANSWERS),
                                                      animal_detector=StubDetector([])))
    monkeypatch.setattr(service.settings, "image_dir", str(tmp_path))
    sent = []
    monkeypatch.setattr(service, "post_to_backend", lambda obs: sent.append(obs) or {"alerts": []})
    c = TestClient(service.app)
    jpg = cv2.imencode(".jpg", field_image())[1].tobytes()
    r = c.post("/analyze", files={"image": ("a.jpg", jpg, "image/jpeg")}, data={"zone_id": "1", "source": "phone"})
    assert r.status_code == 200 and r.json()["observation"]["crop"]["key"] == "tomato"
    assert sent and list(tmp_path.iterdir())
    assert c.post("/analyze", files={"image": ("a.txt", b"nope", "text/plain")}).status_code == 422
    assert c.post("/analyze", files={"image": ("a.jpg", jpg, "image/jpeg")}, data={"source": "drone"}).status_code == 422


@pytest.mark.parametrize("ranking,expected", [
    ([("potato", 0.85), ("pepper", 0.10), ("tomato", 0.05)], "potato"),
    ([("pepper", 0.48), ("potato", 0.42), ("tomato", 0.10)], "unknown"),   # too close: abstain
    ([("pepper", 0.40), ("other", 0.20), ("tomato", 0.20)], "unknown"),    # too low: abstain
    ([("other", 0.45), ("tomato", 0.40), ("pepper", 0.15)], "other"),
])
def test_crop_abstains_when_unsure(ranking, expected):
    ans = dict(ANSWERS, crop=ranking)
    o = Analyzer(Settings(), zero_shot=StubZeroShot(ans), animal_detector=StubDetector([])).analyze(field_image())
    assert o["crop"]["key"] == expected
    if expected == "unknown":
        assert o["crop"]["best_guess"] == ranking[0][0] and o["stage"] is None


def test_env_file_inline_comments(tmp_path):
    from vision.config import load_env_file
    f = tmp_path / ".env"
    f.write_text("# header\nDEVICE_API_KEY=change-me          # same value as in the backend .env\n"
                 "PEST_MODEL=                       # models/pest.pt after training\n"
                 "BACKEND_URL=http://localhost:8000\n\n", encoding="utf-8")
    v = load_env_file(f)
    assert v == {"DEVICE_API_KEY": "change-me", "PEST_MODEL": "", "BACKEND_URL": "http://localhost:8000"}



@pytest.mark.parametrize("crop,expected", [("pepper", "green_fruits"), ("potato", "flowering")])
def test_stage_classes_are_specific_to_the_recognised_crop(crop, expected):
    ans = dict(ANSWERS, crop=[(crop, 0.9), ("other", 0.1)])
    o = Analyzer(Settings(), zero_shot=StubZeroShot(ans), animal_detector=StubDetector([])).analyze(field_image())
    assert o["stage"]["crop"] == crop and o["stage"]["key"] == expected


def test_every_crop_has_its_own_stage_prompts():
    from vision.models import LABELS
    for crop in ("pepper", "potato", "tomato", "eggplant"):
        assert f"stage_{crop}" in LABELS
    assert set(LABELS["stage_potato"]) == {"emergence", "leafy_canopy", "flowering", "senescence"}
    assert "stage" not in LABELS                           # no generic stage set any more


def test_farmer_lines_show_stage_next_stage_and_irrigation():
    from vision.capture import farmer_lines
    backend = {
        "stage": {"available": True, "stage": {"name_en": "Flowering"}, "stage_start_date": "2026-11-20",
                  "certainty": {"level": "medium"}, "next_stage": {"name_en": "Fruit set and fruit development"},
                  "next_stage_date": "2026-12-05", "days_to_next_stage": 13, "reasons": []},
        "irrigation": {"frequency": {"text_en": "every 2 days"}, "irrigations_per_week": 3.0,
                       "volume_per_irrigation_l": 42.0, "duration_per_irrigation_min": 10.5, "weekly_water_l": 126.0,
                       "next_irrigation": {"when_en": "tomorrow at 05:00", "volume_l": 42.0},
                       "status_text_en": "Waiting: soil water still sufficient", "missing": []}}
    text = "\n".join(farmer_lines(backend))
    assert "Next stage: Fruit set and fruit development around 2026-12-05" in text
    assert "every 2 days" in text and "tomorrow at 05:00" in text and "42.0 L" in text


def test_untrained_model_never_reports_a_pest_however_high_its_score():
    ans = dict(ANSWERS, health=[("leaf_miner", 0.99), ("healthy", 0.01)])
    o = Analyzer(Settings(), zero_shot=StubZeroShot(ans), animal_detector=StubDetector([])).analyze(field_image(),
                                                                                                    source="phone")
    assert o["pests"] == [] and "inspection" in o["health"]["message_en"]


def test_saved_images_are_served_by_name_only(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from vision import service
    monkeypatch.setattr(service.settings, "image_dir", str(tmp_path))
    (tmp_path / "20261122T080000000000_z2_phone.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    c = TestClient(service.app)
    r = c.get("/images/20261122T080000000000_z2_phone.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert c.get("/images/..%2F..%2Fetc%2Fpasswd").status_code == 404
    assert c.get("/images/missing.jpg").status_code == 404
    assert c.get("/images/config.py").status_code == 404


def test_analyzer_page_is_served_in_the_dashboard_style():
    from fastapi.testclient import TestClient
    from vision import service
    r = TestClient(service.app).get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "Ce que l'analyseur sait faire" in r.text and "hanko" in r.text and 'fetch("analyze"' in r.text
