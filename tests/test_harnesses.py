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
from unittest.mock import patch

from harness2.harnesses import adapters, registry


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


if __name__ == "__main__":
    unittest.main()
