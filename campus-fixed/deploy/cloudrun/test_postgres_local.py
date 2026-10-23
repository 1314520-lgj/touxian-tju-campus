"""Run real database acceptance in a disposable loopback PostgreSQL cluster.

Requires a separately verified PostgreSQL bin directory. Does not download,
install a service, read production configuration, or create cloud resources.
"""

from __future__ import annotations

import argparse
import csv
import os
import secrets
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-bin", required=True, type=Path)
    args = parser.parse_args(argv)
    suffix = ".exe" if os.name == "nt" else ""
    bins = {name: args.postgres_bin.resolve() / (name + suffix) for name in ["postgres", "initdb", "pg_ctl"]}
    if not all(path.is_file() for path in bins.values()):
        parser.error("postgres, initdb and pg_ctl must exist in the verified directory")
    import psycopg
    from sqlalchemy.engine import URL

    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    def command(arguments, capture=True):
        result = subprocess.run(
            [str(value) for value in arguments],
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
            text=True,
            timeout=60,
            creationflags=flags,
        )
        if result.returncode:
            # Command arguments may name a password file. Do not print them or
            # unfiltered DB diagnostics into a shareable acceptance report.
            raise RuntimeError("Isolated PostgreSQL utility failed; no credentials printed")
        return (result.stdout or "").strip()

    print(command([bins["postgres"], "--version"]), flush=True)
    with tempfile.TemporaryDirectory(prefix="campus-postgres-acceptance-") as directory:
        base = Path(directory)
        if os.name == "nt":
            # PostgreSQL drops Administrator group privileges on Windows. Give
            # only the current user explicit access to this temporary directory;
            # inherited Administrators-only Temp permissions are insufficient.
            owner = next(csv.reader([command(["whoami", "/user", "/fo", "csv", "/nh"])]))[1]
            command(["icacls", base, "/grant", "*" + owner + ":(OI)(CI)F"])
        data = base / "data"
        password = secrets.token_urlsafe(32)
        password_file = base / "password.txt"
        password_file.write_text(password + "\n", encoding="utf-8")
        command(
            [
                bins["initdb"],
                "-D",
                data,
                "--encoding=UTF8",
                "--locale=C",
                "--username=campus_acceptance",
                "--auth-host=scram-sha-256",
                "--auth-local=scram-sha-256",
                "--pwfile=" + str(password_file),
            ]
        )
        password_file.unlink()
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        started = False
        try:
            started = True
            # On Windows server children may inherit pg_ctl's pipe handles even
            # with a log file. DEVNULL avoids waiting for server-lifetime EOF.
            command(
                [bins["pg_ctl"], "-D", data, "-l", base / "server.log", "-o", f"-h 127.0.0.1 -p {port}", "-w", "start"],
                capture=False,
            )
            # The generated password and loopback bind keep the disposable test
            # database separate from any system server or student data.
            url = URL.create(
                "postgresql+psycopg",
                username="campus_acceptance",
                password=password,
                host="127.0.0.1",
                port=port,
                database="campus_operations_test",
            )
            with psycopg.connect(
                host="127.0.0.1",
                port=port,
                user="campus_acceptance",
                password=password,
                dbname="postgres",
                autocommit=True,
            ) as connection:
                connection.execute("CREATE DATABASE campus_operations_test")
            env = {**os.environ, "CAMPUS_TEST_POSTGRES_URL": url.render_as_string(hide_password=False)}
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "server/campus/tests/test_postgres_operations.py",
                    "server/campus/tests/test_worker_execution_gate.py",
                    "server/campus/tests/test_cloud_preflight.py",
                ],
                cwd=ROOT,
                env=env,
                creationflags=flags,
                timeout=180,
                capture_output=True,
                text=True,
            )
            for output in (result.stdout, result.stderr):
                if output:
                    print(output.replace(password, "[redacted]"), end="", flush=True)
            return result.returncode
        finally:
            if started and (data / "postmaster.pid").is_file():
                command([bins["pg_ctl"], "-D", data, "-m", "immediate", "-w", "stop"])
                print("Disposable loopback PostgreSQL stopped; temporary cluster removed.", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
