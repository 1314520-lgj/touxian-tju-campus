"""Release hygiene for the operator relay's local artifacts.

The first real operating cycle produced three kinds of file that must never
reach a shipped release, and the manifest builder had no rule for any of them:

* ``deploy/operator/session.json`` -- a live session cookie. It authenticates
  as the deployment's operator exactly like ``data/.server-secret`` does, so a
  manifest that hashes it in is shipping a key. The builder must stop, the
  same way it stops for a signing key.
* ``data-operator/`` -- a running operator-mode instance: users, sessions,
  candidates, evidence. Machine-local state, not a deliverable.
* ``deploy/operator/out/`` -- feed build output, regenerated verbatim from
  ``staging/`` by ``relay.py build``. Shipping it would let a stale copy
  disagree with the staging records that are the actual source of truth.

``.gitignore`` cannot express these rules for the release: the manifest
builder has its own walk and does not read it, so the invariant has to live
in the builder -- pinned here.
"""

import json
import tempfile
import unittest
from pathlib import Path

from tools.build_release_manifest import build, iter_shipped

MANIFEST_PUBLIC_SAMPLE = "data/campus.sqlite3"
SESSION_PATH = "deploy/operator/session.json"
FEED_OUTPUT = "deploy/operator/out/feed-zs-notices.json"
LIVE_INSTANCE = "data-operator/campus.sqlite3"
SHIPPABLE_RELAY_FILE = "deploy/operator/relay.py"


class OperatorArtifactGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        # build() refuses to run without the public sample database.
        sample = self.root / MANIFEST_PUBLIC_SAMPLE
        sample.parent.mkdir(parents=True)
        sample.write_bytes(b"sqlite-sample")
        # A relay file that must keep shipping, so the exclusions stay narrow.
        shippable = self.root / SHIPPABLE_RELAY_FILE
        shippable.parent.mkdir(parents=True)
        shippable.write_text("# operator relay\n", encoding="utf-8")

    def manifest_paths(self):
        return {entry["path"] for entry in build(self.root, "test", "test")["files"]}

    def test_a_live_session_cookie_stops_the_build(self):
        session = self.root / SESSION_PATH
        session.parent.mkdir(parents=True, exist_ok=True)
        session.write_text(json.dumps({"cookie": "campus_session=live"}), encoding="utf-8")
        with self.assertRaises(SystemExit) as caught:
            build(self.root, "test", "test")
        self.assertIn(SESSION_PATH, str(caught.exception))

    def test_the_operator_instance_data_directory_does_not_ship(self):
        live = self.root / LIVE_INSTANCE
        live.parent.mkdir(parents=True)
        live.write_bytes(b"live operator instance")
        paths = self.manifest_paths()
        self.assertNotIn(LIVE_INSTANCE, paths)
        self.assertIn(SHIPPABLE_RELAY_FILE, paths)

    def test_regenerated_feed_output_does_not_ship(self):
        feed = self.root / FEED_OUTPUT
        feed.parent.mkdir(parents=True)
        feed.write_text('{"items": []}', encoding="utf-8")
        paths = self.manifest_paths()
        self.assertNotIn(FEED_OUTPUT, paths)
        self.assertIn(SHIPPABLE_RELAY_FILE, paths)

    def test_iter_shipped_prunes_the_excluded_directories(self):
        (self.root / "data-operator" / "sub").mkdir(parents=True)
        (self.root / "data-operator" / "sub" / "deep.txt").write_text("x", encoding="utf-8")
        (self.root / "deploy" / "operator" / "out" / "nested").mkdir(parents=True)
        (self.root / "deploy" / "operator" / "out" / "nested" / "deep.json").write_text("{}", encoding="utf-8")
        (self.root / "deploy" / "operator" / "staging").mkdir(parents=True)
        (self.root / "deploy" / "operator" / "staging" / "notice.txt").write_text("notice", encoding="utf-8")
        relatives = {relative for relative, _ in iter_shipped(self.root)}
        self.assertNotIn("data-operator/sub/deep.txt", relatives)
        self.assertNotIn("deploy/operator/out/nested/deep.json", relatives)
        self.assertIn("deploy/operator/staging/notice.txt", relatives)

    def test_an_ad_hoc_scratch_directory_does_not_ship(self):
        # Round 10 built the public data snapshot in `.snapshot-tmp/` and left the
        # helper script behind. The builder had no rule for that name, so the
        # scratch file was hashed into the manifest and would have shipped inside
        # the archive. A scratch directory is never a deliverable; pinned here so
        # the next one cannot widen the manifest silently either.
        scratch = self.root / ".snapshot-tmp" / "update_verification.py"
        scratch.parent.mkdir(parents=True)
        scratch.write_text("# scratch\n", encoding="utf-8")
        paths = self.manifest_paths()
        self.assertNotIn(".snapshot-tmp/update_verification.py", paths)
        # The exclusions stay narrow: an ordinary shipped file is still listed.
        self.assertIn(SHIPPABLE_RELAY_FILE, paths)


if __name__ == "__main__":
    unittest.main()
