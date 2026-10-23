"""The per-entry fixture-server contracts of ``tools/ai_round.py``.

Two of the nine browser entries need more than a plain fixture server, and the
first version of the one-command runner launched all of them the same way: the
feed-mode entry failed on an assertion about a missing
``CAMPUS_E2E_FEED_ORIGIN`` (and so never drove its seeded capture event), and
the reliability module refused to start at all because its data directory
already existed. Both were found by running the entries and reading the
failure -- which is the expensive way to find out, because every entry but
those two had to run first.

These tests pin the launch plan itself, so a later edit to ``check_browser``
that drops a contract fails fast and names the contract, instead of failing
three minutes into an acceptance run -- or not failing at all and reporting a
weaker pass (a skipped check looks like a passed run to a reader of the JSON).
"""

import unittest
from pathlib import Path

import tools.ai_round as ai_round_module
from tools.ai_round import BROWSER_ENTRIES, entry_launch_plan

# The runner's own source, located through the import rather than through
# guesses about how deep this test file sits in the tree.
RUNNER_SOURCE = Path(ai_round_module.__file__).resolve()

# The plan never touches the disk, so a path that does not exist is fine and
# keeps the test independent of the machine's temporary directory.
SCRATCH = Path("scratch-does-not-exist")
PORT = 45999
PLAIN = tuple(entry for entry in BROWSER_ENTRIES if entry not in {"feed-mode", "reliability"})


def plan(entry, **overrides):
    kwargs = {"python": "python", "port": PORT, "scratch": SCRATCH, "base_env": {}}
    kwargs.update(overrides)
    return entry_launch_plan(entry, **kwargs)


class EntryLaunchPlanTests(unittest.TestCase):
    def test_every_entry_gets_its_own_port_and_the_shipped_output_layout(self):
        for entry in BROWSER_ENTRIES:
            with self.subTest(entry=entry):
                launched = plan(entry)
                self.assertIn("--port", launched.command)
                self.assertIn(str(PORT), launched.command)
                self.assertEqual(launched.environment["CAMPUS_E2E_URL"], f"http://127.0.0.1:{PORT}")
                # Compared with the platform's own separator: the plan builds
                # this from pathlib, so a POSIX literal would fail on Windows.
                tail = str(Path("verification") / "browser" / entry / "artifacts")
                self.assertTrue(launched.environment["CAMPUS_E2E_OUTPUT"].endswith(tail))

    def test_feed_mode_seeds_the_capture_event_and_trusts_the_fixture_origin(self):
        launched = plan("feed-mode")
        self.assertIn("server.campus.tests.browser_demo", launched.command)
        self.assertIn("--feed-event-info", launched.command)
        self.assertIsNotNone(launched.feed_event_info)
        # The entry asserts on this before it does anything else; without it
        # the whole entry is an ERR_ASSERTION, not a weaker pass.
        self.assertEqual(launched.environment["CAMPUS_E2E_FEED_ORIGIN"], f"http://127.0.0.1:{PORT}")
        # create_source() runs canonical_url() + resolve_public(): a URL on a
        # non-default port is unsafe_port, and 127.0.0.1 is not a global
        # address, unless the origin is an explicitly trusted gateway. The
        # fixture runs on an ephemeral port by design.
        self.assertEqual(launched.environment["CAMPUS_TRUSTED_GATEWAY_ORIGINS"], f"http://127.0.0.1:{PORT}")

    def test_reliability_runs_its_own_module_with_a_manifest_and_a_fresh_data_dir(self):
        launched = plan("reliability")
        self.assertIn("server.campus.tests.reliability_demo", launched.command)
        self.assertIn("--manifest", launched.command)
        self.assertIsNotNone(launched.manifest)
        self.assertEqual(launched.environment["CAMPUS_E2E_FIXTURE"], str(launched.manifest))
        # reliability_demo errors out unless the data directory does not exist
        # yet; pointing it at the scratch directory itself made the server exit
        # before it ever listened.
        self.assertNotEqual(launched.data_dir, SCRATCH)
        self.assertEqual(launched.data_dir.parent, SCRATCH)

    def test_plain_entries_carry_no_entry_specific_contract(self):
        for entry in PLAIN:
            with self.subTest(entry=entry):
                launched = plan(entry)
                self.assertIn("server.campus.tests.browser_demo", launched.command)
                self.assertNotIn("--feed-event-info", launched.command)
                self.assertNotIn("--manifest", launched.command)
                self.assertIsNone(launched.feed_event_info)
                self.assertIsNone(launched.manifest)
                self.assertEqual(launched.data_dir, SCRATCH)
                for variable in ("CAMPUS_E2E_FEED_ORIGIN", "CAMPUS_E2E_FIXTURE", "CAMPUS_TRUSTED_GATEWAY_ORIGINS"):
                    self.assertNotIn(variable, launched.environment)

    def test_ocr_is_opt_in(self):
        # The OCR check downloads the chi_sim+eng model; a plan that always
        # asked for it would make the default round network-dependent.
        self.assertNotIn("CAMPUS_E2E_OCR", plan("browser").environment)
        self.assertEqual(plan("browser", ocr=True).environment["CAMPUS_E2E_OCR"], "true")

    def test_the_base_environment_is_copied_never_mutated(self):
        base = {"PATH": "/usr/bin", "CAMPUS_E2E_URL": "http://stale"}
        launched = plan("feed-mode", base_env=base)
        self.assertEqual(launched.environment["PATH"], "/usr/bin")
        self.assertNotEqual(launched.environment["CAMPUS_E2E_URL"], "http://stale")
        self.assertEqual(base, {"PATH": "/usr/bin", "CAMPUS_E2E_URL": "http://stale"})


