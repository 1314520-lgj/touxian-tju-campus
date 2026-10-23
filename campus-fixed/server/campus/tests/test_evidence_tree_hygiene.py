"""The delivered tree must not describe the machine it was built on, and must
not contradict itself.

Every acceptance run writes reports and logs, and those files naturally record
the absolute paths they ran from. Shipped verbatim they leak the build machine —
user name, drive, checkout directory, interpreter location — into an artifact
whose subject is the product, and they make two runs from different directories
differ for no reason. The 1.8.0 release fixed the paths it found by hand and
missed the ones JSON-escaped inside report arrays, so this pins the invariant
instead of trusting the next review to notice.

Five checks, deliberately independent:

* the shipped tree is already normalized, judged by the same tool a release
  engineer runs (`tools/normalize_evidence_paths.py`), so the test cannot drift
  from the tool;
* no shipped text file names a home directory at all, which still holds if a
  path is somewhere the tool cannot place;
* every browser entry that records screenshots routes them through the shared
  relative-path helper, so the source of the leak stays closed;
* a report that says it passed does not sit next to the failure screenshot of an
  earlier attempt. An entry writes `<name>-failure.png` only when it fails, and a
  later passing run overwrites the report but not that file — so a green report
  could ship beside a picture of the bug it claims to have fixed;
* the release manifest is LF-only. This one is not about leaking a machine, but
  it belongs with the others: it has bitten twice, because `Path.write_text()`
  translates `\n` to `os.linesep`, so on Windows a rebuild rewrote every line as
  CRLF and buried the real change in a whole-file diff.

The JUnit report of the current run is out of scope: pytest writes the absolute
path of each skipped test into it, and the release step normalises it afterwards.
`tools/normalize_evidence_paths.py --check` is the gate that covers it.
"""

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tools.build_release_manifest import iter_shipped  # noqa: E402
from tools.normalize_evidence_paths import (  # noqa: E402
    build_roots,
    normalize_text,
    read_text,
)

# A drive-letter path, spaces allowed: a checkout may live under a folder whose
# name contains one, and stopping at the first space is how a half-rewritten
# path slipped through once.
WINDOWS_ABSOLUTE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:(?:\\{1,2}|[^\\\"'<>\n\r|])*")
POSIX_ABSOLUTE = re.compile(r"(?<![A-Za-z0-9_])/(?:Users|home|Documents)/[^/\s\"'<>]+")

# Path segments that only ever appear because somebody published a local path.
PERSONAL_SEGMENTS = frozenset({"users", "home", "documents", "appdata", "desktop"})

# The JUnit report is written by the very run executing this test, and pytest
# stamps the absolute path of every skipped test into it. The invariant is a
# property of the *released* tree, and the release step establishes it with
# `tools/normalize_evidence_paths.py --check`, so this file cannot satisfy the
# check from inside the run that dirties it. Everything else is in scope.
WRITTEN_BY_THIS_RUN = frozenset({"verification/backend.xml"})


def shipped_text_files():
    for relative, path in iter_shipped(ROOT):
        if relative in WRITTEN_BY_THIS_RUN:
            continue
        text = read_text(path)
        if text is not None:
            yield relative, path, text


class EvidenceTreeHygieneTests(unittest.TestCase):
    def test_shipped_tree_is_already_normalized(self):
        specific, generic = build_roots(ROOT)
        pending = []
        for relative, _, text in shipped_text_files():
            _, changed = normalize_text(text, specific, generic, is_xml=Path(relative).suffix.lower() == ".xml")
            if changed:
                pending.append(f"{relative} ({changed})")
        self.assertEqual(
            pending,
            [],
            "run `python tools/normalize_evidence_paths.py --root .` before releasing: " + ", ".join(pending[:10]),
        )

    def test_no_shipped_file_names_a_personal_directory(self):
        offenders = []
        for relative, _, text in shipped_text_files():
            for token in WINDOWS_ABSOLUTE.findall(text):
                real = token.replace("\\\\", "\\")
                segments = {segment.lower() for segment in re.split(r"[\\/]+", real) if segment}
                if segments & PERSONAL_SEGMENTS:
                    offenders.append(f"{relative}: {token[:120]}")
            for match in POSIX_ABSOLUTE.findall(text):
                offenders.append(f"{relative}: {match[:120]}")
        self.assertEqual(offenders, [], "delivered evidence names the build machine: " + "; ".join(offenders[:10]))

    def test_browser_entries_record_screenshots_through_the_shared_helper(self):
        entries = sorted((ROOT / "src" / "campus" / "tests").glob("*.e2e.mjs"))
        self.assertTrue(entries, "no browser entries found")
        unwrapped = []
        for entry in entries:
            source = entry.read_text(encoding="utf-8")
            for match in re.finditer(r"screenshots\.push\(([^)]*)", source):
                if "relativeArtifact(" not in match.group(1):
                    unwrapped.append(f"{entry.name}: {match.group(1)[:60]}")
            if "screenshots.push(" in source:
                self.assertIn(
                    "relativeArtifact", source, f"{entry.name} pushes screenshots but never imports the helper"
                )
        self.assertEqual(unwrapped, [], "a browser report would store an absolute path: " + "; ".join(unwrapped))

    def test_a_passing_report_does_not_ship_a_failure_screenshot(self):
        leftovers = []
        for report in sorted((ROOT / "verification").rglob("*report*.json")):
            try:
                data = json.loads(report.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if data.get("status") != "passed":
                continue
            for stale in sorted(report.parent.glob("*failure*.png")):
                leftovers.append(f"{report.relative_to(ROOT).as_posix()} says passed, but {stale.name} is still there")
        self.assertEqual(
            leftovers, [], "a superseded failure screenshot would ship next to a green report: " + "; ".join(leftovers)
        )

    def test_release_manifest_is_lf_only(self):
        raw = (ROOT / "RELEASE_MANIFEST.json").read_bytes()
        self.assertEqual(
            raw.count(b"\r"),
            0,
            "RELEASE_MANIFEST.json carries CRLF; tools/build_release_manifest.py must open it with newline='\\n'",
        )


if __name__ == "__main__":
    unittest.main()
