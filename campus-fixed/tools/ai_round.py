#!/usr/bin/env python3
"""Run one verification round of the autonomous improvement loop.

This is the entry point a scheduler calls. It exists because the loop must be
repeatable by something other than a person remembering nine commands: every
step below is one that this project has already got wrong at least once by
skipping it.

    python tools/ai_round.py                 # backend + frontend + normalise + hygiene gate
    python tools/ai_round.py --browser       # also the 9 browser entries
    python tools/ai_round.py --browser --ocr # ... with the real OCR check
    python tools/ai_round.py --json          # machine-readable summary

What it checks, and why each one is here:

* **path hygiene, twice** — ``tools/normalize_evidence_paths.py --check``.
  Delivered evidence must not name the machine it was built on. It runs as a
  *pre-flight* (cheap, and reports a leak that already exists before anything
  else writes more evidence) and again as the *release gate* at the end.
* **normalise evidence** — ``tools/normalize_evidence_paths.py --root .`` (no
  ``--check``), between the writers and the gate. The backend suite and the
  browser entries rewrite ``backend.xml`` and the run logs with absolute paths
  on every run, so the gate would otherwise fail on evidence this script itself
  produced and the remedy would be a second command. The normaliser step only
  runs after those writers, only touches files the round generated, and is
  reported like any other check; the pre-flight gate is unchanged and still
  catches a tree that arrived dirty. (Earlier rounds documented the second
  command as the intended flow; three rounds of paying that cost by hand is what
  changed the policy.)
* **backend suite** — the authoritative record, written to
  ``verification/backend.xml`` and ``verification/audit/backend-suite.log``.
* **frontend unit tests** — ``node --test src/campus/tests/*.test.mjs``.
* **browser entries** (opt-in) — each one gets its **own** freshly seeded
  loopback server on a free port. Sharing one server does not work: the login
  challenge has a 30 second per-email cooldown and a 6/hour cap, so the later
  entries fail with 429 and look like a product fault. ``--ocr`` additionally
  asks the browser entry to exercise the real Chinese OCR model, which needs
  the network on a first run; without it that check reports itself as not run
  instead of silently passing.

Exit status is 0 only when every selected check passed, so a scheduler can treat
non-zero as "do not proceed".

Nothing here mutates the delivered product. It writes evidence and reports only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The interpreter that has the project's dependencies. Overridable, because the
# loop must also run on a machine where they live somewhere else.
DEFAULT_PYTHON = os.environ.get("CAMPUS_PYTHON") or sys.executable
DEFAULT_NODE = os.environ.get("CAMPUS_NODE") or shutil.which("node") or "node"

BROWSER_ENTRIES = (
    "browser",
    "a11y",
    "ui-polish",
    "ui-review",
    "highlight-contrast",
    "source-live",
    "source-selection",
    "feed-mode",
    "reliability",
)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def tail(text: str, lines: int = 12) -> str:
    rows = [row for row in text.splitlines() if row.strip()]
    return "\n".join(rows[-lines:])


class Result:
    def __init__(self, name: str, ok: bool, detail: str = "", seconds: float = 0.0):
        self.name = name
        self.ok = ok
        self.detail = detail
        self.seconds = seconds

    def as_dict(self) -> dict:
        return {"check": self.name, "ok": self.ok, "seconds": round(self.seconds, 1), "detail": self.detail}


def run(
    name: str,
    command: list[str],
    cwd: Path = ROOT,
    timeout: int = 1800,
    env: dict | None = None,
    log_path: Path | None = None,
) -> Result:
    """Run one check.

    ``log_path`` streams the combined output straight into that check's evidence
    file, the way the documented shell command redirects it. Two things depend
    on streaming rather than writing at the end:

    * the evidence is what a reviewer reads, and a stale log is worse than no
      log -- one still quoting a failure that has since been fixed fails the
      path-hygiene guard on every later run;
    * ``test_evidence_tree_hygiene.py`` runs *inside* the backend suite and reads
      that same file, so it must see this run's content, not the last one's.
    """
    started = time.time()
    handle = None
    try:
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(log_path, "wb")
            done = subprocess.run(
                command, cwd=str(cwd), stdout=handle, stderr=subprocess.STDOUT, timeout=timeout, env=env
            )
            output = log_path.read_bytes().decode("utf-8", errors="replace")
        else:
            done = subprocess.run(
                command,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=env,
            )
            output = (done.stdout or "") + (done.stderr or "")
    except subprocess.TimeoutExpired:
        return Result(name, False, f"timed out after {timeout}s", time.time() - started)
    except FileNotFoundError as error:
        return Result(name, False, f"not found: {error.filename}", time.time() - started)
    finally:
        if handle is not None:
            handle.close()
    return Result(name, done.returncode == 0, tail(output), time.time() - started)


def wait_ready(url: str, seconds: int = 60) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/api/v1/health/ready", timeout=2) as reply:
                if reply.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    return False


def check_hygiene(python: str, label: str) -> Result:
    return run(label, [python, "tools/normalize_evidence_paths.py", "--root", ".", "--check"])


def normalize_evidence(python: str) -> Result:
    """Re-normalise the evidence this round just wrote, before the release gate.

    ``check_backend`` and the browser entries regenerate ``backend.xml`` and the
    run logs, and pytest stamps every one of those files with the absolute paths
    of the machine that produced them. Evidence that names the build machine
    cannot ship, so the normaliser has to run *after* the writers and *before*
    the gate. Without this step the gate fails on evidence the round itself made
    and the only remedy is a second command -- three consecutive rounds recorded
    that as an accepted cost, which is what made it worth fixing.

    This does not weaken the pre-flight gate: that one still runs first and
    still fails on a leak that existed before anything wrote more evidence. The
    normaliser here only ever touches files this round generated, and it runs as
    its own reported step rather than behind the caller's back.
    """
    return run("normalize evidence", [python, "tools/normalize_evidence_paths.py", "--root", "."])


def check_backend(python: str) -> Result:
    # A one-off --basetemp is load-bearing: this host blocks a single bulk delete,
    # so pytest's own temp-directory cleanup aborts the process after the tests
    # finish and the console summary is lost. The JUnit file is still complete.
    basetemp = Path(tempfile.mkdtemp(prefix="ai-round-"))
    try:
        return run(
            "backend suite",
            [
                python,
                "-m",
                "pytest",
                "server/campus/tests",
                "-q",
                "--junitxml=verification/backend.xml",
                f"--basetemp={basetemp}",
            ],
            log_path=ROOT / "verification" / "audit" / "backend-suite.log",
        )
    finally:
        shutil.rmtree(basetemp, ignore_errors=True)


def check_frontend(node: str) -> Result:
    tests = sorted(
        str(path.relative_to(ROOT)).replace(os.sep, "/")
        for path in (ROOT / "src" / "campus" / "tests").glob("*.test.mjs")
    )
    if not tests:
        return Result("frontend unit tests", False, "no *.test.mjs found")
    return run("frontend unit tests", [node, "--test", *tests])


def read_seeded_event_id(path: Path, seconds: float = 15.0) -> str | None:
    """The event ID a feed-mode fixture server seeds before it starts serving.

    ``browser_demo`` writes the file *before* ``uvicorn.run``, so by the time
    ``/api/v1/health/ready`` answers the file is already complete. The poll is
    defensive: a reader that just opens the path can lose a race against a slow
    disk and fail an entry that in fact works.
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            time.sleep(0.25)
            continue
        return payload.get("event_id")
    return None


