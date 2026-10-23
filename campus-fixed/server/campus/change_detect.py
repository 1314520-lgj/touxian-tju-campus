"""Block-level change detection between two captures of the same document.

Whole-document hashing already deduplicates byte-identical captures in
``sources.persist``. This module answers the different question a student
actually asks: *what changed* since the last time this notice was read.

The module is deliberately side-effect free and never guesses. It reports only
text that is literally present in one capture and absent from the other, so a
"change summary" can never claim an edit that did not happen.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import unicodedata

MAX_BLOCKS = 2000
MAX_BLOCK_CHARS = 2000
MAX_REPORTED = 40

_WHITESPACE = re.compile(r"\s+")
_WORDLIKE = re.compile(r"[\w\u4e00-\u9fff]")
_DATE_LIKE = re.compile(r"(?:20\d{2}\s*[年./-]|\d{1,2}\s*月\s*\d{1,2}|截止|截至|报名|提交|时间|日期)")


def clean_text(value):
    """Display text: whitespace collapsed only, so punctuation stays literal."""
    text = str(value or "").replace("\u3000", " ")
    return _WHITESPACE.sub(" ", text).strip()


def normalize_text(value):
    """Comparison key: width-folded and whitespace-collapsed.

    Used only for identity and matching. Reported text always comes from
    :func:`clean_text`, so a summary never quotes a character the source did
    not contain.
    """
    folded = unicodedata.normalize("NFKC", str(value or "")).replace("\u3000", " ")
    return _WHITESPACE.sub(" ", folded).strip()


def fingerprint(value):
    """Stable identity of a capture's text, independent of incidental whitespace."""
    return hashlib.sha256(normalize_text(value).encode("utf-8")).hexdigest()


def blocks(value):
    """Split a capture into comparable blocks, dropping punctuation-only noise.

    Blocks stay in document order; duplicates are preserved so that a notice
    which genuinely repeats a line is not reported as unchanged.
    """
    result = []
    for line in re.split(r"[\r\n]+", str(value or "")):
        line = clean_text(line)
        if not line or not _WORDLIKE.search(line):
            continue
        result.append(line[:MAX_BLOCK_CHARS])
        if len(result) >= MAX_BLOCKS:
            break
    return result


def _keys(values):
    return [normalize_text(value) for value in values]


def _bounded(values, limit=MAX_REPORTED):
    return list(values[:limit])


def diff(old_value, new_value):
    """Compare two captures block by block.

    ``rewritten`` pairs a replaced block with its successor only when the two
    occupy the same position in the sequence, which keeps an insertion from
    being mislabelled as an edit.
    """
    old_blocks = blocks(old_value)
    new_blocks = blocks(new_value)
    matcher = difflib.SequenceMatcher(a=_keys(old_blocks), b=_keys(new_blocks), autojunk=False)
    added, removed, rewritten, unchanged = [], [], [], 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            unchanged += i2 - i1
        elif tag == "insert":
            added.extend(new_blocks[j1:j2])
        elif tag == "delete":
            removed.extend(old_blocks[i1:i2])
        else:
            pairs = min(i2 - i1, j2 - j1)
            for offset in range(pairs):
                rewritten.append({"before": old_blocks[i1 + offset], "after": new_blocks[j1 + offset]})
            removed.extend(old_blocks[i1 + pairs : i2])
            added.extend(new_blocks[j1 + pairs : j2])
    report = {
        "changed": bool(added or removed or rewritten),
        "old_fingerprint": fingerprint(old_value),
        "new_fingerprint": fingerprint(new_value),
        "added": _bounded(added),
        "removed": _bounded(removed),
        "rewritten": _bounded(rewritten),
        "counts": {
            "added": len(added),
            "removed": len(removed),
            "rewritten": len(rewritten),
            "unchanged": unchanged,
        },
    }
    report["truncated"] = any(count > MAX_REPORTED for count in (len(added), len(removed), len(rewritten)))
    report["kinds"] = classify(report)
    return report


def classify(report):
    """Tag a change by what it touches, without deciding whether it matters.

    A date moving is treated separately from prose because the product promises
    students a "re-check" prompt when a deadline moves, and nothing when only
    wording was tidied.
    """
    touched = list(report.get("added", [])) + list(report.get("removed", []))
    touched += [item["after"] for item in report.get("rewritten", [])]
    touched += [item["before"] for item in report.get("rewritten", [])]
    kinds = []
    if any(_DATE_LIKE.search(block) for block in touched):
        kinds.append("deadline_or_date_touched")
    if report.get("added"):
        kinds.append("content_added")
    if report.get("removed"):
        kinds.append("content_removed")
    if report.get("rewritten") and not report.get("added") and not report.get("removed"):
        kinds.append("wording_revised")
    return kinds


def summarize(report, limit=2):
    """Short Chinese summary for a student-facing re-check note.

    Returns ``None`` when nothing changed, so callers never render an empty
    change note as if an edit had occurred.
    """
    if not report.get("changed"):
        return None
    counts = report["counts"]
    parts = []
    if counts["added"]:
        parts.append(f"新增 {counts['added']} 段")
    if counts["removed"]:
        parts.append(f"删除 {counts['removed']} 段")
    if counts["rewritten"]:
        parts.append(f"改写 {counts['rewritten']} 段")
    headline = "、".join(parts) or "内容有变化"
    if "deadline_or_date_touched" in report.get("kinds", []):
        headline = "时间或期限可能有变化：" + headline
    samples = report.get("added") or [item["after"] for item in report.get("rewritten", [])]
    if samples:
        excerpt = "；".join(block[:60] for block in samples[:limit])
        headline = f"{headline}。新增内容：{excerpt}"
    return headline[:400]


def compare(old_value, new_value):
    """Convenience wrapper returning both the report and its summary."""
    report = diff(old_value, new_value)
    return report, summarize(report)
