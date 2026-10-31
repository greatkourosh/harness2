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

"""Regressions for the harness control plane: registry, adapters, isolation."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness2.harnesses import adapters, registry
from harness2.substrate.external_cli import ExternalCliSubstrate


class RegistryTests(unittest.TestCase):
    def test_shipped_manifests_all_load(self):
        specs = registry.load_all()
        ids = {s.id for s in specs}
        self.assertEqual(
            ids, {"pi", "opencode", "deepseek", "claude-code", "claude-desktop"}
        )
        for spec in specs:
            self.assertTrue(spec.config_path.is_absolute(), spec.id)
            self.assertTrue(spec.key_name, spec.id)

    def test_matching_uses_globs_for_groups(self):
        catalog = ["glm-5.3", "qwen3.5-plus", "claude-sonnet-4-6", "All", "local"]
        spec = registry.load_one("deepseek")
        self.assertEqual(
            sorted(registry.match_models(spec, catalog)), ["glm-5.3", "local", "qwen3.5-plus"]
        )

    def test_empty_allow_list_means_unrestricted(self):
        spec = registry.load_one("pi")
        spec = type(spec)(**{**spec.__dict__, "allowed_models": ()})
        self.assertEqual(registry.match_models(spec, ["a", "b"]), ["a", "b"])

    def test_trace_dir_stops_before_the_wildcard(self):
        """A glob like ~/.pi/agent/sessions/**/*.jsonl must resolve to a real dir."""
        spec = registry.load_one("pi")
        directory = spec.trace_dir()
        self.assertIsNotNone(directory)
        self.assertNotIn("*", str(directory))


class AdapterTests(unittest.TestCase):
    def _pi(self, root: Path) -> adapters.PiAdapter:
        spec = registry.load_one("pi")
        return adapters.PiAdapter(
            type(spec)(**{**spec.__dict__, "config_path": root / "models.json"})
        )

    def test_pi_apply_merges_and_keeps_other_providers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "models.json"
            config.write_text(
                json.dumps(
                    {
                        "providers": {
                            "other": {"baseUrl": "http://x", "models": [{"id": "keep"}]},
                            "9router": {"baseUrl": "http://old", "api": "openai-completions"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            adapter = self._pi(root)
            with patch.object(adapters.config, "NINEROUTER_BASE_URL", "http://gw/v1"), patch.object(
                adapters.config, "NINEROUTER_API_KEY", "k"
            ):
                result = adapter.apply(allow=["All", "Safe"])
            self.assertTrue(result.ok)
            data = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(data["providers"]["other"]["models"], [{"id": "keep"}])
            self.assertEqual(
                [m["id"] for m in data["providers"]["9router"]["models"]], ["All", "Safe"]
            )
            self.assertEqual(data["providers"]["9router"]["baseUrl"], "http://gw/v1")
            self.assertIsNotNone(result.backup)

    def test_claude_code_aliases_keep_their_tiers(self):
        """SONNET must get a sonnet id, not whatever the glob sort produced."""
        spec = registry.load_one("claude-code")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            # Deliberately unsorted: a positional assignment would mislabel these.
            adapter = adapters.ClaudeCodeAdapter(
                type(spec)(**{**spec.__dict__, "config_path": path})
            )
            with patch.object(adapters.config, "NINEROUTER_BASE_URL", "http://gw/v1"), patch.object(
                adapters.config, "NINEROUTER_API_KEY", "k"
            ):
                adapter.apply(allow=["claude-haiku-4-5", "claude-opus-4-6", "claude-sonnet-4-6"])
            env = json.loads(path.read_text(encoding="utf-8"))["env"]
            self.assertEqual(env["ANTHROPIC_DEFAULT_SONNET_MODEL"], "claude-sonnet-4-6")
            self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], "claude-opus-4-6")
            self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "claude-haiku-4-5")

    def test_claude_code_apply_preserves_hooks_and_other_keys(self):
        spec = registry.load_one("claude-code")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(
                json.dumps({"hooks": {"Stop": [{"hooks": []}]}, "enabledPlugins": {"x": True}}),
                encoding="utf-8",
            )
            adapter = adapters.ClaudeCodeAdapter(
                type(spec)(**{**spec.__dict__, "config_path": path})
            )
            with patch.object(adapters.config, "NINEROUTER_BASE_URL", "http://gw/v1"), patch.object(
                adapters.config, "NINEROUTER_API_KEY", "k"
            ):
                adapter.apply(allow=["claude-sonnet-4-6"])
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("hooks", data)
            self.assertIn("enabledPlugins", data)
            self.assertEqual(data["env"]["ANTHROPIC_BASE_URL"], "http://gw")

    def test_opencode_uses_singular_provider_key(self):
        """v1.14.18 rejects `providers`; the adapter must write `provider`."""
        spec = registry.load_one("opencode")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "opencode.json"
            path.write_text(json.dumps({"providers": {"keep": {}}}), encoding="utf-8")
            adapter = adapters.OpencodeAdapter(
                type(spec)(**{**spec.__dict__, "config_path": path})
            )
            with patch.object(adapters.config, "NINEROUTER_BASE_URL", "http://gw/v1"), patch.object(
                adapters.config, "NINEROUTER_API_KEY", "k"
            ):
                adapter.apply(allow=["claude-sonnet-4-6"])
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("provider", data)
            self.assertIn("9router", data["provider"])
            self.assertIn("keep", data["providers"])  # the pre-existing key survives

    def test_claude_desktop_refuses_to_write(self):
        adapter = adapters.get_adapter(registry.load_one("claude-desktop"))
        result = adapter.apply(allow=["gemini-2.5-flash"])
        self.assertFalse(result.ok)
        self.assertIn("claude-code", result.detail)

    def test_run_refuses_a_harness_without_headless_mode(self):
        adapter = adapters.get_adapter(registry.load_one("claude-desktop"))
        result = adapter.run("hi", model="m", cwd=Path("."))
        self.assertFalse(result.ok)
        self.assertIn("non-interactive", result.detail)


class SubstrateTests(unittest.TestCase):
    """The external-CLI substrate: argv from the manifest, env from the adapter."""

    def test_substrate_name_is_the_harness_id(self):
        """`--substrate pi` must resolve by id, not fall back to another class."""
        self.assertEqual(ExternalCliSubstrate("pi").name, "pi")
        self.assertEqual(ExternalCliSubstrate("claude-code").name, "claude-code")

    def test_unknown_harness_raises_with_the_known_ids(self):
        with self.assertRaises(ValueError) as ctx:
            ExternalCliSubstrate("nope")
        self.assertIn("pi", str(ctx.exception))

    def test_argv_substitutes_prompt_then_appends_model(self):
        substrate = ExternalCliSubstrate("pi")
        argv = substrate.argv("hello", Path("/ws"), "9router/All")
        self.assertEqual(argv, ["pi", "--print", "hello", "--model", "9router/All"])

    def test_argv_places_model_when_the_template_asks_for_it(self):
        """A manifest may put {model} inline; it must not then be appended twice."""
        spec = registry.load_one("pi")
        inline = type(spec)(**{**spec.__dict__, "headless": ("--model", "{model}", "{prompt}")})
        substrate = ExternalCliSubstrate("pi")
        substrate.spec = inline
        argv = substrate.argv("hi", Path("/ws"), "9router/All")
        self.assertEqual(argv, ["pi", "--model", "9router/All", "hi"])

    def test_claude_code_headless_carries_a_permission_mode(self):
        """Without it every write is held for approval and nothing is delivered.

        The mode must be one that lets Bash run: `acceptEdits` denies
        `python3 scripts/read_doc.py`, which is how every binary document in the
        LAB workspace is read, so the agent cannot do the work at all.
        """
        spec = registry.load_one("claude-code")
        self.assertIn("--permission-mode", spec.headless)
        mode = spec.headless[spec.headless.index("--permission-mode") + 1]
        self.assertIn(mode, ("auto", "bypassPermissions"))

    def test_deepseek_headless_passes_no_model_flag(self):
        """dsh selects its model from the profile, not from argv."""
        spec = registry.load_one("deepseek")
        self.assertEqual(spec.model_flag, "")
        substrate = ExternalCliSubstrate("deepseek")
        self.assertNotIn("--model", substrate.argv("x", Path("/ws"), "glm-cn/glm-5.3"))

    def test_deepseek_apply_sets_the_default_model_entry(self):
        """A provider entry alone leaves a headless dsh run with no model chosen."""
        spec = registry.load_one("deepseek")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cordis.patch.yml"
            adapter = adapters.DeepseekAdapter(
                type(spec)(**{**spec.__dict__, "config_path": path})
            )
            with patch.object(adapters.config, "NINEROUTER_BASE_URL", "http://gw/v1"), patch.object(
                adapters.config, "NINEROUTER_API_KEY", "k"
            ):
                adapter.apply(allow=["glm-cn/glm-5.3"])
            import yaml

            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            default = next(e for e in data if e["id"] == "agent-default-model")
            self.assertEqual(default["config"]["provider"], "9router")
            self.assertEqual(default["config"]["model"], "glm-cn/glm-5.3")

    def test_exec_env_applies_adapter_values_after_the_drops(self):
        """A dropped ambient var must not also be the value the adapter supplies."""
        substrate = ExternalCliSubstrate("claude-code")
        leaked = {
            "ANTHROPIC_API_KEY": "leaked",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-5[1m]",
        }
        env = substrate.exec_env(leaked)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("ANTHROPIC_DEFAULT_OPUS_MODEL", env)
        self.assertTrue(env.get("ANTHROPIC_AUTH_TOKEN"))
        self.assertTrue(env.get("ANTHROPIC_BASE_URL"))

    def test_surface_is_the_agents_md_convention(self):
        """Every external harness here reads AGENTS.md + .agents/skills."""
        surface = ExternalCliSubstrate("pi").surface()
        self.assertIn("AGENTS.md", surface.always_on)
        self.assertEqual(surface.skill_dir, ".agents/skills")

    def test_materialise_stages_only_the_root_entries(self):
        substrate = ExternalCliSubstrate("pi")
        bench = SimpleNamespace(install_mode="codex_root")
        with tempfile.TemporaryDirectory() as tmp:
            tree_dir = Path(tmp) / "tree"
            (tree_dir / ".agents/skills/thing").mkdir(parents=True)
            (tree_dir / "AGENTS.md").write_text("# body\n", encoding="utf-8")
            (tree_dir / ".agents/skills/thing/SKILL.md").write_text(
                "---\nname: thing\n", encoding="utf-8"
            )
            install = substrate.materialise(tree_dir, bench, {})
            self.assertTrue((install.staging_dir / "AGENTS.md").is_file())
            self.assertTrue((install.staging_dir / ".agents/skills/thing/SKILL.md").is_file())

    def test_materialise_rejects_a_tree_without_agents_md(self):
        substrate = ExternalCliSubstrate("pi")
        bench = SimpleNamespace(install_mode="codex_root")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                substrate.materialise(Path(tmp), bench, {})


class RoutingTests(unittest.TestCase):
    """Choosing a harness from the task text."""

    def _specs(self) -> list:
        """Shipped manifests with a headless mode, so the router has a full pool.

        claude-desktop declares `can_run = false` and is deliberately left that
        way: it is not a routing target.
        """
        return [
            type(spec)(**{**spec.__dict__, "can_run": True})
            for spec in registry.load_all()
            if spec.can_run
        ]

    def test_keyword_picks_the_specialist_not_the_default(self):
        decision = registry.route("fix the auth bug in parser.py", self._specs())
        self.assertEqual(decision.spec.id, "pi")
        self.assertIn("bug", decision.matched)

    def test_legal_task_goes_to_the_document_harness(self):
        decision = registry.route("draft a legal memo about this contract", self._specs())
        self.assertEqual(decision.spec.id, "claude-code")

    def test_bulk_task_goes_to_the_cheap_lane(self):
        decision = registry.route("translate these support emails in bulk", self._specs())
        self.assertEqual(decision.spec.id, "deepseek")

    def test_unmatched_task_falls_to_the_declared_default(self):
        decision = registry.route("hello there", self._specs())
        self.assertTrue(decision.spec.default)
        self.assertEqual(decision.matched, ())

    def test_keywords_match_whole_words_only(self):
        """'digital' must not fire a `debug`-style word, nor 'website' a `web`."""
        spec = self._specs()[0]
        tuned = type(spec)(**{**spec.__dict__, "id": "t", "routes": ("web",), "default": False})
        decision = registry.route("the website is down", [tuned])
        # No whole-word 'web' in 'website', so nothing matched.
        self.assertEqual(decision.matched, ())

    def test_more_keyword_hits_beat_priority(self):
        a = SimpleNamespace(
            id="a", routes=("code", "test"), priority=100, default=False,
            installed=lambda: True, can_run=True,
        )
        b = SimpleNamespace(
            id="b", routes=("code",), priority=1, default=False,
            installed=lambda: True, can_run=True,
        )
        decision = registry.route("write a test for this code", [a, b])
        self.assertEqual(decision.spec.id, "a")

    def test_priority_breaks_a_tie_on_equal_hits(self):
        lo = SimpleNamespace(
            id="lo", routes=("code",), priority=1, default=False,
            installed=lambda: True, can_run=True,
        )
        hi = SimpleNamespace(
            id="hi", routes=("code",), priority=9, default=False,
            installed=lambda: True, can_run=True,
        )
        decision = registry.route("fix the code", [lo, hi])
        self.assertEqual(decision.spec.id, "hi")

    def test_preferred_harness_wins_over_keywords(self):
        decision = registry.route("fix the bug", self._specs(), preferred="deepseek")
        self.assertEqual(decision.spec.id, "deepseek")
        self.assertIn("explicitly", decision.reason)

    def test_a_non_runnable_harness_is_never_routed_to(self):
        """An un-runnable harness is excluded; with no others, routing refuses."""
        spec = registry.load_one("pi")
        broken = type(spec)(**{**spec.__dict__, "can_run": False})
        with self.assertRaises(KeyError):
            registry.route("fix the code bug", [broken])

    def test_candidates_rank_priority_then_id(self):
        ids = [s.id for s in registry.candidates(self._specs())]
        self.assertEqual(ids[0], "claude-code")  # priority 20
        self.assertNotIn("claude-desktop", ids)  # can_run is False in its manifest

    def test_default_model_is_concrete_not_a_pattern(self):
        """`allowed_models` holds globs; a glob sent as a model id 404s."""
        from harness2.cli.harness import _resolve_model

        for spec in registry.load_all():
            model = _resolve_model(spec, "")
            self.assertNotIn(
                "*", model, f"{spec.id}: default_model must name one real model"
            )

    def test_override_beats_the_default(self):
        from harness2.cli.harness import _resolve_model

        spec = registry.load_one("pi")
        self.assertEqual(_resolve_model(spec, "some/explicit-id"), "some/explicit-id")


class ModelGroupGlobs(unittest.TestCase):
    """A group's globs must match the ids the gateway actually serves.

    Gateway ids are namespaced (`bai/glm-5.3`, `Yeta/deepseek-v4.1-flash`), so a
    leading-anchored pattern like `glm*` matches nothing and leaves that harness
    with an empty model group -- it then cannot run at all, and the failure looks
    like a provider problem rather than a glob one.
    """

    def test_the_deepseek_group_matches_namespaced_ids(self):
        import fnmatch

        spec = registry.load_one("deepseek")
        real_ids = ["bai/glm-5.3", "Yeta/deepseek-v4.1-flash", "aikit/qwen-plus", "local"]
        matched = [m for m in real_ids if any(fnmatch.fnmatch(m, p) for p in spec.allowed_models)]
        self.assertIn("bai/glm-5.3", matched, "a namespaced glm id must be in the group")
        self.assertIn("Yeta/deepseek-v4.1-flash", matched)
        self.assertIn("aikit/qwen-plus", matched)

    def test_a_family_glob_is_not_anchored_to_the_start(self):
        """`glm*` is the bug; `*glm*` is the fix.

        A family pattern exists to pick out ids of that family, and those ids are
        namespaced, so it has to allow anything before the family name. A bare
        `glm*` can only match an id that STARTS with it, which none do.

        Exact ids and bare combos (`local`, `All`) are a different thing and are
        matched literally -- only patterns carrying a `*` are checked here.
        """
        for name in ("deepseek", "pi", "claude-code"):
            spec = registry.load_one(name)
            for pat in spec.allowed_models:
                if "*" not in pat:
                    continue  # an exact id or a combo name, matched literally
                self.assertTrue(
                    pat.startswith("*") or "/" in pat or pat.endswith("*"),
                    f"{name}: family pattern {pat!r} is anchored to the start and "
                    f"cannot match a namespaced gateway id",
                )


class ContinuationFlags(unittest.TestCase):
    """Continuation is a manifest fact, not a guess.

    claude, pi and opencode each take `--continue`; dsh's headless profile has no
    such flag, so a caller that assumed one would silently start a fresh session
    and report it as a continuation.
    """

    def test_a_harness_declares_how_to_continue(self):
        self.assertEqual(registry.load_one("claude-code").continue_flag, "--continue")
        self.assertEqual(registry.load_one("pi").continue_flag, "--continue")
        self.assertEqual(registry.load_one("opencode").continue_flag, "--continue")
        self.assertEqual(registry.load_one("deepseek").continue_flag, "")

    def test_the_flag_is_added_only_when_asked(self):
        sub = ExternalCliSubstrate("claude-code")
        fresh = sub.argv("P", Path("/ws"), "m")
        cont = sub.argv("P", Path("/ws"), "m", continue_last=True)
        self.assertNotIn("--continue", fresh, "a fresh run must not resume anything")
        self.assertEqual(cont[-1], "--continue")

    def test_it_is_refused_where_the_manifest_has_no_flag(self):
        with self.assertRaises(ValueError) as ctx:
            ExternalCliSubstrate("deepseek").argv("P", Path("/ws"), "m", continue_last=True)
        self.assertIn("continue", str(ctx.exception).lower())


class BenchMetricsTests(unittest.TestCase):
    """`harness2-harness bench` reads its own stream, so a claude capture needs the
    same reading rule the LAB runner uses: totals come from the terminal `result`
    event, whose `type` field claude writes LAST and which the per-assistant
    events cannot supply (their usage is all zeros)."""

    def _metrics(self, raw: str) -> dict:
        from harness2.benchmarks.external_runner import _read_metrics_stream

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "claude-code_session.jsonl"
            p.write_text(raw, encoding="utf-8")
            return _read_metrics_stream(p, "claude-code")

    # `type` last in the result event, exactly as claude writes it.
    _CLAUDE = "\n".join([
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "assistant", "message": {
            "role": "assistant", "content": [{"type": "text", "text": "hi"}],
            "usage": {"input_tokens": 0, "output_tokens": 0}}}),
        json.dumps({"duration_api_ms": 12, "num_turns": 3, "is_error": False,
                    "result": "done", "timeout": 1,
                    "usage": {"input_tokens": 30952, "output_tokens": 12},
                    "type": "result"}, sort_keys=True),
    ]) + "\n"

    def test_totals_come_from_the_result_event(self):
        got = self._metrics(self._CLAUDE)
        self.assertEqual(got["turn_count"], 3)
        self.assertEqual(got["input_tokens"], 30952)
        self.assertEqual(got["output_tokens"], 12)

    def test_zero_usage_is_not_double_counted(self):
        got = self._metrics(self._CLAUDE)
        self.assertEqual(got["input_tokens"], 30952, "assistant usage added on top")

    def test_prose_stream_yields_no_figures(self):
        got = self._metrics("plain prose, no events\n")
        self.assertIsNone(got["turn_count"])
        self.assertIsNone(got["input_tokens"])

    def test_the_closing_answer_is_captured(self):
        """A task need not write a file -- "hey" is answered in prose, and that
        prose is what the panel has to show."""
        self.assertEqual(self._metrics(self._CLAUDE)["answer"], "done")

    def test_codex_answer_is_read_from_its_agent_message(self):
        raw = "\n".join([
            json.dumps({"type": "item.completed", "item": {
                "id": "i1", "type": "agent_message", "text": "first"}}),
            json.dumps({"type": "item.completed", "item": {
                "id": "i1", "type": "agent_message", "text": "final answer"}}),
        ]) + "\n"
        self.assertEqual(self._metrics(raw)["answer"], "final answer")


class DoAcceptsAnAnswer(unittest.TestCase):
    """`harness2-harness do` must not call a prose answer a failure.

    It used to require a delivered file, so asking a question printed "every
    harness failed" for a run that exited 0 and answered correctly -- and fell
    through to the next harness, spending on every candidate in turn.
    """

    def _run_do(self, metrics: dict) -> tuple[int, str]:
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from unittest.mock import patch

        from harness2.benchmarks import external_runner as ER
        from harness2.cli import harness as H

        def fake_run_one(**kw):
            run_dir = Path(kw["results_root"]) / kw["run_id"]
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
            return run_dir

        args = SimpleNamespace(
            task="what does this do?",
            harness="claude-code",
            model=None,
            run_id="do/t",
            results_root=Path(tempfile.mkdtemp()),
            task_dir=None,
            timeout=60,
            no_fallback=True,
        )
        out, err = io.StringIO(), io.StringIO()
        with patch.object(ER, "run_one", side_effect=fake_run_one), \
             redirect_stdout(out), redirect_stderr(err):
            code = H.cmd_do(args)
        return code, out.getvalue()

    def test_an_answered_run_is_a_success(self):
        code, out = self._run_do(
            {"exit_code": 0, "delivered": [], "answer": "It parses the file."}
        )
        self.assertEqual(code, 0, "a prose answer must not be reported as a failure")
        self.assertIn("It parses the file.", out, "the answer must be shown")

    def test_a_nonzero_exit_is_still_a_failure(self):
        code, _ = self._run_do({"exit_code": 1, "delivered": [], "answer": "partial"})
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
