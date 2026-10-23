"""t23 取样：队列里「正文有日期串、候选却没有期限」的候选逐条对照。

分类（决定后面怎么修）：
  A 有明确期限 cue（截止/请于/于…前/…前提交）却没产出期限 —— 抽取 bug
  B 只有「日期(+时刻)前」这种写法（cue 表里没有）—— 本轮目标规律
  C 正文只有裸日期串，没有任何 cue —— 必须继续不确定（红线）

每条样例都带原文逐字引文。

    python verification/extraction/sample_missing_deadline.py
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
DB = REPO / "data-operator" / "campus.sqlite3"
OUT = REPO / "verification" / "extraction"

DATE_ANY = re.compile(r"(?:\d{2,4}\s*年\s*)?\d{1,2}\s*月\s*\d{1,2}\s*[日号]")
# 「日期(+时刻)前」——本轮要认的写法（今天的 cue 表里没有）
DATE_THEN_BEFORE = re.compile(
    r"(?:\d{2,4}\s*年\s*)?\d{1,2}\s*月\s*\d{1,2}\s*[日号]"
    r"(?:\s*\d{1,2}\s*[:：]\s*\d{2})?\s*(?:之)?前(?!后|期|夕|往|来|者|年|天|日|提|完|的)"
)
SENTENCE = re.compile(r"[^。；;！？\n]+")


def sentences(text):
    return [match.group(0).strip() for match in SENTENCE.finditer(text)]


def load_texts(connection):
    cursor = connection.cursor()
    cursor.execute("select key, data from campus_records where collection='parsed'")
    parsed = {}
    for key, data in cursor.fetchall():
        record = json.loads(data)
        parsed[record["snapshot_id"]] = record
    return parsed


def main() -> int:
    sys.path.insert(0, str(REPO / "scripts"))
    from server.campus.content import BEFORE_DATE_CUE, DATE_CUE, DEADLINE_CUE

    connection = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cursor = connection.cursor()
    cursor.execute("select key, data from campus_records where collection='candidates'")
    candidates = [json.loads(data) for _, data in cursor.fetchall()]
    parsed = load_texts(connection)

    pending = [c for c in candidates if c.get("review_status") == "pending"]
    no_deadline = [c for c in pending if not c.get("deadlines")]
    print(f"候选 {len(candidates)} · pending {len(pending)} · pending 且无期限 {len(no_deadline)}")

    buckets = Counter()
    cases = []
    for candidate in no_deadline:
        snapshots = candidate.get("snapshot_ids") or []
        text = ""
        for snapshot_id in snapshots:
            record = parsed.get(snapshot_id)
            if not record:
                continue
            text = "\n".join(segment["text"] for segment in record.get("segments") or [])
            break
        if not text:
            buckets["无正文可查"] += 1
            continue
        hits = []
        for sentence in sentences(text):
            if not DATE_ANY.search(sentence):
                continue
            deadline_cue = bool(re.search(DEADLINE_CUE, sentence))
            date_cue = bool(re.search(DATE_CUE, sentence))
            before_date_cue = bool(re.search(BEFORE_DATE_CUE, sentence))
            trailing = bool(DATE_THEN_BEFORE.search(sentence))
            if trailing and not deadline_cue and not date_cue:
                kind = "B 只有「日期前」写法"
            elif trailing and not deadline_cue:
                kind = "B+ 有普通日期 cue、无期限 cue，另有「日期前」"
            elif deadline_cue:
                kind = "A 有期限 cue 却没抽出来"
            elif date_cue:
                kind = "D 只有排期/时间 cue"
            else:
                kind = "C 裸日期串（必须保持不确定）"
            hits.append(
                {
                    "kind": kind,
                    "quote": sentence[:200],
                    "deadline_cue": deadline_cue,
                    "date_cue": date_cue,
                    "before_date_cue": before_date_cue,
                    "trailing_before": trailing,
                }
            )
        if not hits:
            buckets["正文没有日期串"] += 1
            continue
        best = next((h for h in hits if h["kind"].startswith("B")), None)
        best = best or next((h for h in hits if h["kind"].startswith("A")), None)
        best = best or hits[0]
        buckets[best["kind"]] += 1
        cases.append(
            {
                "candidate_id": candidate.get("candidate_id"),
                "title": candidate.get("title"),
                "review_status": candidate.get("review_status"),
                "warnings": candidate.get("warnings"),
                "kind": best["kind"],
                "snapshot_ids": snapshots,
                "hits": hits[:6],
            }
        )

    print("\n分类计数:", dict(buckets))
    for kind in sorted({case["kind"] for case in cases}):
        print(f"\n=== {kind} ===")
        shown = 0
        for case in cases:
            if case["kind"] != kind or shown >= 6:
                continue
            shown += 1
            hit = next(h for h in case["hits"] if h["kind"] == kind)
            print(f"· «{case['title'][:48]}»")
            print(f"  引文: {hit['quote'][:160]}")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "sample-missing-deadline.json").write_text(
        json.dumps({"counts": dict(buckets), "cases": cases}, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print("\nwrote verification/extraction/sample-missing-deadline.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())