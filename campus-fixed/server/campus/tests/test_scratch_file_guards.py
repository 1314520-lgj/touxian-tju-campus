"""A live session cookie must never reach the release tree.

``ignored_dir_prefixes`` drops scratch *directories* like ``.test-temp/``, but a
scratch *file* at the repository root (``.test-temp-student-session.json``) is
not a directory and sailed into the manifest — a live cookie shipped with the
release. These tests pin both halves: the builder refuses a session-shaped file,
and the feature-check script keeps its cache outside the repository.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_release_manifest", ROOT / "tools" / "build_release_manifest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_session_file_at_the_repository_root_is_refused(tmp_path, monkeypatch):
    builder = _load_builder()
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    (tmp_path / "sample.json").write_text("{}", encoding="utf-8")
    # A scratch-named file that still carries a live cookie.
    (tmp_path / ".test-temp-student-session.json").write_text(
        json.dumps({"cookie": "touxian_session=secret"}), encoding="utf-8"
    )
    monkeypatch.setattr(builder, "PUBLIC_SAMPLE", "sample.json", raising=False)
    with pytest.raises(SystemExit):
        builder.build(tmp_path, "1.8.0", "1.5.0")


def test_a_scratch_file_at_the_repository_root_never_ships(tmp_path):
    """A scratch *file* is not a scratch directory, and shipped anyway once.

    ".test-temp-srv9.log" at the repository root went into the manifest because
    the filter only dropped scratch directories. Scratch is scratch by name.
    """
    builder = _load_builder()
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    (tmp_path / "sample.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".test-temp-srv9.log").write_text("INFO: server started", encoding="utf-8")
    (tmp_path / ".test-temp-student-session.json").write_text("{}", encoding="utf-8")

    shipped = [relative for relative, _ in builder.iter_shipped(tmp_path)]

    assert "README.md" in shipped
    assert ".test-temp-srv9.log" not in shipped
    # A session file deliberately survives this filter so that build() can refuse
    # the whole release over it; test_a_session_file_at_the_repository_root_is_refused
    # pins that half.
    assert ".test-temp-student-session.json" in shipped


def test_the_feature_check_never_writes_its_session_into_the_repository():
    source = (ROOT / "scripts" / "verify_student_features.py").read_text(encoding="utf-8")
    assert "tempfile.gettempdir()" in source, "the session cache must live outside the repository"
    assert "parents[1] / \".test-temp-student-session.json\"" not in source
