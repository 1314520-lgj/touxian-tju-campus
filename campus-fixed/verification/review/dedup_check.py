"""t25：分组去重的对照数字与「一条候选都不能少」的不变量（只读真实 data-operator）。

    python verification/review/dedup_check.py [数据目录]
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str]) -> int:
    data_dir = Path(argv[1]).resolve() if len(argv) > 1 else REPO / "data-operator"
    review_queue = load("review_queue")
    daily = review_queue.daily
    collect = review_queue.collect

    import os

    os.environ.setdefault("PYTHONUTF8", "1")
    restore = daily.prepare_readonly_secret(data_dir)
    core = collect.build_core(data_dir, timeout=20)
    try:
        today = date.fromisoformat(argv[2]) if len(argv) > 2 else date(2026, 10, 21)
        views = review_queue.pending_views(core, today)
        groups = review_queue.group_views(review_queue.sort_views(views, "priority"))
        ids = [view["candidate_id"] for view in views]
        grouped = [member["candidate_id"] for group in groups for member in group["members"]]
        title_only = len({review_queue.normalize_title(view["title"]) for view in views})
        duplicates = [group for group in groups if len(group["members"]) > 1]
        print(f"数据目录 {data_dir.name} · 今天 {today}")
        print(f"待人工判断 {len(views)} 条")
        print(f"按「标题 + 期限集合」分组：{len(groups)} 组（合并掉 {len(views) - len(groups)} 条）")
        print(f"只按标题去重：{title_only} 组")
        print(f"重复组 {len(duplicates)} 个，涉及 {sum(len(g['members']) for g in duplicates)} 条；最大一组 {max((len(g['members']) for g in groups), default=0)} 条")
        print("\n最重复的 5 组：")
        for group in sorted(duplicates, key=lambda item: -len(item["members"]))[:5]:
            sources = "、".join(group["sources"])
            print(f"  ×{len(group['members'])} «{group['representative']['title'][:44]}»")
            print(f"      来源：{sources}")
            print(f"      组内候选：{', '.join(group['member_ids'])}")
            print(f"      快照 {len(group['snapshots'])} 份 · 期限 {group['representative']['next_deadline'] or '无'}")

        print("\n不变量：")
        checks = [
            ("候选总数不变", len(grouped) == len(ids)),
            ("组内候选 id 无重复", len(set(grouped)) == len(grouped)),
            ("分组覆盖全部候选", set(grouped) == set(ids)),
            ("每组都有代表项与成员", all(group["representative"] in group["members"] for group in groups)),
            ("决策目标仍是候选 id（未被合成 id 取代）", all(any(member["candidate_id"] == cid for group in groups for member in group["members"]) for cid in ids[:20])),
        ]
        for label, ok in checks:
            print(f"  {'✔' if ok else '✘'} {label}")
        if not all(ok for _, ok in checks):
            return 1
        return 0
    finally:
        core.store.close()
        restore()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))