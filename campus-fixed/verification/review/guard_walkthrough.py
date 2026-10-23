"""t25：守卫不能因为分组而放松——在副本上把每条守卫跑一遍。

场景（全部在 data-operator 的副本上，真实目录只读）：
  1. --group 但没 --reason            → 必须拒绝（退出码 2），不写任何东西
  2. --group … --apply 但没有 --yes   → 只打印将影响的候选 id 列表，退出码 2，不写
  3. 组大小 > --max-batch             → 拒绝（退出码 2）
  4. --yes --apply（组大小合规）      → 逐条写入 reviews，候选/快照总数不变
  5. 不带条件的「批量目标」           → 拒绝（没有「全部接受」入口）

    python verification/review/guard_walkthrough.py
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
REAL = REPO / "data-operator"
WORK = REPO.parent / "_t25-verify"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def counts(db: Path):
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out = {}
    for collection in ("candidates", "snapshots", "reviews"):
        out[collection] = connection.execute(
            "select count(*) from campus_records where collection=?", (collection,)
        ).fetchone()[0]
    out["pending"] = connection.execute(
        "select count(*) from campus_records where collection='candidates' and json_extract(data,'$.review_status')='pending'"
    ).fetchone()[0]
    connection.close()
    return out


def run(args, cwd=REPO):
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "review_queue.py"), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**__import__("os").environ, "PYTHONUTF8": "1"},
    )
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def main() -> int:
    review_queue = load("review_queue")
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    copy = WORK / "operator-data"
    shutil.copytree(REAL, copy)

    # Pick the biggest group as the walkthrough subject.
    collect = review_queue.collect
    daily = review_queue.daily
    restore = daily.prepare_readonly_secret(copy)
    core = collect.build_core(copy, timeout=20)
    try:
        from datetime import date

        views = review_queue.pending_views(core, date(2026, 10, 21))
        groups = review_queue.group_views(review_queue.sort_views(views, "priority"))
    finally:
        core.store.close()
        restore()
    subject = max(groups, key=lambda group: len(group["members"]))
    anchor = subject["representative"]["candidate_id"]
    print(f"对照组：{len(subject['members'])} 条 · «{subject['representative']['title'][:40]}»")
    print(f"组内候选：{', '.join(subject['member_ids'])}\n")

    before = counts(copy / "campus.sqlite3")
    steps = [
        ("1 缺 --reason", ["--data-dir", str(copy), "--group", anchor, "--accept", "--apply"], 2),
        ("2 缺 --yes（只看计划）", ["--data-dir", str(copy), "--group", anchor, "--accept", "--reason", "t25 守卫演练", "--apply"], 2),
        ("3 超过 --max-batch", ["--data-dir", str(copy), "--group", anchor, "--accept", "--reason", "t25 守卫演练", "--yes", "--apply", "--max-batch", "1"], 2),
        ("5 无条件的批量目标", ["--data-dir", str(copy), "--accept", "--reason", "t25 守卫演练", "--yes", "--apply"], 2),
    ]
    failures = 0
    for label, args, expected in steps:
        code, output = run(args)
        ok = code == expected
        failures += not ok
        print(f"[{label}] 退出码 {code}（期望 {expected}）{'✔' if ok else '✘'}")
        for line in [row for row in output.splitlines() if row.strip()][:3]:
            print(f"      {line[:120]}")
        after = counts(copy / "campus.sqlite3")
        if after != before:
            failures += 1
            print(f"      ✘ 被拒绝的场景竟然改了数据：{before} → {after}")
    print(f"拒绝场景后仍与之前一致：{counts(copy / 'campus.sqlite3') == before}")

    print("\n[4] 合规的整组写入（--yes --apply --max-batch 25）")
    code, output = run(
        ["--data-dir", str(copy), "--group", anchor, "--accept", "--reason", "t25 守卫演练：整组接受", "--yes", "--apply"]
    )
    print(f"    退出码 {code}（期望 0）")
    for line in output.splitlines():
        if line.strip() and ("已写入审计" in line or "批量完成" in line or "整组将影响" in line or "计划" in line):
            print(f"      {line[:130]}")
    after = counts(copy / "campus.sqlite3")
    print(f"    候选 {before['candidates']} → {after['candidates']} · 快照 {before['snapshots']} → {after['snapshots']} · "
          f"审计 {before['reviews']} → {after['reviews']}（应 +{len(subject['members'])}）")
    print(f"    待人工判断 {before['pending']} → {after['pending']}（应 -{len(subject['members'])}）")
    checks = {
        "候选总数未变": after["candidates"] == before["candidates"],
        "快照总数未变": after["snapshots"] == before["snapshots"],
        "审计恰好增加组内条数": after["reviews"] == before["reviews"] + len(subject["members"]),
        "队列恰好减少组内条数": after["pending"] == before["pending"] - len(subject["members"]),
        "成功退出码": code == 0,
    }
    for label, ok in checks.items():
        print(f"    {'✔' if ok else '✘'} {label}")
        failures += not ok
    shutil.rmtree(WORK)
    print(f"\n结论：{'全部守卫如期生效，且没有任何候选/快照被丢弃' if not failures else f'{failures} 项不符合预期'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())