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

import importlib.util
import json
import sys
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
from harness2.substrate.codex import render_events as render_codex_events
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


# A real `claude -p --output-format stream-json --verbose` capture, trimmed to the
# events that matter: init, a thinking+tool_use turn, the nested tool_result user
# event, a final text turn, and the terminal result. Both field orders are
# represented exactly as claude writes them -- `type` is LAST in the result event,
# which is what a `{"type":` prefix filter silently drops.
_CLAUDE_JSONL = "\n".join([
    json.dumps({"type": "system", "subtype": "init", "cwd": "/tmp/ws",
                "tools": ["Write", "Bash"], "model": "claude-opus-5[1m]"}),
    json.dumps({"type": "assistant", "message": {
        "id": "msg_1", "role": "assistant", "content": [
            {"type": "thinking", "thinking": "Trivial. Just write it."},
            {"type": "tool_use", "id": "tu_1", "name": "Write",
             "input": {"file_path": "/tmp/ws/output/hello.txt", "content": "hi\n"}},
        ],
        "usage": {"input_tokens": 0, "output_tokens": 0}}}, sort_keys=True),
    json.dumps({"type": "user", "message": {
        "role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_1",
             "content": "File created successfully at: /tmp/ws/output/hello.txt"}]}}),
    json.dumps({"type": "assistant", "message": {
        "id": "msg_2", "role": "assistant", "content": [
            {"type": "thinking", "thinking": "Done."},
            {"type": "text", "text": "`hello.txt` written, contains `hi`."}],
        "usage": {"input_tokens": 0, "output_tokens": 0}}}, sort_keys=True),
    # sort_keys puts `type` last here, exactly as claude's own serializer does.
    json.dumps({"duration_api_ms": 10516, "is_error": False, "num_turns": 2,
                "result": "`hello.txt` written, contains `hi`.",
                "session_id": "s1", "subtype": "success", "total_cost_usd": 0.17,
                "usage": {"input_tokens": 68352, "output_tokens": 42,
                          "cache_creation_input_tokens": 100,
                          "cache_read_input_tokens": 200,
                          "output_tokens_details": {"thinking_tokens": 7}},
                "type": "result"}, sort_keys=True),
]) + "\n"

# The same stream cut off before its terminal result event: a rollout killed mid-turn.
_CLAUDE_TRUNCATED_JSONL = "\n".join(_CLAUDE_JSONL.splitlines()[:-1]) + "\n"

# An auth failure: claude still emits an assistant event, with an error field and a
# synthetic message, then a result event carrying is_error.
_CLAUDE_ERROR_JSONL = "\n".join([
    json.dumps({"type": "assistant", "error": "authentication_failed",
                "message": {"id": "x", "role": "assistant",
                            "content": [{"type": "text",
                                         "text": "Not logged in · Please run /login"}],
                            "usage": {"input_tokens": 0, "output_tokens": 0}}},
               sort_keys=True),
    json.dumps({"is_error": True, "num_turns": 1, "result": "Not logged in · Please run /login",
                "terminal_reason": "api_error", "type": "result",
                "usage": {"input_tokens": 0, "output_tokens": 0}}, sort_keys=True),
]) + "\n"

# A codex-shaped stream, to prove the codex path is untouched by the claude support.
_CODEX_JSONL = "\n".join([
    json.dumps({"type": "item.completed", "item": {
        "id": "i1", "type": "command_execution", "command": "ls -la",
        "aggregated_output": "total 0\n", "exit_code": 0, "status": "completed"}}),
    json.dumps({"type": "item.completed", "item": {
        "id": "i2", "type": "agent_message", "text": "done"}}),
    json.dumps({"type": "turn.completed", "usage": {
        "input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 5,
        "reasoning_output_tokens": 1}}),
]) + "\n"


