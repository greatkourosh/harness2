# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""LAB cells: an external registered harness beside the in-tree oc/cx frameworks.

Two properties matter and neither is visible from one file. First, an external id must
become a real ExternalCliSubstrate and NOT silently fall back to CodexSubstrate -- the
original reason external ids were refused here. Second, adding a third cell must not
change what the oc and cx cells do.
"""

from __future__ import annotations

import tempfile
import types
import unittest
from dataclasses import replace
from pathlib import Path

from harness2 import config
from harness2.benchmarks import lab as LAB
from harness2.cli._common import make_adapter
from harness2.core import tree as HT
from harness2.substrate.codex import CodexSubstrate
from harness2.substrate.external_cli import ExternalCliSubstrate
from harness2.substrate.opencode import OpencodeSubstrate


class SubstrateSelection(unittest.TestCase):
    def test_known_frameworks_keep_their_own_class(self):
        self.assertIsInstance(LAB.substrate_for("oc", launcher=None), OpencodeSubstrate)
        self.assertIsInstance(LAB.substrate_for("cx", launcher=None), CodexSubstrate)

    def test_external_id_does_not_fall_back_to_codex(self):
        """The failure this whole change had to avoid: `--substrate pi` running codex."""
        sub = LAB.substrate_for("pi", launcher=None)
        self.assertIsInstance(sub, ExternalCliSubstrate)
        self.assertNotIsInstance(sub, CodexSubstrate)
        self.assertEqual(sub.name, "pi")

    def test_unknown_id_is_refused_with_the_known_ids(self):
        with self.assertRaises(ValueError) as ctx:
            LAB.substrate_for("nope", launcher=None)
        self.assertIn("nope", str(ctx.exception))

    def test_external_is_codex_shaped_but_not_opencode(self):
        """External harnesses read the AGENTS.md surface, so their tree is codex-shaped."""
        self.assertTrue(LAB.is_external("pi"))
        self.assertFalse(LAB.is_opencode("pi"))
        self.assertTrue(LAB.is_opencode("oc"))
        self.assertFalse(LAB.is_external("oc"))
        self.assertEqual(LAB.surface_for(LAB.substrate_for("pi", launcher=None)).name, "codex")


class SpecAndModel(unittest.TestCase):
    def test_external_spec_stages_a_codex_root_tree(self):
        spec = LAB.lab_spec("claude-code")
        self.assertEqual(spec.install_mode, "codex_root")
        # The oc runtime scripts exemption must NOT leak onto an external cell.
        self.assertEqual(spec.reserved_scripts, frozenset())

    def test_oc_spec_is_unchanged(self):
        spec = LAB.lab_spec("oc")
        self.assertEqual(spec.install_mode, "workspace_config")
        self.assertEqual(spec.name, "lab")

    def test_explicit_model_wins_for_every_cell(self):
        for substrate in ("oc", "cx", "pi"):
            self.assertEqual(LAB.model_for(substrate, "some/model"), "some/model")

    def test_external_model_comes_from_the_manifest(self):
        """A manifest default_model means no gateway round trip and no bare pattern."""
        self.assertEqual(LAB.model_for("pi", None), "All")
        self.assertNotIn("*", LAB.model_for("claude-code", None))

    def test_oc_and_cx_models_are_the_configured_ones(self):
        from harness2 import config

        self.assertEqual(
            LAB.model_for("oc", None) or config.SOLVER_MODEL_OC, config.SOLVER_MODEL_OC
        )
        self.assertEqual(LAB.model_for("cx", None), config.SOLVER_MODEL_CX)


class AdapterGuards(unittest.TestCase):
    def test_lab_accepts_an_external_id(self):
        adapter = make_adapter("lab", None, "claude-code")
        self.assertEqual(adapter.substrate.name, "claude-code")

    def test_jb_and_wb_still_refuse_an_external_id(self):
        """They own no launcher body for one, so accepting it would run the wrong agent."""
        for bench in ("jb", "wb"):
            with self.assertRaises(ValueError) as ctx:
                make_adapter(bench, None, "claude-code")
            self.assertIn("claude-code", str(ctx.exception))

    def test_oc_and_cx_still_work_for_every_bench(self):
        for bench, subset in (("jb", None), ("wb", "office"), ("lab", None)):
            for substrate in ("oc", "cx"):
                self.assertIsNotNone(make_adapter(bench, subset, substrate))


class BaseHarnessParity(unittest.TestCase):
    def _adapter_in_temp_runs(self, substrate: str, td: str) -> LAB.LabAdapter:
        """A cell whose base tree lands under a temp dir (runs_root is a frozen dataclass
        field, so it is replaced, not assigned)."""
        adapter = LAB.LabAdapter(substrate=substrate)
        adapter.spec = replace(adapter.spec, runs_root=Path(td))
        return adapter

    def test_external_base_tree_satisfies_the_lab_invariants(self):
        """An external cell starts from the same AGENTS.md base as cx, and that base must
        pass LAB's own validate_tree (output/ + read_doc.py), or no rollout is accepted."""
        with tempfile.TemporaryDirectory() as td:
            adapter = self._adapter_in_temp_runs("claude-code", td)
            base = adapter.base_harness()
            self.assertTrue(HT.is_tree(LAB.CODEX_SURFACE, base))
            adapter.validate_tree({"AGENTS.md": (base / "AGENTS.md").read_text(encoding="utf-8")})

    def test_cx_base_tree_is_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            adapter = self._adapter_in_temp_runs("cx", td)
            base = adapter.base_harness()
            self.assertTrue(HT.is_tree(LAB.CODEX_SURFACE, base))

    def test_external_and_cx_bases_do_not_collide(self):
        """Keyed by cell, so a claude-code rollout is never served the cx base tree."""
        with tempfile.TemporaryDirectory() as td:
            external = self._adapter_in_temp_runs("claude-code", td).base_harness()
            cx = self._adapter_in_temp_runs("cx", td).base_harness()
            self.assertNotEqual(external, cx)


