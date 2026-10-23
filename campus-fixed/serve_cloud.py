"""Single-port launcher for hosted (cloud sandbox) deployments.

``app.py`` stays the public production entry: it forces the development login
code off, which is the right default whenever real SMTP delivers login codes.
A hosted instance usually has no SMTP credentials, so this launcher serves that
one gap explicitly and nothing else:

* ``demo`` mode (``CAMPUS_CLOUD_MODE=demo``, the default): an empty SQLite
  store is seeded once with the labeled synthetic demo data
  (``server.campus.demo.seed_demo``), the development login code is switched on
  so the instance is enterable without SMTP, the embedded worker stays off, and
  the first address in ``CAMPUS_ADMIN_EMAILS`` becomes the instance operator.
  No real notice is faked: every seeded record carries the 【演示】 marker.
* ``live`` mode (``CAMPUS_CLOUD_MODE=live``): same bootstrap as ``app.py`` —
  the development login code stays off even when inherited from the
  environment — the official sources from ``config/campus-sources.json`` are
  registered once, and the embedded worker collects from them.
* ``operator`` mode (``CAMPUS_CLOUD_MODE=operator``): the local operations
  instance. The official sources are registered once and the embedded worker
  runs, but no synthetic demo data is ever seeded, so everything the pipeline
  publishes is real. Because such an instance has no SMTP either, the
  development login code is switched on — this is only acceptable because an
  operator instance binds ``127.0.0.1`` by default (override with
  ``CAMPUS_OPERATOR_HOST``): the development code never leaves the machine.
  This is the mode a restricted-network operator runs when an authorized
  relay (``deploy/operator/relay.py``) feeds the product from a machine that
  can reach the official sites.

Both hosted modes listen on ``0.0.0.0`` at ``PORT`` (injected by the host), verify the
data directory, the SQLite file and the frontend build before serving, and
keep the interactive API docs unexposed. Nothing here changes product
behaviour: every value is a launch-time environment default that an explicit
environment variable overrides.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

DEMO_MODE = "demo"
LIVE_MODE = "live"
OPERATOR_MODE = "operator"
MODES = (DEMO_MODE, LIVE_MODE, OPERATOR_MODE)
DEFAULT_ADMIN_EMAIL = "demo-admin@touxian.local"
DEFAULT_OPERATOR_EMAIL = "operator@touxian.local"
LOCAL_ORIGINS = ",".join(f"http://{host}:{port}" for port in (5173, 4173, 8000) for host in ("localhost", "127.0.0.1"))
# ``app.py`` protects its public entrypoint with an unconditional assignment;
# a live instance must keep that guard even when a development-auth variable was
# inherited from the hosting environment. Operator mode is deliberately not in
# FORCED: it is a loopback-bound instance, and forcing dev-auth off there would
# lock the operator out of an instance that has no SMTP by construction.
FORCED = {"CAMPUS_DEV_AUTH"}
OPERATOR_HOST_MODES = frozenset({OPERATOR_MODE})


def _true(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def cloud_env(env=None) -> dict:
    """Return the launch-time environment defaults for the selected mode.

    ``setdefault`` semantics: a value already present in the environment always
    wins. The result is a mapping of variable name to default value; ``live``
    mode deliberately sets ``CAMPUS_DEV_AUTH=false`` so an inherited value can
    never re-enable the development login code on a live instance.
    """
    env = os.environ if env is None else env
    mode = (env.get("CAMPUS_CLOUD_MODE") or DEMO_MODE).strip().lower()
    if mode not in MODES:
        raise ValueError(f"CAMPUS_CLOUD_MODE must be one of {', '.join(MODES)}")
    defaults = {
        "CAMPUS_CLOUD_MODE": mode,
        "CAMPUS_DATA_DIR": str(ROOT / "data"),
        "CAMPUS_SOURCE_REFRESH_SECONDS": "21600",
        "CAMPUS_PUBLIC_API_DOCS": "false",
    }
    public_origin = (env.get("CAMPUS_PUBLIC_ORIGIN") or "").strip().rstrip("/")
    if public_origin:
        origins = [item.strip() for item in (env.get("CAMPUS_ALLOWED_ORIGINS") or "").split(",") if item.strip()]
        if public_origin not in origins:
            origins.append(public_origin)
        defaults["CAMPUS_ALLOWED_ORIGINS"] = ",".join(origins or [LOCAL_ORIGINS])
    if mode == DEMO_MODE:
        defaults.update(
            {
                "CAMPUS_DEV_AUTH": "true",
                "CAMPUS_ADMIN_EMAILS": DEFAULT_ADMIN_EMAIL,
                "CAMPUS_DEMO": "true",
                "CAMPUS_EMBEDDED_WORKER": "false",
            }
        )
    elif mode == OPERATOR_MODE:
        # A loopback operations instance without SMTP: the development login
        # code is the only way in, and nothing synthetic is ever seeded, so a
        # published event can only come from a real collection cycle.
        defaults.update(
            {
                "CAMPUS_DEV_AUTH": "true",
                "CAMPUS_ADMIN_EMAILS": DEFAULT_OPERATOR_EMAIL,
                "CAMPUS_EMBEDDED_WORKER": "true",
            }
        )
    else:
        defaults.update({"CAMPUS_DEV_AUTH": "false", "CAMPUS_EMBEDDED_WORKER": "true"})
    return defaults


def apply_cloud_env(env=None) -> dict:
    """Apply :func:`cloud_env`; return what this call set.

    Defaults follow ``setdefault`` semantics (an explicit value wins). Only a
    live instance re-asserts :data:`FORCED` against inherited values, mirroring
    the unconditional guard of ``app.py``; a demo instance may still switch the
    development login code off explicitly.
    """
    environ = os.environ if env is None else env
    live = (environ.get("CAMPUS_CLOUD_MODE") or DEMO_MODE).strip().lower() == LIVE_MODE
    forced = FORCED if live else set()
    applied = {}
    for key, value in cloud_env(environ).items():
        if key in forced or environ.get(key) is None:
            environ[key] = value
            applied[key] = value
    return applied


def should_seed_demo(core) -> bool:
    """Seed only a demonstrably empty store; never overwrite operator data."""
    return not core.store.list("events")


def should_register_sources(core) -> bool:
    """Register the configured sources once; never re-register over live rows."""
    return not core.store.list("sources")


def serve_address(env=None) -> tuple:
    env = os.environ if env is None else env
    port = int(env.get("PORT") or "3000")
    mode = (env.get("CAMPUS_CLOUD_MODE") or DEMO_MODE).strip().lower()
    if mode in OPERATOR_HOST_MODES:
        # An operations instance listens on loopback unless explicitly published.
        return env.get("CAMPUS_OPERATOR_HOST") or "127.0.0.1", port
    return "0.0.0.0", port


def same_origin(request_host: str, origin: str) -> bool:
    """True when a browser Origin names the very host being served."""
    origin = origin.strip().rstrip("/")
    return any(origin == f"{scheme}://{request_host}" for scheme in ("http", "https"))


class SameOriginWrites:
    """Let same-origin writes through the product's fixed origin allowlist.

    The guard in ``server.campus.platform`` compares the request Origin against
    ``CAMPUS_ALLOWED_ORIGINS``, a value set at launch time. An ephemeral hosting
    domain is not known when the allowlist is computed, so a browser's own
    origin would be rejected as foreign. A browser always sends the site's own
    origin for same-origin writes, and any real cross-site forgery still carries
    a foreign origin and still fails the guard, so dropping a self-origin header
    preserves the guard's intent without weakening it.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            headers = scope.setdefault("headers", [])
            host = origin = None
            for key, value in headers:
                name = key.decode("latin-1").lower()
                if name == "host":
                    host = value.decode("latin-1")
                elif name == "origin":
                    origin = value.decode("latin-1")
            if host and origin and same_origin(host, origin):
                scope["headers"] = [(key, value) for key, value in headers if key.decode("latin-1").lower() != "origin"]
        await self.app(scope, receive, send)


