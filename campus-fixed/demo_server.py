"""One-command local demo launcher (used by run-demo.sh / run-demo.bat).

Binds 127.0.0.1 only. Login codes are shown on the login page (no SMTP needed),
the background collector is OFF, and every inherited production-ish setting is
cleared, so this never fetches external sites and never touches real services.

Data modes:
  sample     (default) a private copy of the shipped 2026-10-09 historical public
             sample, kept in .campus-sample/ so data/ is never written to.
  synthetic  labelled 【演示】 data in .campus-demo/.

Usage: python demo_server.py [--port 8000] [--synthetic]
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

CLEARED = (
    "CAMPUS_DATABASE_URL",
    "CAMPUS_MEILI_URL",
    "CAMPUS_MEILI_KEY",
    "CAMPUS_PUSH_USER_ENDPOINTS",
    "CAMPUS_PUSH_ENDPOINTS",
    "CAMPUS_GCS_BUCKET",
    "CAMPUS_PUBLIC_ORIGIN",
    "CAMPUS_SECRET",
    "CAMPUS_SMTP_HOST",
    "CAMPUS_SMTP_FROM",
    "CAMPUS_TRUSTED_GATEWAY_ORIGINS",
)


def prepare_sample_dir(target: Path) -> None:
    source = ROOT / "data"
    if (target / "campus.sqlite3").exists():
        return
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / "campus.sqlite3", target / "campus.sqlite3")
    if (source / "PUBLIC_SAMPLE.json").exists():
        shutil.copy2(source / "PUBLIC_SAMPLE.json", target / "PUBLIC_SAMPLE.json")
    if (source / "snapshots").is_dir():
        shutil.copytree(source / "snapshots", target / "snapshots", dirs_exist_ok=True)


def configure(port: int, synthetic: bool) -> Path:
    for key in CLEARED:
        os.environ[key] = ""
    data_dir = ROOT / (".campus-demo" if synthetic else ".campus-sample")
    os.environ.update(
        {
            "CAMPUS_ENV": "development",
            "CAMPUS_CLOUD_MODE": "demo",
            "CAMPUS_BLOB_STORE": "local",
            "CAMPUS_DEV_AUTH": "true",
            "CAMPUS_ADMIN_EMAILS": "demo-admin@tju.edu.cn",
            "CAMPUS_EMBEDDED_WORKER": "false",  # collector stays off: no external fetching
            "CAMPUS_DEMO": "true" if synthetic else "false",
            "CAMPUS_ALLOWED_ORIGINS": f"http://127.0.0.1:{port},http://localhost:{port}",
            "CAMPUS_DATA_DIR": str(data_dir),
            "CAMPUS_PUBLIC_API_DOCS": "false",
            "PORT": str(port),
        }
    )
    if not synthetic:
        prepare_sample_dir(data_dir)
    return data_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 8000))
    parser.add_argument("--synthetic", action="store_true", help="use labelled synthetic demo data instead of the sample")
    args = parser.parse_args()
    data_dir = configure(args.port, args.synthetic)

    import uvicorn
    from serve_cloud import build_application

    _, application = build_application()
    api = getattr(application, "app", application)

    @api.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok"}

    kind = "合成演示数据（【演示】）" if args.synthetic else "2026-10-09 历史公开样本（已过期，仅供演示）"
    print(f"数据：{kind}；目录：{data_dir}")
    print(f"打开 http://127.0.0.1:{args.port} ；登录码会直接显示在登录页；演示管理员 demo-admin@tju.edu.cn；Ctrl+C 停止。")
    uvicorn.run(application, host="127.0.0.1", port=args.port, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
