"""Validate a durable runtime before opening the public application.

SQLite owns its recovery sidecars. Bootstrap never resets storage, rotates a
persisted secret, or silently substitutes a temporary copy of operator data.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import stat
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from sqlalchemy.engine import make_url

from .platform import Settings


class BootstrapError(RuntimeError):
    pass


def require_writable_directory(path):
    path = Path(path).resolve()
    try:
        path.mkdir(parents=True, exist_ok=True)
        # Also respect an explicitly read-only directory when running privileged.
        if not path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
            raise PermissionError("directory is read-only")
        with tempfile.TemporaryFile(prefix=".campus-write-probe-", dir=path):
            pass
    except OSError as error:
        raise BootstrapError(f"Data directory is not writable: {path}") from error
    return path


def check_database(database_url):
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return
    path = Path(url.database).resolve()
    if not path.exists():
        return
    if not path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
        raise BootstrapError("SQLite database is read-only; restore writable storage before starting")
    try:
        # Ordinary read/write opening permits SQLite's own crash recovery. Do not
        # use immutable=1: it would ignore committed transactions still in WAL.
        connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=30)
        try:
            result = [row[0] for row in connection.execute("PRAGMA integrity_check")]
            if result != ["ok"]:
                raise BootstrapError("SQLite integrity check failed; restore storage before starting")
        finally:
            connection.close()
    except sqlite3.DatabaseError as error:
        raise BootstrapError("SQLite could not be recovered; restore storage before starting") from error


def complete_frontend(directory):
    directory = Path(directory)
    try:
        html = (directory / "campus.html").read_text(encoding="utf-8")
        refs = re.findall(r"(?:src|href)\s*=\s*[\"\']([^\"\']+)[\"\']", html)
        assets = []
        for reference in refs:
            value = urlsplit(reference)
            if value.scheme or value.netloc or not value.path.startswith("/assets/"):
                continue
            relative = Path(unquote(value.path.lstrip("/")))
            target = (directory / relative).resolve()
            if directory.resolve() not in target.parents or not target.is_file():
                return False
            assets.append(target)
        return bool(assets) and any(asset.suffix == ".js" for asset in assets)
    except (OSError, ValueError):
        return False


def materialize_frontend(root):
    root = Path(root).resolve()
    dist, web = root / "dist", root / "web"
    if complete_frontend(dist):
        return dist  # A complete read-only deployment needs no startup writes.
    if not complete_frontend(web):
        raise BootstrapError("Missing complete frontend build in dist/ or web/")
    if dist.exists() and not dist.is_dir():
        raise BootstrapError("Frontend destination dist/ is not a directory")
    staged = backup = None
    installed = False
    try:
        staged = Path(tempfile.mkdtemp(prefix=".campus-dist-stage-", dir=root))
        shutil.copytree(web, staged, dirs_exist_ok=True)
        if not complete_frontend(staged):
            raise BootstrapError("Frontend copy is incomplete")
        if dist.exists():
            backup = root / (".campus-dist-backup-" + uuid4().hex)
            dist.rename(backup)
        staged.rename(dist)
        staged = None
        installed = True
    except OSError as error:
        if backup is not None and backup.exists() and not dist.exists():
            backup.rename(dist)
            backup = None
        raise BootstrapError("Cannot materialize frontend; build dist/ before starting") from error
    finally:
        for generated in (staged, backup if installed else None):
            if generated is not None:
                # Delete only the exact generated directories within this root.
                resolved = generated.resolve()
                if resolved.parent == root and resolved.name.startswith(".campus-dist-"):
                    shutil.rmtree(resolved)
    return dist


def prepare_runtime(root):
    root = Path(root).resolve()
    os.environ.setdefault("CAMPUS_DATA_DIR", str(root / "data"))
    os.environ.setdefault("CAMPUS_SOURCE_REFRESH_SECONDS", "21600")
    os.environ.setdefault("CAMPUS_PUBLIC_API_DOCS", "false")
    # This entrypoint is public, even if a development-auth variable was inherited.
    os.environ["CAMPUS_DEV_AUTH"] = "false"
    settings = Settings.from_env()  # Preserve production guards and external configuration.
    require_writable_directory(settings.data_dir)
    check_database(settings.database_url)
    materialize_frontend(root)
    return settings
