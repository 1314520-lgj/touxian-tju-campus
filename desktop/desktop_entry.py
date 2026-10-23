"""偷闲·天大版 — 桌面版启动器（PyInstaller 冻结入口）。

设计目标：双击 exe 就能得到一个本机「偷闲·天大版」实例：
  * 数据落在 exe 同目录的 data/（目录不可写时退回 %LOCALAPPDATA%\\TouxianCampus\\data）
  * 首次启动注册 config/campus-sources.json 里的全部官方来源，并启动内嵌采集 worker
  * 开发登录码打开（本机 loopback 实例没有 SMTP），默认管理员邮箱 operator@touxian.local
  * 自动打开浏览器到本机实例；关闭控制台窗口即停止服务

公众号通路（可选、默认关闭）：exe 不登录微信、不持任何微信凭证；只读取运营者自己
运行的第三方工具（WeWe RSS）产出的 feed，把 feed 当来源采集。在 settings 文件里写
"wechat_feed_enabled": true 与 "wechat_feeds": [{"name": "...", "url": "..."}]。
四项风险见 docs/WECHAT-RSS-SETUP.md（微信读书账号封控 / 第三方转发 / 上游已归档 /
绕过平台常规访问方式需自担风险）。

注意：本源码文件不属于产品运行时代码，只由 tools/desktop_exe/build.ps1 打包使用。
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

APP_NAME = "TouxianCampus"
SETTINGS_NAME = "touxian-settings.json"
DEFAULT_SETTINGS = {
    "port": 8765,
    "admin_email": "operator@touxian.local",
    "wechat_feed_enabled": False,
    "wechat_feeds": [],
    "trusted_origins": [],
}


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    """代码与静态资源所在目录：冻结后是 PyInstaller 解包目录。"""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parents[1] / "campus-fixed"


def exe_dir() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / (".write-probe-" + os.urandom(4).hex())
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def load_settings(base: Path) -> dict:
    path = base / SETTINGS_NAME
    settings = dict(DEFAULT_SETTINGS)
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                settings.update(loaded)
        except (OSError, ValueError):
            pass
    else:
        try:
            path.write_text(json.dumps(DEFAULT_SETTINGS, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass
    return settings


def pick_port(preferred: int) -> int:
    for port in [preferred] + list(range(preferred + 1, preferred + 30)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    return 0


def config_origins(root: Path) -> list[str]:
    """官方来源配置里的 origin —— 本机 TUN fake-IP 场景下的显式受信网关。"""
    path = root / "config" / "campus-sources.json"
    try:
        entries = json.loads(path.read_text(encoding="utf-8")).get("sources", [])
    except (OSError, ValueError):
        return []
    origins = set()
    for entry in entries:
        url = entry.get("origin_url") if isinstance(entry, dict) else None
        if not isinstance(url, str):
            continue
        from urllib.parse import urlsplit

        parsed = urlsplit(url)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            origins.add(f"{parsed.scheme}://{parsed.netloc}")
    return sorted(origins)


def merge_env(name: str, values: list[str]) -> None:
    existing = [item.strip() for item in (os.environ.get(name) or "").split(",") if item.strip()]
    merged = []
    for value in existing + [v for v in values if v]:
        if value not in merged:
            merged.append(value)
    if merged:
        os.environ[name] = ",".join(merged)


def feed_entries(settings: dict) -> list[str]:
    """把 settings 里的公众号 feed 列表转成 CAMPUS_WECHAT_FEEDS 的 name=url 形式。"""
    entries = []
    for item in settings.get("wechat_feeds") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        url = str(item.get("url") or "").strip()
        if name and url.startswith(("http://127.0.0.1", "http://localhost", "https://")):
            entries.append(f"{name}={url}")
    return entries


def local_wewe_feed(timeout: float = 1.5) -> tuple[str | None, int]:
    """本机 WeWe RSS（微信读书路线）的 feed 地址与条目数。

    返回 (url, item_count)。服务没起时返回 (None, 0)。只认 127.0.0.1/localhost
    的 /feeds/all.json，避免把任意地址当成公众号来源。
    """
    candidates = [
        "http://127.0.0.1:4000/feeds/all.json",
        "http://localhost:4000/feeds/all.json",
    ]
    for url in candidates:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                if response.status != 200:
                    continue
                payload = json.loads(response.read().decode("utf-8", "replace"))
                items = payload.get("items") if isinstance(payload, dict) else None
                return url, len(items) if isinstance(items, list) else 0
        except Exception:
            continue
    return None, 0


def configure_environment(root: Path, settings: dict) -> tuple[int, Path]:
    data_default = exe_dir() / "data"
    fallback = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())) / "TouxianCampus" / "data"
    data_dir = Path(os.environ["CAMPUS_DATA_DIR"]) if os.environ.get("CAMPUS_DATA_DIR") else (
        data_default if writable_dir(data_default) else fallback
    )
    data_dir.mkdir(parents=True, exist_ok=True)

    port = pick_port(int(settings.get("port") or 8765))
    origins = ["http://127.0.0.1:" + str(port), "http://localhost:" + str(port)]

    os.environ.setdefault("CAMPUS_ENV", "development")
    os.environ.setdefault("CAMPUS_DEV_AUTH", "true")
    os.environ.setdefault("CAMPUS_ADMIN_EMAILS", str(settings.get("admin_email") or "operator@touxian.local"))
    os.environ.setdefault("CAMPUS_SOURCE_REFRESH_SECONDS", "21600")
    os.environ.setdefault("CAMPUS_PUBLIC_API_DOCS", "false")
    os.environ.setdefault("CAMPUS_EMBEDDED_WORKER", "true")
    os.environ["CAMPUS_DATA_DIR"] = str(data_dir)
    merge_env("CAMPUS_ALLOWED_ORIGINS", origins)

    trusted = config_origins(root) + [str(item) for item in settings.get("trusted_origins") or []]
    feeds = feed_entries(settings)
    detected = None
    if not feeds and settings.get("wechat_feed_auto", True):
        # 本机 WeWe RSS 起了服务且已有订阅内容时自动接上：用户不需要手改配置。
        detected_url, item_count = local_wewe_feed()
        if detected_url and item_count > 0:
            detected = detected_url
            feeds = ["微信公众号（本机 WeWe RSS）=" + detected]
        elif detected_url:
            print(
                "检测到本机 WeWe RSS，但还没有订阅内容；"
                "请在 http://127.0.0.1:4000/ 扫码登录并添加公众号后重启本程序。",
                flush=True,
            )
    for entry in feeds:
        from urllib.parse import urlsplit

        parsed = urlsplit(entry.split("=", 1)[1])
        if parsed.scheme and parsed.netloc:
            trusted.append(f"{parsed.scheme}://{parsed.netloc}")
    merge_env("CAMPUS_TRUSTED_GATEWAY_ORIGINS", trusted)

    if (settings.get("wechat_feed_enabled") or detected) and feeds:
        os.environ["CAMPUS_WECHAT_FEED_ENABLED"] = "true"
        os.environ["CAMPUS_WECHAT_FEEDS"] = ";".join(feeds)
    if detected:
        print("已自动接上本机 WeWe RSS：" + detected, flush=True)
    return port, data_dir


def open_browser_when_ready(url: str, timeout_seconds: float = 90.0) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    webbrowser.open(url)
                    return
        except Exception:
            time.sleep(0.5)
    print("提示：服务已启动，但没等到 200；请手动打开 " + url, flush=True)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if is_frozen() and argv[:1] == ["-c"] and len(argv) >= 2:
        # 冻结后 sys.executable 就是本 exe：产品里的 PDF 解析会用
        # ``subprocess.run([sys.executable, "-c", script])`` 起子进程。子进程必须
        # 原样执行那段脚本（只用 stdlib + pypdf，已打包），而不是再起一个服务实例。
        exec(compile(argv[1], "<frozen-subprocess>", "exec"), {"__name__": "__main__"})
        return 0
    if sys.platform == "win32":
        try:
            os.system("chcp 65001 >nul")
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
    root = bundle_root()
    base = exe_dir()
    if "--diag-parse" in argv:
        # 冻结环境诊断：用产品自己的解析器解析一个快照 blob，打印真实异常。
        import traceback
        from pathlib import Path as _Path

        index = argv.index("--diag-parse")
        blob = _Path(argv[index + 1]).read_bytes()
        url = argv[index + 2] if len(argv) > index + 2 else "https://mp.weixin.qq.com/s/diag"
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from server.campus.content import html_body_segments

        try:
            segments, state, warnings = html_body_segments(blob, url)
            print("ok", state, warnings, len(segments))
            for segment in segments[:3]:
                print("segment:", segment["id"], segment["text"][:40])
        except Exception:
            traceback.print_exc()
        return 0
    if "--diag-worker" in argv:
        # 冻结环境诊断：对指定数据目录跑一次 worker.run_once，打印真实异常。
        import traceback
        from pathlib import Path as _Path

        index = argv.index("--diag-worker")
        data_dir = _Path(argv[index + 1]).resolve() if len(argv) > index + 1 else (exe_dir() / "data")
        os.environ["CAMPUS_DATA_DIR"] = str(data_dir)
        os.environ.setdefault("CAMPUS_ENV", "development")
        os.environ.setdefault("CAMPUS_DEV_AUTH", "true")
        os.environ.setdefault("CAMPUS_ADMIN_EMAILS", "operator@touxian.local")
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from server.campus.platform import Settings
        from server.campus.worker import Worker
        from server.campus_app import assemble

        core = assemble(Settings.from_env())
        try:
            print("run_once:", Worker(core).run_once(max_jobs=3))
        except Exception:
            traceback.print_exc()
        finally:
            core.store.close()
        return 0
    settings = load_settings(base)
    if "--port" in argv:
        settings["port"] = int(argv[argv.index("--port") + 1])
    no_browser = "--no-browser" in argv

    port, data_dir = configure_environment(root, settings)
    if not sys.path or str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from server.campus.bootstrap import materialize_frontend
    from server.campus.platform import Settings
    from server.campus_app import assemble, create_app

    settings_obj = Settings.from_env()
    materialize_frontend(root)
    core = assemble(settings_obj)
    try:
        # seed() 是幂等的：只补缺失的来源。必须每次启动都跑一次，
        # 否则用户在 WeWe RSS 里新订阅公众号后重启，公众号来源不会被补注册。
        core.pipeline.sources.seed()
        print("已注册/补齐配置中的官方来源与公众号 feed 来源", flush=True)
    finally:
        core.store.close()

    application = create_app(settings_obj)
    url = f"http://127.0.0.1:{port}/"
    print("", flush=True)
    print("偷闲·天大版 桌面版", flush=True)
    print("  数据目录：" + str(data_dir), flush=True)
    print("  登录邮箱：" + os.environ.get("CAMPUS_ADMIN_EMAILS", "operator@touxian.local") + "（开发登录码）", flush=True)
    print("  访问地址：" + url, flush=True)
    print("  公众号 feed：" + ("已启用（微信读书路线）" if os.environ.get("CAMPUS_WECHAT_FEEDS") else "未启用（官方站点照常采集）"), flush=True)
    print("  首次启动会自动采集，约 1-3 分钟；关闭本窗口即停止服务。", flush=True)
    print("", flush=True)
    if not no_browser:
        threading.Thread(target=open_browser_when_ready, args=(url,), name="open-browser", daemon=True).start()

    import uvicorn

    uvicorn.run(application, host="127.0.0.1", port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