class ExternalCellLifecycle(unittest.TestCase):
    """The whole cell for an external harness, with a stub argv in place of the real CLI.

    This is the test that would have caught the bugs found by running: the runner used to
    refuse to write metrics.json for a stream it could not parse (which rejected every
    plain-text harness), and the launcher used to discard the runner's own output, turning
    a long failure into a bare exit status.
    """

    def setUp(self):
        # The stub runs through the REAL LAB runner, which needs the LAB checkout and its
        # interpreter; on a copy without them this is skipped, not failed.
        if not config.LAB_PYTHON.is_file() or not (
            config.lab_repo() / "external" / "adapt_runs_cx" / "run_codex_lab.py"
        ).is_file():
            self.skipTest("LAB checkout / runner not installed; run scripts/setup_benchmark.py lab")
        # Under RUNS, not /tmp: the runner refuses a results root under /tmp (a run dir
        # there could be reaped mid-rollout), and this test must exercise the real runner.
        self.td = tempfile.TemporaryDirectory(dir=config.RUNS)
        self.addCleanup(self.td.cleanup)
        adapter = LAB.LabAdapter(substrate="claude-code")
        self.run_root = Path(self.td.name) / "results"
        adapter.spec = replace(adapter.spec, runs_root=Path(self.td.name) / "runs")
        adapter._results_root = self.run_root
        # A stub stands in for the CLI: it records the prompt it received, so the sentinel
        # contract is asserted rather than assumed.
        self.stub = Path(self.td.name) / "stub.py"
        self.stub.write_text(
            "import pathlib, sys\n"
            "p = sys.argv[1]\n"
            "assert '@@HARNESS2_PROMPT@@' not in p, 'sentinel not substituted'\n"
            "d = pathlib.Path('output'); d.mkdir(exist_ok=True)\n"
            "(d / 'answer.md').write_text(p)\n",
            encoding="utf-8",
        )
        adapter.substrate.argv = types.MethodType(
            lambda s, prompt, ws, model: ["python3", str(self.stub), LAB._PROMPT_SENTINEL],
            adapter.substrate,
        )
        self.adapter = adapter

    def test_execute_completes_and_is_idempotent(self):
        out = self.adapter.execute(self.TASK, None, "exec/ext/1", timeout=120)
        self.assertTrue(out.is_dir())
        self.assertTrue((out / "metrics.json").is_file(), "metrics.json is the completion mark")
        self.assertTrue(self.adapter.artifacts_intact(out), "marker must match the output")
        self.assertTrue(self.adapter.delivered(out))
        self.assertEqual(
            sorted(p.name for p in (out / "output").rglob("*") if p.is_file()), ["answer.md"]
        )
        # The runner logs beside the run it describes, not to nowhere.
        log = out / "claude-code_runner.log"
        self.assertTrue(log.is_file())
        # A second execute() adopts the finished rollout rather than re-rolling it.
        self.assertEqual(self.adapter.execute(self.TASK, None, "exec/ext/1", timeout=120), out)

    def test_model_is_recorded_as_the_harness_that_ran(self):
        import json

        out = self.adapter.execute(self.TASK, None, "exec/ext/2", timeout=120)
        metrics = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
        self.assertEqual(metrics["runner"], "claude-code", "a cell must name its own runner")
        self.assertEqual(metrics["exit_code"], 0)

    TASK = "employment-labor/draft-markup-of-settlement-agreement"


class PromptSentinel(unittest.TestCase):
    def test_adapter_and_runner_agree_on_the_sentinel(self):
        """The adapter puts this token in the prompt slot; the runner replaces it with the
        real prompt. If the two literals drift, every external rollout gets the token as
        its instructions -- a silent wrong run, so it is asserted here."""
        self.assertEqual(LAB._PROMPT_SENTINEL, "@@HARNESS2_PROMPT@@")
        runner = (
            Path(LAB.__file__).resolve().parents[2]
            / "third_party"
            / "lab_runner"
            / "adapt_runs_cx"
            / "run_codex_lab.py"
        )
        if not runner.is_file():
            self.skipTest(f"runner not present at {runner}")
        self.assertIn(
            f'PROMPT_SENTINEL = "{LAB._PROMPT_SENTINEL}"',
            runner.read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
