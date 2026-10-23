"""t23：把 daily_update 的 JSON 摘要里真正关心的数字打出来。

    python verification/extraction/daily_stages.py <daily-after.json>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def walk(node, prefix=""):
    if isinstance(node, dict):
        for key, value in node.items():
            walk(value, f"{prefix}{key}.")
    elif isinstance(node, list) and node and not isinstance(node[0], (dict, list)):
        print(f"{prefix[:-1]}: {node}")
    elif isinstance(node, (str, int, float, bool)) or node is None:
        print(f"{prefix[:-1]}: {node}")
    elif isinstance(node, list):
        print(f"{prefix[:-1]}: {len(node)} 项")


def main(argv: list[str]) -> int:
    payload = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    stages = payload.get("stages") or {}
    print("命令:", payload.get("command"), "退出码:", payload.get("exit_code"), "今天:", payload.get("today"))
    for name, stage in stages.items():
        print(f"\n=== {name} ===")
        walk(stage)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))