def build_application(env=None):
    """Bootstrap and return ``(core, application)`` for the selected mode."""
    from server.campus.bootstrap import check_database, materialize_frontend, require_writable_directory
    from server.campus.platform import Settings
    from server.campus_app import assemble, create_app

    apply_cloud_env(env)
    settings = Settings.from_env()
    require_writable_directory(settings.data_dir)
    check_database(settings.database_url)
    materialize_frontend(ROOT)
    core = assemble(settings)
    mode = (os.environ.get("CAMPUS_CLOUD_MODE") or DEMO_MODE).strip().lower()
    try:
        if mode == DEMO_MODE:
            if should_seed_demo(core):
                from server.campus.demo import seed_demo

                seed_demo(core)
                print("serve_cloud: seeded labeled synthetic demo data", flush=True)
        elif should_register_sources(core):
            # live and operator both register the catalog once; operator never
            # seeds synthetic events, so every published event is real.
            core.pipeline.sources.seed()
            print("serve_cloud: registered configured sources", flush=True)
    except Exception as error:  # pragma: no cover - startup diagnostics only
        print(f"serve_cloud: startup preparation failed: {error!r}", file=sys.stderr, flush=True)
    finally:
        core.store.close()
    return core, SameOriginWrites(create_app(settings))


def main():
    import uvicorn

    host, port = serve_address()
    _, application = build_application()
    uvicorn.run(application, host=host, port=port, access_log=True)


if __name__ == "__main__":
    main()
