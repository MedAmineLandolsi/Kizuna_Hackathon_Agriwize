"""Vision service -> real backend (in-process). Skipped if the backend folder is not next to this one."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2] / "kizuna-backend"
pytestmark = pytest.mark.skipif(not BACKEND.exists(), reason="kizuna-backend not found next to kizuna-vision")


@pytest.fixture()
def backend():
    os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.NamedTemporaryFile(suffix='.db', delete=False).name}"
    os.environ["SCHEDULER_ENABLED"] = "false"
    os.environ["DEVICE_API_KEY"] = "e2e-key"
    os.environ["IRRIGATION_WINDOWS"] = "00:00-23:59"
    sys.path.insert(0, str(BACKEND))
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        yield c
    sys.path.remove(str(BACKEND))


def test_vision_results_reach_backend(backend, monkeypatch):
    from test_vision_service import ANSWERS, StubDetector, StubZeroShot, field_image, trap_image

    from vision import service
    from vision.config import Settings
    from vision.pipeline import Analyzer

    monkeypatch.setattr(service.settings, "backend_url", "http://testserver")
    monkeypatch.setattr(service.settings, "device_key", "e2e-key")
    a = Analyzer(Settings(), zero_shot=StubZeroShot(ANSWERS),
                 animal_detector=StubDetector([{"label": "bird", "confidence": 0.9}]))

    obs = a.analyze(field_image(0.4), source="carriage", zone_id=1, plant_ref="Z1-P04")
    res = service.post_to_backend(obs, client=backend)
    assert res["identification"]["status"] == "confirmed"          # tomato 0.88 >= 0.80, same as zone 1
    assert {x["kind"] for x in res["alerts"]} == {"animal:bird"}      # zero-shot health = hint, not an alert
    assert res["health_hint"]["message_fr"].startswith("Diagnostic incertain")

    stored = backend.get("/api/vision/observations?zone_id=1").json()[0]
    assert stored["plant_ref"] == "Z1-P04" and stored["growth_stage"] == "flowers"
    # the photo became stage evidence for plant Z1-P04 of the tomato zone
    assert res["stage_evidence_id"] is not None and res["stage"]["crop_key"] == "tomato"
    # the answer to a photo carries the irrigation plan (or says what is missing for it)
    assert res["irrigation"] is not None
    from vision.capture import farmer_lines
    text = "\n".join(farmer_lines(res))
    assert "Stage:" in text and "Next stage:" in text
    assert stored["raining_now"] is True                            # rain 0.62 + wet 0.25 = 0.87

    # camera sees rain with high confidence -> the next irrigation decision is postponed
    backend.post("/api/controller/readings", headers={"X-Device-Key": "e2e-key"},
                 json={"controller": {"air_temp_c": 22}, "zones": [{"valve_channel": 0, "soil_raw": 2514}]})
    backend.post("/api/controller/readings", headers={"X-Device-Key": "e2e-key"},
                 json={"controller": {"air_temp_c": 33}, "zones": [{"valve_channel": 0, "soil_raw": 2514}]})
    assert backend.post("/api/zones/1/decide").json()["action"] == "postpone_rain"

    t = service.post_to_backend(a.analyze(trap_image(15), source="trap", zone_id=1), client=backend)
    assert t["alerts"][0]["kind"] == "insect_trap"
