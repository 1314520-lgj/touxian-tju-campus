#!/usr/bin/env python3
"""Mask personal contact details in the shipped public sample (format-preserving).

    python tools/mask_public_contacts.py --root . [--dry-run]

What is masked
  * mainland mobile numbers  13812345678 / 138 1234 5678 / 138-1234-5678
        -> 138****5678 / 138 **** 5678 / 138-****-5678   (first 3 + last 4 kept)
  * e-mail addresses         alice@tju.edu.cn -> a***@tju.edu.cn  (domain kept)
        (demo / test / example addresses used by the product itself are kept)

What is deliberately kept
  * department landlines (022-xxxxxxxx ...): office lines, not personal.
  * QQ *group* numbers and the public WeChat official-account id: they are
    public group / account identifiers, not an individual's contact.
  * Person names (out of scope for this tool).

Where it works
  * data/campus.sqlite3 and deploy/snapshot/*.sqlite3: every string inside every
    JSON record, plus the full-text index (campus_segment_fts).
  * data/snapshots/**/*.bin that are UTF-8 text (HTML / plain text). Snapshots
    are content-addressed (<sha256>.bin and snapshots.content_hash), so a masked
    file gets a new hash; the file is renamed and every record that carries the
    old hash (snapshots, parsed, evidence, candidates, events ...) is rewritten.
    Binary office files (.docx/.xlsx/.doc/.xls) and images are scanned and left
    untouched when they contain no hit.
  * deploy/operator/staging/**: raw text replacement.
  * data/PUBLIC_SAMPLE.json: database_sha256 refreshed, masking flag added.

Afterwards run  python tools/build_release_manifest.py --root .  to refresh
RELEASE_MANIFEST.json. The tool is idempotent; the report holds counts only,
never the original values.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import sqlite3
import sys
import zipfile
import io
from pathlib import Path

MOBILE = re.compile(r"(?<![0-9A-Za-z])(1[3-9]\d)([ \-]?)(\d{4})([ \-]?)(\d{4})(?![0-9A-Za-z])")
EMAIL = re.compile(r"(?<![A-Za-z0-9._%+\-])([A-Za-z0-9._%+\-]+)@([A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+)")
KEEP_EMAIL_DOMAINS = ("example.com", "example.org", "example.net", ".example", ".local", ".invalid")
KEEP_EMAIL_LOCALS = {"demo-admin"}
LANDLINE = re.compile(r"(?<!\d)0\d{2,3}[-\s]?\d{7,8}(?!\d)")
QQ_GROUP = re.compile(r"QQ群(?:号)?[:：]?\s*\d{5,12}", re.I)
SHA = re.compile(r"[0-9a-f]{64}")


class Counter:
    def __init__(self):
        self.n = collections.Counter()
        self.distinct = collections.defaultdict(set)

    def add(self, kind, value):
        self.n[kind] += 1
        self.distinct[kind].add(hashlib.sha256(value.encode()).hexdigest())


def mask_text(text, counter=None):
    def mobile(m):
        if counter is not None:
            counter.add("mobile", re.sub(r"\D", "", m.group(0)))
        return f"{m.group(1)}{m.group(2)}****{m.group(4)}{m.group(5)}"

    def email(m):
        local, domain = m.group(1), m.group(2)
        low = domain.lower()
        if local.lower() in KEEP_EMAIL_LOCALS or low.endswith(KEEP_EMAIL_DOMAINS):
            return m.group(0)
        if counter is not None:
            counter.add("email", m.group(0).lower())
        return f"{local[0]}***@{domain}"

    return EMAIL.sub(email, MOBILE.sub(mobile, text))


# A mobile number whose digits are interleaved with HTML tags (<span> per font run).
TAG = r"(?:<[^>]*>)"
SPLIT_MOBILE = re.compile(r"(?<![0-9A-Za-z])1[3-9](?:" + TAG + r"*\d){9}(?![0-9A-Za-z])")


def mask_html(text, counter=None):
    def split_mobile(m):
        s = m.group(0)
        if "<" not in s:
            return s
        digits, out, i = re.sub(r"<[^>]*>", "", s), [], 0
        if counter is not None:
            counter.add("mobile", digits)
        pos = 0
        for part in re.split(r"(<[^>]*>)", s):
            if part.startswith("<"):
                out.append(part)
            else:
                chunk = []
                for ch in part:
                    chunk.append("*" if 3 <= pos <= 6 else ch)
                    pos += 1
                out.append("".join(chunk))
        return "".join(out)

    return mask_text(mask_split_emails(SPLIT_MOBILE.sub(split_mobile, text), counter), counter)


def mask_split_emails(text, counter=None):
    """Mask e-mails whose characters are interleaved with HTML tags."""
    if "<" not in text:
        return text
    idx, stripped, last = [], [], 0
    for tag in re.finditer(r"<[^>]*>", text):
        for i in range(last, tag.start()):
            idx.append(i)
            stripped.append(text[i])
        last = tag.end()
    for i in range(last, len(text)):
        idx.append(i)
        stripped.append(text[i])
    stripped = "".join(stripped)
    edits = {}
    for m in EMAIL.finditer(stripped):
        local, domain = m.group(1), m.group(2)
        if local.lower() in KEEP_EMAIL_LOCALS or domain.lower().endswith(KEEP_EMAIL_DOMAINS):
            continue
        start, end = idx[m.start()], idx[m.end() - 1]
        if "<" not in text[start:end]:
            continue  # contiguous: handled by mask_text
        if counter is not None:
            counter.add("email", m.group(0).lower())
        local_pos = idx[m.start() + 1 : m.start() + len(local)]
        for n, pos in enumerate(local_pos):
            edits[pos] = "***" if n == 0 else ""
    if not edits:
        return text
    return "".join(edits.get(i, ch) for i, ch in enumerate(text))


def mask_json(value, counter):
    if isinstance(value, str):
        return mask_text(value, counter)
    if isinstance(value, list):
        return [mask_json(v, counter) for v in value]
    if isinstance(value, dict):
        return {k: mask_json(v, counter) for k, v in value.items()}
    return value


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def report_add(report, bucket, counter):
    if not counter.n:
        return
    entry = report.setdefault(bucket, {"occurrences": {}, "distinct": {}})
    for k, v in counter.n.items():
        entry["occurrences"][k] = entry["occurrences"].get(k, 0) + v
    for k, v in counter.distinct.items():
        entry["distinct"][k] = entry["distinct"].get(k, 0) + len(v)


def mask_db(path, hash_map, report, label, dry):
    con = sqlite3.connect(path)
    rows = con.execute("select collection, key, data from campus_records").fetchall()
    updates = []
    for collection, key, data in rows:
        c = Counter()
        text = data
        for old, new in hash_map.items():
            if old in text:
                text = text.replace(old, new)
        obj = mask_json(json.loads(text), c)
        new_data = json.dumps(obj)  # same serialisation SQLAlchemy used (ensure_ascii, default separators)
        if new_data != data:
            updates.append((new_data, collection, key))
        report_add(report, f"{label}::records::{collection}", c)
    fts = Counter()
    fts_updates = []
    has_fts = con.execute("select 1 from sqlite_master where name='campus_segment_fts'").fetchone()
    if has_fts:
        for rowid, title, body in con.execute("select rowid, title, body from campus_segment_fts").fetchall():
            c = Counter()
            t2, b2 = mask_text(title or "", c), mask_text(body or "", c)
            if (t2, b2) != (title or "", body or ""):
                fts_updates.append((t2, b2, rowid))
            for k, v in c.n.items():
                fts.n[k] += v
            for k, v in c.distinct.items():
                fts.distinct[k] |= v
        report_add(report, f"{label}::fts", fts)
    if dry:
        con.close()
        return
    with con:
        con.executemany("update campus_records set data=? where collection=? and key=?", updates)
        con.executemany("update campus_segment_fts set title=?, body=? where rowid=?", fts_updates)
    if has_fts:
        with con:
            con.execute("insert into campus_segment_fts(campus_segment_fts) values('optimize')")
    con.execute("pragma wal_checkpoint(TRUNCATE)")
    con.execute("vacuum")  # drop freed pages so masked originals do not linger in the file
    con.execute("pragma wal_checkpoint(TRUNCATE)")
    con.close()


def plan_snapshots(root, report):
    """Return {old_hash: (path, new_bytes, new_hash)} for text snapshots that change."""
    plan = {}
    c_all = Counter()
    skipped = collections.Counter()
    base = root / "data" / "snapshots"
    for path in sorted(base.glob("*/*.bin")):
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            hit = False
            if raw[:2] == b"PK":
                try:
                    z = zipfile.ZipFile(io.BytesIO(raw))
                    for n in z.namelist():
                        try:
                            hit |= bool(MOBILE.search(z.read(n).decode("utf-8")) or EMAIL.search(z.read(n).decode("utf-8")))
                        except UnicodeDecodeError:
                            pass
                except zipfile.BadZipFile:
                    pass
            skipped["binary_with_hit" if hit else "binary_no_hit"] += 1
            continue
        c = Counter()
        new = mask_html(text, c)
        for k, v in c.n.items():
            c_all.n[k] += v
        for k, v in c.distinct.items():
            c_all.distinct[k] |= v
        if new != text:
            nb = new.encode("utf-8")
            plan[path.stem] = (path, nb, sha256_bytes(nb))
    report_add(report, "data/snapshots (HTML/text files)", c_all)
    report["data/snapshots (HTML/text files)"]["files_changed"] = len(plan)
    report["data/snapshots (binary, left untouched)"] = dict(skipped)
    return plan


def apply_snapshots(plan):
    for old, (path, nb, new) in plan.items():
        if sha256_bytes(path.read_bytes()) != old:
            raise SystemExit(f"{path} does not match its own name; refusing")
        target = path.with_name(new + ".bin")
        target.write_bytes(nb)
        path.unlink()


def mask_staging(root, report, dry):
    c = Counter()
    files = 0
    base = root / "deploy" / "operator" / "staging"
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.suffix not in {".json", ".txt", ".md"}:
            continue
        text = path.read_text(encoding="utf-8")
        local = Counter()
        new = mask_text(text, local)
        for k, v in local.n.items():
            c.n[k] += v
        for k, v in local.distinct.items():
            c.distinct[k] |= v
        if new != text:
            files += 1
            if not dry:
                path.write_text(new, encoding="utf-8", newline="")
    report_add(report, "deploy/operator/staging (files)", c)
    report["deploy/operator/staging (files)"]["files_changed"] = files


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=Path("."))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", type=Path, default=None, help="write JSON count report here")
    args = ap.parse_args()
    root = args.root.resolve()
    report = {}

    plan = plan_snapshots(root, report)
    hash_map = {old: new for old, (_, _, new) in plan.items()}
    main_db = root / "data" / "campus.sqlite3"
    mask_db(main_db, hash_map, report, "data/campus.sqlite3", args.dry_run)
    for extra in sorted((root / "deploy" / "snapshot").glob("*.sqlite3")):
        # No blobs ship next to this database, so its content hashes stay as recorded.
        mask_db(extra, {}, report, f"deploy/snapshot/{extra.name}", args.dry_run)
    mask_staging(root, report, args.dry_run)

    if not args.dry_run:
        apply_snapshots(plan)
        sample = root / "data" / "PUBLIC_SAMPLE.json"
        meta = json.loads(sample.read_text(encoding="utf-8"))
        meta["contacts_masked"] = True
        meta["contacts_masking"] = "mobile numbers -> 138****5678, e-mail -> a***@domain; department landlines kept (see docs/MASKING_REPORT.json)"
        meta["database_sha256"] = hashlib.sha256(main_db.read_bytes()).hexdigest()
        sample.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report["data/PUBLIC_SAMPLE.json"] = {"database_sha256_refreshed": True}
        report["new_database_sha256"] = meta["database_sha256"]
        report["deploy/snapshot sha256"] = {
            p.name: {"bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in sorted((root / "deploy" / "snapshot").glob("*.sqlite3"))
        }
    if args.report and not args.dry_run:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.exit(main())
