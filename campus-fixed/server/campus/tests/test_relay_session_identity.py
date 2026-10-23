"""A persisted relay session belongs to one operator identity.

``relay.py`` caches the login cookie in ``deploy/operator/session.json`` so a
daily cycle does not spend a development login code every time. ``_restore()``
checked only the API base URL, so a cookie saved for one email was reused for a
different ``--email``. The product then authenticated as the *old* account and
refused the write with 403 — which looks like a permissions bug, not a stale
credential, and costs an hour of tracing.

A cached session is only valid for the identity it was created for.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from deploy.operator import relay  # noqa: E402


class FakeResponse:
    def __init__(self, payload, cookie=None):
        self._body = json.dumps(payload).encode()
        self.headers = {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _write_session(tmp_path, monkeypatch, *, api, email, cookie):
    session_file = tmp_path / "session.json"
    session_file.write_text(
        json.dumps({"api": api, "email": email, "cookie": cookie}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(relay, "SESSION_FILE", session_file)
    return session_file


def test_a_session_saved_for_another_email_is_not_reused(tmp_path, monkeypatch):
    _write_session(
        tmp_path,
        monkeypatch,
        api="http://127.0.0.1:8000",
        email="someone-else@touxian.local",
        cookie="touxian_session=abc",
    )
    called = {"network": False}

    def explode(*args, **kwargs):  # pragma: no cover - must not be reached
        called["network"] = True
        raise AssertionError("a stale identity must not be validated over the network")

    monkeypatch.setattr(relay.AdminSession, "_request", explode)
    session = relay.AdminSession("http://127.0.0.1:8000", "operator@tju.edu.cn")
    assert session._restore() is False
    assert called["network"] is False
    assert session.cookies == ""


def test_a_session_saved_for_the_same_email_is_reused(tmp_path, monkeypatch):
    _write_session(
        tmp_path,
        monkeypatch,
        api="http://127.0.0.1:8000",
        email="operator@tju.edu.cn",
        cookie="touxian_session=abc",
    )

    def fake_request(self, method, path, body=None, headers=None):
        assert path == "/api/v1/me"
        return {"user": {"email": "operator@tju.edu.cn"}, "csrf_token": "csrf-token"}, None

    monkeypatch.setattr(relay.AdminSession, "_request", fake_request)
    session = relay.AdminSession("http://127.0.0.1:8000", "operator@tju.edu.cn")
    assert session._restore() is True
    assert session.csrf == "csrf-token"


def test_a_session_for_another_api_is_not_reused(tmp_path, monkeypatch):
    _write_session(
        tmp_path,
        monkeypatch,
        api="http://127.0.0.1:9999",
        email="operator@tju.edu.cn",
        cookie="touxian_session=abc",
    )
    session = relay.AdminSession("http://127.0.0.1:8000", "operator@tju.edu.cn")
    assert session._restore() is False