class RoundFlowTests(unittest.TestCase):
    """The order of one whole round, pinned on the runner's own source.

    The browser contracts above are per-entry; this class pins the *sequence* of
    the round, which is the other thing that has silently drifted. The release
    gate judged a tree whose evidence still carried the build machine's absolute
    paths, because the backend suite rewrites ``backend.xml`` and the run log on
    every run and nothing between the writer and the gate normalised them. For
    three rounds that was recorded as a documented second command instead of a
    defect; it is a defect -- a round that cannot end green on a clean machine is
    not a repeatable round.
    """

    @classmethod
    def setUpClass(cls):
        import ast

        tree = ast.parse(RUNNER_SOURCE.read_text(encoding="utf-8"))
        main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
        cls.calls = [
            node.func.id for node in ast.walk(main) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        ]

    def test_the_writers_run_before_the_normaliser_and_the_normaliser_before_the_gate(self):
        self.assertIn("check_backend", self.calls)
        self.assertIn("normalize_evidence", self.calls)
        self.assertIn("check_hygiene", self.calls)
        self.assertLess(self.calls.index("check_backend"), self.calls.index("normalize_evidence"))
        # The gate is the last check_hygiene call in the flow; the pre-flight one
        # must still come first, so an inherited leak is still reported.
        gates = [i for i, name in enumerate(self.calls) if name == "check_hygiene"]
        self.assertLess(self.calls.index("normalize_evidence"), gates[-1])
        self.assertLess(gates[0], self.calls.index("check_backend"))

    def test_the_normaliser_invocation_applies_instead_of_only_checking(self):
        import ast

        tree = ast.parse(RUNNER_SOURCE.read_text(encoding="utf-8"))
        normalise = next(
            node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "normalize_evidence"
        )
        invocation = next(
            node
            for node in ast.walk(normalise)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "run"
        )

        # Collected element-wise on purpose: the command list contains the
        # ``python`` parameter, so literal_eval on the whole list would raise.
        strings = {
            node.value
            for node in ast.walk(invocation)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        self.assertIn("tools/normalize_evidence_paths.py", strings)
        self.assertIn("--root", strings)
        self.assertNotIn("--check", strings)


if __name__ == "__main__":
    unittest.main()