@dataclass(frozen=True)
class EntryPlan:
    """Everything needed to start one browser entry's fixture server."""

    command: list[str]
    environment: dict[str, str]
    data_dir: Path
    feed_event_info: Path | None = None
    manifest: Path | None = None


def entry_launch_plan(
    entry: str,
    python: str,
    port: int,
    scratch: Path,
    *,
    ocr: bool = False,
    base_env: dict[str, str] | None = None,
) -> EntryPlan:
    """Build the server command and environment for one entry.

    Pure, and deliberately so: the per-entry contracts encoded here are exactly
    what the first version of this runner omitted -- it launched all nine
    entries with one uniform command, so feed-mode asserted on a missing
    ``CAMPUS_E2E_FEED_ORIGIN`` and reliability never started at all. A pure
    function makes those contracts unit-testable
    (``test_ai_round_browser_plan.py``) instead of only observable by running
    the entries and reading the failure.

    ``scratch`` is a caller-owned fresh directory; it is never touched here, so
    a test can pass a path that does not exist.
    """
    # reliability_demo refuses to start unless its data directory does not exist
    # yet, so it gets a fresh subdirectory of the scratch tree; the other
    # servers accept an existing one.
    data_dir = scratch / "fixture" if entry == "reliability" else scratch
    # Evidence lands in the shipped per-entry layout. A scratch directory at
    # the root would ship with the next manifest rebuild and sit outside the
    # hygiene gate's normalised tree; the tracked layout is what the acceptance
    # record already refers to.
    # The base environment is inherited wholesale, which is how
    # PLAYWRIGHT_MODULE and the Playwright browser cache reach the server and
    # the entry alike.
    environment = dict(os.environ if base_env is None else base_env)
    environment.update(
        {
            "CAMPUS_E2E_URL": f"http://127.0.0.1:{port}",
            "CAMPUS_E2E_OUTPUT": str(ROOT / "verification" / "browser" / entry / "artifacts"),
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
    )
    if ocr:
        # Off by default: the browser entry then downloads the chi_sim+eng
        # recognition model, which needs the network and is the one check a
        # reviewer must be able to see ran.
        environment["CAMPUS_E2E_OCR"] = "true"
    server_module = (
        "server.campus.tests.reliability_demo" if entry == "reliability" else "server.campus.tests.browser_demo"
    )
    command = [python, "-m", server_module, "--port", str(port), "--data-dir", str(data_dir)]
    feed_event_info: Path | None = None
    manifest: Path | None = None
    # Two entries have contracts a plain fixture server cannot meet (see
    # src/campus/tests/BROWSER_ACCEPTANCE.md): feed-mode refuses to run without
    # CAMPUS_E2E_FEED_ORIGIN and drives a seeded authorized-feed event whose ID
    # browser_demo writes to --feed-event-info; reliability runs a different
    # module that will not even start without --manifest, and its entry reads
    # that same manifest. Without this, "one command" covers seven entries, not
    # nine.
    if entry == "feed-mode":
        feed_event_info = data_dir / "feed-event-info.json"
        command += ["--feed-event-info", str(feed_event_info)]
        environment["CAMPUS_E2E_FEED_ORIGIN"] = f"http://127.0.0.1:{port}"
        # The entry creates a source pointing at the fixture, and
        # create_source() runs canonical_url() + resolve_public(): a URL on a
        # non-default port is refused as unsafe_port unless its origin is an
        # explicitly trusted gateway, and 127.0.0.1 is not a global address
        # unless the same list trusts it. The fixture runs on an ephemeral port
        # by design, so the server must trust exactly that origin -- which is
        # what BROWSER_ACCEPTANCE.md means by "服务以 CAMPUS_TRUSTED_GATEWAY_ORIGINS
        # 信任该来源".
        environment["CAMPUS_TRUSTED_GATEWAY_ORIGINS"] = f"http://127.0.0.1:{port}"
    elif entry == "reliability":
        manifest = data_dir / "reliability-manifest.json"
        command += ["--manifest", str(manifest)]
        environment["CAMPUS_E2E_FIXTURE"] = str(manifest)
    return EntryPlan(command, environment, data_dir, feed_event_info, manifest)


def check_browser(python: str, node: str, entries: tuple[str, ...], *, ocr: bool = False) -> list[Result]:
    results: list[Result] = []
    for entry in entries:
        script = ROOT / "src" / "campus" / "tests" / f"{entry}.e2e.mjs"
        if not script.is_file():
            results.append(Result(f"browser/{entry}", False, "entry script missing"))
            continue
        port = free_port()
        scratch = Path(tempfile.mkdtemp(prefix=f"ai-round-{entry}-"))
        plan = entry_launch_plan(entry, python, port, scratch, ocr=ocr)
        server = subprocess.Popen(
            plan.command,
            cwd=str(ROOT),
            env=plan.environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            waited = time.time()
            if not wait_ready(f"http://127.0.0.1:{port}"):
                results.append(
                    Result(
                        f"browser/{entry}",
                        False,
                        "fixture server did not become ready",
                        time.time() - waited,
                    )
                )
                continue
            if plan.feed_event_info is not None:
                event_id = read_seeded_event_id(plan.feed_event_info)
                if not event_id:
                    results.append(Result(f"browser/{entry}", False, "fixture did not report a seeded feed event"))
                    continue
                plan.environment["CAMPUS_E2E_CAPTURE_EVENT"] = event_id
            # The entry must receive CAMPUS_E2E_URL: without it each script falls
            # back to its own hard-coded port and the run reports ECONNREFUSED
            # against a server that was never started.
            results.append(
                run(f"browser/{entry}", [node, str(script.relative_to(ROOT))], timeout=600, env=plan.environment)
            )
        finally:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
            shutil.rmtree(scratch, ignore_errors=True)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--python", default=DEFAULT_PYTHON)
    parser.add_argument("--node", default=DEFAULT_NODE)
    parser.add_argument(
        "--browser", action="store_true", help="also run the browser entries (slow; each starts its own server)"
    )
    parser.add_argument("--only", action="append", default=None, help="run only this browser entry (repeatable)")
    parser.add_argument(
        "--ocr", action="store_true", help="with --browser, also run the real OCR check (downloads the model)"
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    results: list[Result] = [
        check_hygiene(args.python, "path hygiene (pre-flight)"),
        check_backend(args.python),
        check_frontend(args.node),
    ]
    if args.browser:
        entries = tuple(args.only) if args.only else BROWSER_ENTRIES
        results.extend(check_browser(args.python, args.node, entries, ocr=args.ocr))
    # The writers above just stamped fresh evidence with this machine's paths, so
    # the tree is normalised before it is judged, not after the FAIL.
    results.append(normalize_evidence(args.python))
    # Last, so it judges the tree the run just wrote, not the tree it inherited.
    results.append(check_hygiene(args.python, "path hygiene (release gate)"))

    failed = [result for result in results if not result.ok]
    if args.json:
        print(json.dumps({"ok": not failed, "checks": [r.as_dict() for r in results]}, ensure_ascii=False, indent=2))
    else:
        for result in results:
            print(f"{'PASS' if result.ok else 'FAIL'}  {result.name}  ({result.seconds:.1f}s)")
            if not result.ok:
                print("\n".join("      " + row for row in result.detail.splitlines()))
        print()
        print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
