"""Field evaluation on short official excerpts, never a whole-notice accuracy score."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.campus.content import EXTRACTOR_VERSION, date_values, material_values


def evaluate(corpus):
    rows = []
    totals = Counter()
    for item in corpus["items"]:
        snippet = item["snippet"]
        dates = date_values(snippet, item.get("published_at"), "00000000-0000-4000-8000-000000000001")
        # Unknown dates are review signals; compare absolute-date fields separately.
        observed_dates = Counter(
            (date["date"] or (date["at"] or "")[:10], "event_time" if date["kind"] == "event_time" else "deadline")
            for date in dates
            if date["precision"] != "unknown"
        )
        expected_dates = Counter(
            (date["iso_date"], date["kind"])
            for date in item["expected"]["date_mentions"]
            if date["iso_date"] is not None
        )
        observed_materials = set(
            value for sentence in re.split(r"[。；\n]", snippet) for value in material_values(sentence)
        )
        expected_materials = set(item["expected"]["materials"])
        true_dates = sum((observed_dates & expected_dates).values())
        false_dates = sum((observed_dates - expected_dates).values())
        missed_dates = sum((expected_dates - observed_dates).values())
        true_materials = len(observed_materials & expected_materials)
        false_materials = len(observed_materials - expected_materials)
        missed_materials = len(expected_materials - observed_materials)
        # A labelled unknown year must never become an absolute calendar date.
        unknown_expected = any(date["iso_date"] is None for date in item["expected"]["date_mentions"])
        unknown_preserved = not unknown_expected or not observed_dates
        totals.update(
            date_true=true_dates,
            date_false=false_dates,
            date_missed=missed_dates,
            material_true=true_materials,
            material_false=false_materials,
            material_missed=missed_materials,
        )
        exact = not (false_dates or missed_dates or false_materials or missed_materials) and unknown_preserved
        totals.update(
            items=1,
            exact_items=int(exact),
            unknown_cases=int(unknown_expected),
            unknown_cases_preserved=int(unknown_expected and unknown_preserved),
        )
        rows.append(
            {
                "id": item["id"],
                "source_url": item["source_url"],
                "exact_fields": exact,
                "expected_absolute_dates": [
                    {"date": value, "kind": kind, "count": count} for (value, kind), count in expected_dates.items()
                ],
                "observed_absolute_dates": [
                    {"date": value, "kind": kind, "count": count} for (value, kind), count in observed_dates.items()
                ],
                "expected_materials": sorted(expected_materials),
                "observed_materials": sorted(observed_materials),
                "unknown_year_preserved": unknown_preserved,
                "review_signals": [date["raw_text"] for date in dates if date["precision"] == "unknown"],
            }
        )
    fingerprint = hashlib.sha256(
        json.dumps(corpus, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "extractor_version": EXTRACTOR_VERSION,
        "corpus_verified_on": corpus["verified_on"],
        "corpus_fingerprint_sha256": fingerprint,
        "scope": "short_official_excerpt_fields",
        "limits": [
            "Hand-picked excerpts, not a random or held-out test set.",
            "Absolute date and role are compared; this does not evaluate exact clocks, audience, full notices, live ingestion, or student time saved.",
            "The expected requires_review flag describes missing excerpt context and is not scored as an automatic candidate state.",
        ],
        "totals": dict(totals),
        "items": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "corpus", nargs="?", type=Path, default=ROOT / "fixtures" / "notice-corpus" / "official-notices.json"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = evaluate(json.loads(args.corpus.read_text(encoding="utf-8")))
    output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    print(json.dumps(result["totals"], ensure_ascii=False))


if __name__ == "__main__":
    main()
