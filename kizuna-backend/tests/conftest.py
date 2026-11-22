import os
import tempfile

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["DEVICE_API_KEY"] = "test-key"
os.environ["IRRIGATION_WINDOWS"] = "00:00-23:59"

import pytest  # noqa: E402
from sqlmodel import SQLModel  # noqa: E402


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.db import engine, init_db
    from app.main import app

    SQLModel.metadata.drop_all(engine)
    init_db(seed_demo=True)
    with TestClient(app) as c:
        yield c
