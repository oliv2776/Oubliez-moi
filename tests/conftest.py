import os
import sys
import tempfile
from pathlib import Path

# La configuration est lue à l'import : on la fixe avant d'importer l'application.
_tmp = tempfile.mkdtemp(prefix="oubli-test-")
os.environ.update(
    DATA_DIR=_tmp,
    ADMIN_PASSWORD="test-password",
    DISABLE_SCHEDULER="true",
    DRY_RUN="true",
    SMTP_FROM="moi@exemple.fr",
    BROKER_AUTO_UPDATE_DAYS="0",
)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        r = c.post("/login", data={"username": "admin", "password": "test-password"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
        yield c
