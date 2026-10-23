"""Starting the app must hand a single student a way in.

Found by running it: ``start-campus.ps1`` without a ``.env`` produced a server
where login answered 503 LOGIN_DELIVERY_UNAVAILABLE — no dev code, no SMTP. The
process started fine, so nothing looked broken until someone tried to log in,
which is the first thing a student does.

The single-user local profile has to set four things: development env, dev auth
on, a persistent data directory, and an operator mailbox to type into the login
box. This pins both halves — the launcher sets them, and the settings they
produce actually allow a local login (the server only ever binds 127.0.0.1).
"""

import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[3]
LAUNCHER = ROOT / "start-campus.ps1"


def test_the_launcher_sets_the_single_user_defaults():
    text = LAUNCHER.read_text(encoding="utf-8")
    for variable, value in (
        ("CAMPUS_ENV", "development"),
        ("CAMPUS_DEV_AUTH", "true"),
        ("CAMPUS_DATA_DIR", "data-operator"),
    ):
        pattern = rf"-not \$env:{variable}\).*\$\{{?env:{variable}"
        assert re.search(pattern, text), f"{variable} default missing from the launcher"
        assert value in text
    assert "CAMPUS_ADMIN_EMAILS" in text, "the login mailbox must be shown to the user"
    # A guard against accidentally starting a production database in local mode.
    assert "production" in text


def test_the_defaults_respect_an_explicit_environment():
    """A .env that already answers must win; the launcher only fills gaps."""
    text = LAUNCHER.read_text(encoding="utf-8")
    guarded = re.findall(r"if \(-not \$env:(CAMPUS_[A-Z_]+)\)", text)
    assert {"CAMPUS_ENV", "CAMPUS_DEV_AUTH", "CAMPUS_DATA_DIR"} <= set(guarded)


@pytest.fixture
def local_profile(tmp_path):
    env = {
        "CAMPUS_ENV": "development",
        "CAMPUS_DEV_AUTH": "true",
        "CAMPUS_DATA_DIR": str(tmp_path),
        "CAMPUS_ADMIN_EMAILS": "operator@tju.edu.cn",
        "CAMPUS_DATABASE_URL": "",
        "CAMPUS_MEILI_URL": "",
        "CAMPUS_DEMO": "false",
    }
    with patch.dict(os.environ, env, clear=False):
        from server.campus_app import create_app
        from server.campus.platform import Settings

        settings = Settings.from_env()
        assert settings.dev_auth is True
        assert settings.production is False
        app = create_app(settings, start_worker=False)
        client = TestClient(app)
        yield client
        client.close()
        app.state.core.store.close()


def test_the_local_profile_can_actually_log_in(local_profile):
    response = local_profile.post("/api/v1/auth/challenges", json={"email": "operator@tju.edu.cn"})
    assert response.status_code == 201, response.text
    code = response.headers.get("X-Dev-Login-Code")
    assert code, "the page needs the development code, or the student cannot log in"
    session = local_profile.post(
        "/api/v1/auth/sessions",
        json={"challenge_id": response.json()["challenge_id"], "code": code},
    )
    assert session.status_code == 200, session.text
    assert session.json()["user"]["email"] == "operator@tju.edu.cn"