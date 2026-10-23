"""Read-only deployment checks. Default: local files only, no network or DB writes."""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[2]


def check(name, ok, detail):
    return {"check": name, "ok": bool(ok), "detail": detail}


def local_checks(root=ROOT):
    """Do not load Settings/Core: their construction may initialize the database."""
    required = [
        "deploy/cloudrun/Dockerfile",
        "deploy/cloudrun/deploy.ps1",
        "deploy/cloudrun/cloudbuild.yaml",
        "requirements-campus.lock.txt",
        "requirements-cloud.lock.txt",
        "pnpm-lock.yaml",
        "config/campus-sources.json",
        "campus-contracts/openapi.v1.json",
        "server/campus/cloud_run.py",
    ]
    missing = [name for name in required if not (root / name).is_file()]
    results = [
        check(
            "deployment_files",
            not missing,
            "required files present" if not missing else "missing: " + ", ".join(missing),
        )
    ]
    docker = (root / "deploy/cloudrun/Dockerfile").read_text(encoding="utf-8") if not missing else ""
    bases = re.findall(r"^FROM\s+(\S+)", docker, re.M)
    results.append(
        check(
            "image_digests",
            len(bases) == 2 and all(re.search(r"@sha256:[a-f0-9]{64}$", value) for value in bases),
            "both base images require SHA256 digests",
        )
    )

    def pins(path):
        return (
            {
                line.split("==")[0].lower(): line.split("==")[1].split(";")[0].strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if re.match(r"^[a-zA-Z0-9_.-]+==[^\s]+", line)
            }
            if path.is_file()
            else {}
        )

    core = pins(root / "requirements-campus.lock.txt")
    cloud = pins(root / "requirements-cloud.lock.txt")
    cloud_file = root / "requirements-cloud.lock.txt"
    includes_core = (
        cloud_file.is_file()
        and "-r requirements-campus.lock.txt" in cloud_file.read_text(encoding="utf-8").splitlines()
    )
    combined = {**core, **cloud}
    results.append(
        check(
            "cloud_lock_preserves_core",
            bool(core) and includes_core and all(combined.get(name) == version for name, version in core.items()),
            "cloud lock must preserve every core package version",
        )
    )
    guards = ["**/.server-secret", "**/*.sqlite3*", "**/*.db", "**/*.pem", "**/*.key"]
    for relative in ["deploy/cloudrun/.gcloudignore", "deploy/cloudrun/Dockerfile.dockerignore"]:
        path = root / relative
        lines = set(path.read_text(encoding="utf-8").splitlines()) if path.is_file() else set()
        absent = [value for value in guards if value not in lines]
        results.append(
            check(
                "secret_exclusions:" + path.name,
                not absent,
                "secret/runtime patterns excluded" if not absent else "missing patterns: " + ", ".join(absent),
            )
        )
    try:
        schema = json.loads((root / "campus-contracts/openapi.v1.json").read_text(encoding="utf-8"))
        valid = schema.get("openapi", "").startswith("3.") and "/health/ready" in schema.get("paths", {})
    except (OSError, ValueError):
        valid = False
    results.append(check("health_contract", valid, "OpenAPI declares /health/ready"))
    return results