def _load_runner():
    """The real LAB runner module, with only `external.tts_lab.config` stubbed.

    parse_session is pure over the capture text, so the stubs exist solely to let the
    module import; nothing here reimplements it.
    """
    path = (Path(LAB.__file__).resolve().parents[2] / "third_party" / "lab_runner"
            / "adapt_runs_cx" / "run_codex_lab.py")
    if not path.is_file():
        raise unittest.SkipTest(f"runner not present at {path}")
    cfg = types.ModuleType("external.tts_lab.config")
    cfg.documents_dir = lambda t: Path("/nonexistent")
    cfg.task_json = lambda t: {}
    pkg = types.ModuleType("external")
    ttslab = types.ModuleType("external.tts_lab")
    ttslab.config = cfg
    saved = {k: sys.modules.get(k) for k in
             ("external", "external.tts_lab", "external.tts_lab.config")}
    sys.modules.update({"external": pkg, "external.tts_lab": ttslab,
                        "external.tts_lab.config": cfg})
    try:
        spec = importlib.util.spec_from_file_location("_lab_runner_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    return mod


class ClaudeStreamParsing(unittest.TestCase):
    """parse_session over claude's real stream-json schema.

    The bug these pin: usage lives ONLY on the terminal `result` event, whose `type`
    field claude writes last -- so a `{"type":` prefix filter drops it and the run
    reports zero tokens while looking perfectly healthy.
    """

    @classmethod
    def setUpClass(cls):
        cls.runner = _load_runner()

    def _parse(self, raw: str) -> dict:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "claude-code_session.jsonl"
            p.write_text(raw, encoding="utf-8")
            return self.runner.parse_session(p)

    def test_usage_taken_from_the_result_event(self):
        got = self._parse(_CLAUDE_JSONL)
        self.assertTrue(got["usage_present"])
        self.assertEqual(got["input_tokens"], 68352)
        self.assertEqual(got["output_tokens"], 42)

    def test_tokens_are_not_double_counted(self):
        """Assistant events carry an all-zero usage; summing them must not shift the
        totals, and summing them WITH the result event must not inflate them."""
        got = self._parse(_CLAUDE_JSONL)
        self.assertEqual(got["input_tokens"], 68352, "assistant usage leaked into the total")
        self.assertEqual(got["output_tokens"], 42)

    def test_turns_and_tool_calls_come_from_their_own_places(self):
        got = self._parse(_CLAUDE_JSONL)
        self.assertEqual(got["turns"], 2, "num_turns on the result event")
        self.assertEqual(got["tool_calls"], 1, "tool_use blocks in assistant events")
        self.assertEqual(got["errors"], 0)

    def test_final_text_prefers_the_result_summary(self):
        got = self._parse(_CLAUDE_JSONL)
        self.assertEqual(got["final_text"], "`hello.txt` written, contains `hi`.")

    def test_cached_input_sums_both_cache_figures(self):
        got = self._parse(_CLAUDE_JSONL)
        self.assertEqual(got["cached_input_tokens"], 300, "100 creation + 200 read")
        self.assertEqual(got["reasoning_output_tokens"], 7)

    def test_truncated_stream_reports_usage_absent_not_zero(self):
        """A rollout killed before its result event: the figures are ABSENT, and a caller
        must be able to tell that from a real zero-token run."""
        got = self._parse(_CLAUDE_TRUNCATED_JSONL)
        self.assertFalse(got["usage_present"])
        self.assertEqual(got["input_tokens"], 0)
        self.assertEqual(got["tool_calls"], 1, "the tool call still happened")
        self.assertGreater(got["n_events"], 1, "the stream was parseable, just unfinished")

    def test_prose_stream_is_not_read_as_a_claude_stream(self):
        """A harness printing prose must fall to the codex branch and report nothing,
        not be misread as a claude stream with absent usage."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "pi_session.jsonl"
            p.write_text("just some prose\nthe agent said things\n", encoding="utf-8")
            got = self.runner.parse_session(p)
        self.assertEqual(got["n_events"], 0)
        self.assertNotIn("usage_present", got, "prose is the codex branch's cold case")

    def test_error_stream_is_counted(self):
        got = self._parse(_CLAUDE_ERROR_JSONL)
        self.assertGreaterEqual(got["errors"], 1)
        self.assertTrue(got["usage_present"], "a failed run still reports its usage")


class ClaudeStreamRendering(unittest.TestCase):
    """render_events over claude's schema, and the codex path beside it."""

    def _render(self, raw: str) -> str:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "claude-code_session.jsonl"
            p.write_text(raw, encoding="utf-8")
            return render_codex_events(p, obs_trunc=500, max_chars=10 ** 6)

    def test_no_longer_renders_as_no_parseable_events(self):
        self.assertNotIn("(no parseable events", self._render(_CLAUDE_JSONL))

    def test_text_tool_and_result_are_all_rendered(self):
        out = self._render(_CLAUDE_JSONL)
        self.assertIn("[ASSISTANT] `hello.txt` written", out)
        self.assertIn("[ACTION] Write:", out)
        self.assertIn("[OBSERVATION] File created successfully", out)
        self.assertIn("[REASONING] Trivial.", out)

    def test_result_line_carries_the_token_totals(self):
        out = self._render(_CLAUDE_JSONL)
        self.assertIn("turns=2", out)
        self.assertIn("in=68352", out)
        self.assertIn("out=42", out)
        self.assertIn("cached=300", out)

    def test_error_stream_surfaces_the_failure(self):
        out = self._render(_CLAUDE_ERROR_JSONL)
        self.assertIn("[ERROR]", out)
        self.assertIn("authentication_failed", out)

    def test_truncated_stream_renders_what_arrived(self):
        """No result event, so no [HARNESS] line -- but the work is still visible."""
        out = self._render(_CLAUDE_TRUNCATED_JSONL)
        self.assertNotIn("(no parseable events", out)
        self.assertIn("[ACTION] Write:", out)
        self.assertNotIn("[HARNESS] result", out)

    def test_codex_stream_is_rendered_exactly_as_before(self):
        """The claude branch must not capture codex's shape: codex writes `type` first
        and has no `assistant`/`result` events."""
        out = self._render(_CODEX_JSONL)
        self.assertIn("[ACTION] shell: ls -la", out)
        self.assertIn("[ASSISTANT] done", out)
        self.assertIn("[HARNESS] turn completed", out)
        self.assertIn("tokens in=100", out)

    def test_codex_parser_unchanged(self):
        runner = _load_runner()
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "codex_session.jsonl"
            p.write_text(_CODEX_JSONL, encoding="utf-8")
            got = runner.parse_session(p)
        self.assertEqual(got["turns"], 1)
        self.assertEqual(got["input_tokens"], 100)
        self.assertEqual(got["cached_input_tokens"], 20)
        self.assertEqual(got["output_tokens"], 5)
        self.assertEqual(got["tool_calls"], 1)
        self.assertEqual(got["final_text"], "done")
        self.assertNotIn("usage_present", got, "the codex branch keeps its own key set")


class ClaudeManifest(unittest.TestCase):
    def test_headless_asks_for_structured_output(self):
        """Without these flags the capture is prose and the renderer stays blind."""
        spec = __import__("harness2.harnesses.registry", fromlist=["x"]).load_one("claude-code")
        self.assertIn("--output-format", spec.headless)
        self.assertIn("stream-json", spec.headless)
        self.assertIn("--verbose", spec.headless)
        self.assertIn("{prompt}", spec.headless)

    def test_permission_mode_allows_bash(self):
        """acceptEdits is not enough: it denies `python3 scripts/read_doc.py`, which is
        how LAB reads every binary document, so such a rollout delivers nothing. The
        manifest must keep a mode that lets Bash run."""
        spec = __import__("harness2.harnesses.registry", fromlist=["x"]).load_one("claude-code")
        self.assertIn("--permission-mode", spec.headless)
        mode = spec.headless[spec.headless.index("--permission-mode") + 1]
        self.assertIn(mode, ("auto", "bypassPermissions"), "acceptEdits blocks the LAB work")
        self.assertNotEqual(mode, "acceptEdits")


if __name__ == "__main__":
    unittest.main()
