"""t30：开启微信读书路线的本机联调运行器（显式开启，默认关闭）。

它只做三件事，然后调用产品自己的日常更新命令：

  1. 把本机 feed 地址写进 `CAMPUS_WECHAT_FEEDS`（名称=URL，分号分隔，可多个公众号）；
  2. 设置 `CAMPUS_WECHAT_FEED_ENABLED=1`（**不设这个，产品完全不会注册这类来源**）；
  3. 把「config 里的官方 origin」**加上**本机回环 origin，一起放进
     `CAMPUS_TRUSTED_GATEWAY_ORIGINS`——因为 `daily_update --gateway-from-config` 在环境变量
     已经有值时不会覆盖它，而 fake-IP 场景下两者都必须显式受信（第 18/29 轮的结论）。

产品这一侧没有任何微信凭证：feed 由你自己跑的第三方工具产生，这里只是把地址告诉它。

    python verification/wechat/run_local_wxfeed.py --data-dir <副本> --apply \\
        --feed "天津大学教务处=http://127.0.0.1:8791/feed-tju-oaa.json" [-- 传给 daily_update 的其它参数]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))


def main(argv: list[str]) -> int:
    from campus_collect import gateway_origins_from_config

    parser = argparse.ArgumentParser(prog="run_local_wxfeed.py", add_help=True)
    parser.add_argument("--feed", action="append", required=True, help="名称=URL，可重复")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--today")
    parser.add_argument("--json-out")
    parser.add_argument("--extra", nargs=argparse.REMAINDER, help="余下参数原样传给 daily_update")
    args = parser.parse_args(argv[1:])

    config_origins = list(gateway_origins_from_config() or [])
    loopback = []
    for item in args.feed:
        _, _, url = item.partition("=")
        if url.startswith(("http://127.0.0.1", "http://localhost")):
            parts = url.split("/")
            loopback.append(parts[0] + "//" + parts[2])
    origin_list = config_origins + [origin for origin in loopback if origin not in config_origins]

    env = dict(os.environ)
    env["CAMPUS_WECHAT_FEED_ENABLED"] = "1"
    env["CAMPUS_WECHAT_FEEDS"] = ";".join(args.feed)
    env["CAMPUS_TRUSTED_GATEWAY_ORIGINS"] = ",".join(origin_list)
    env.setdefault("PYTHONUTF8", "1")

    command = [sys.executable, str(REPO / "scripts" / "daily_update.py"), "--data-dir", args.data_dir]
    if args.apply:
        command.append("--apply")
    if args.today:
        command += ["--today", args.today]
    if args.json_out:
        command += ["--json-out", args.json_out]
    command += list(args.extra or [])
    print(f"微信读书路线：{'开' if env['CAMPUS_WECHAT_FEED_ENABLED'] == '1' else '关'}"
          f" · feed {len(args.feed)} 个 · 受信 origin {len(origin_list)} 个（含回环 {len(loopback)}）")
    print("命令:", " ".join(command[1:]))
    result = subprocess.run(command, env=env, cwd=str(REPO))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))