def database_checks(database_url):
    """SELECT only; no schema initialization, row locks, jobs or student content."""
    try:
        from sqlalchemy import create_engine, text
        from sqlalchemy.engine import make_url

        url = make_url(database_url)
        if url.get_backend_name() != "postgresql":
            return [check("postgres_read_only", False, "PostgreSQL URL required")]
        if url.drivername == "postgresql":
            url = url.set(drivername="postgresql+psycopg")
        engine = create_engine(url, connect_args={"connect_timeout": 5}, pool_pre_ping=True)
        try:
            with engine.connect() as connection, connection.begin():
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                connection.exec_driver_sql("SET LOCAL statement_timeout = '5000ms'")
                version = connection.exec_driver_sql("SHOW server_version").scalar_one()
                tables = (
                    connection.execute(
                        text(
                            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() AND tablename LIKE 'campus_%'"
                        )
                    )
                    .scalars()
                    .all()
                )
                required = {"campus_records", "campus_jobs", "campus_outbox", "campus_schema_versions"}
                results = [
                    check("postgres_read_only", True, "connected; server " + str(version)),
                    check(
                        "schema_tables",
                        required.issubset(tables),
                        "required table count " + str(len(required.intersection(tables))) + "/4",
                    ),
                ]
                if not required.issubset(tables):
                    return results
                versions = (
                    connection.exec_driver_sql("SELECT version FROM campus_schema_versions ORDER BY version")
                    .scalars()
                    .all()
                )
                results.append(check("schema_version", versions == [1], "versions " + ",".join(map(str, versions))))
                rows = connection.exec_driver_sql("SELECT state, COUNT(*) FROM campus_jobs GROUP BY state").all()
                pending = connection.exec_driver_sql(
                    "SELECT COUNT(*) FROM campus_outbox WHERE dispatched = false"
                ).scalar_one()
                overdue = connection.execute(
                    text("SELECT MIN(run_at) FROM campus_jobs WHERE state = 'queued' AND run_at <= :now"),
                    {"now": datetime.now(UTC).isoformat().replace("+00:00", "Z")},
                ).scalar_one()
                results.append(
                    check(
                        "queue_snapshot",
                        True,
                        json.dumps(
                            {"states": dict(rows), "pending_outbox": pending, "oldest_due_at": overdue},
                            separators=(",", ":"),
                        ),
                    )
                )
                return results
        finally:
            engine.dispose()
    except Exception:
        # Driver errors can contain URLs, SQL parameters, passwords and hostnames.
        return [check("postgres_read_only", False, "connection/schema check failed; inspect protected database logs")]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, newurl):
        return None


def service_checks(origin, identity_token=None):
    try:
        parts = urlsplit(origin)
        valid = (
            parts.scheme == "https"
            and parts.hostname
            and parts.hostname.endswith(".run.app")
            and not any(value.isspace() for value in origin)
            and "\\" not in origin
            and not parts.username
            and not parts.password
            and parts.port in (None, 443)
            and parts.path in ("", "/")
            and not parts.query
            and not parts.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        return [check("service_origin", False, "use the exact HTTPS run.app service origin")]
    opener = build_opener(NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))
    results = []
    for route in ["/health/live", "/health/ready"]:
        headers = {"Accept": "application/json"}
        if identity_token:
            headers["Authorization"] = "Bearer " + identity_token
        try:
            request = Request(origin.rstrip("/") + "/api/v1" + route, headers=headers)
            with opener.open(request, timeout=10) as response:
                body = response.read(16385)
                if len(body) > 16384:
                    raise ValueError("response too large")
                data = json.loads(body)
            if route.endswith("/live"):
                ok = data.get("status") == "alive"
            else:
                ok = (
                    data.get("status") == "ready"
                    and data.get("demo_data") is False
                    and data.get("login_delivery") == "smtp_configured"
                )
            results.append(
                check(route, ok, "expected production health shape" if ok else "production health conditions not met")
            )
        except (HTTPError, URLError, OSError, ValueError):
            results.append(
                check(
                    route, False, "HTTPS check failed; redirects are rejected; no credentials or response body printed"
                )
            )
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-read-only",
        action="store_true",
        help="read CAMPUS_DATABASE_URL; execute SELECT-only PostgreSQL checks",
    )
    parser.add_argument("--service-url", help="explicit HTTPS health checks of an existing run.app service")
    args = parser.parse_args(argv)
    results = local_checks()
    if args.database_read_only:
        value = os.getenv("CAMPUS_DATABASE_URL")
        results += (
            database_checks(value) if value else [check("postgres_read_only", False, "CAMPUS_DATABASE_URL is not set")]
        )
    if args.service_url:
        results += service_checks(args.service_url, os.getenv("CAMPUS_CHECK_IDENTITY_TOKEN"))
    print(
        json.dumps(
            {
                "read_only": True,
                "network_requested": bool(args.database_read_only or args.service_url),
                "checks": results,
                "ok": all(value["ok"] for value in results),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if all(value["ok"] for value in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
