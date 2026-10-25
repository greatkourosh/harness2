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

"""Regressions for the 9router backend wiring."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness2 import ninerouter
from harness2.substrate.opencode import OpencodeSubstrate


class NineRouterTests(unittest.TestCase):
    def test_model_id_strips_provider_prefix(self):
        self.assertEqual(ninerouter.model_id("9router/All"), "All")
        self.assertEqual(ninerouter.model_id("All"), "All")
        self.assertTrue(ninerouter.is_9router_model("9router/All"))
        self.assertFalse(ninerouter.is_9router_model("All"))

    def test_provider_block_declares_openai_compatible_provider(self):
        with patch.object(ninerouter.config, "NINEROUTER_BASE_URL", "http://x/v1"), patch.object(
            ninerouter.config, "NINEROUTER_API_KEY", "k"
        ):
            block = ninerouter.provider_block()
        provider = block["9router"]
        self.assertEqual(provider["npm"], "@ai-sdk/openai-compatible")
        self.assertEqual(provider["options"]["baseURL"], "http://x/v1")
        self.assertEqual(provider["options"]["apiKey"], "k")
        self.assertIn("All", provider["models"])

    def test_write_preserves_existing_global_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = ninerouter.config_path(home)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"model": "keep/me"}), encoding="utf-8")
            with patch.object(ninerouter.config, "NINEROUTER_BASE_URL", "http://x/v1"), patch.object(
                ninerouter.config, "NINEROUTER_API_KEY", "k"
            ):
                ninerouter.write_provider_config(home)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["model"], "keep/me")
            self.assertIn("9router", data["provider"])

    def test_opencode_config_home_is_isolated_from_user_config(self):
        """The pinned OpenCode rejects a global config written for a newer schema."""
        env = OpencodeSubstrate.oc_config_env({"PATH": "/usr/bin"})
        self.assertEqual(env["XDG_CONFIG_HOME"], str(ninerouter.config_home()))
        self.assertNotIn(str(Path.home() / ".config"), env["XDG_CONFIG_HOME"])

    def test_model_limits_carry_both_context_and_output(self):
        """OpenCode's `limit` requires both fields; a half-filled one is invalid."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = ninerouter.config_path(home)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"provider": {}}), encoding="utf-8")
            models = {"m": {"name": "m", "tool_call": True, "limit": {"context": 100, "output": 20}}}
            with patch.object(ninerouter.config, "NINEROUTER_BASE_URL", "http://x/v1"), patch.object(
                ninerouter.config, "NINEROUTER_API_KEY", "k"
            ):
                ninerouter.write_provider_config(home, models)
            data = json.loads(path.read_text(encoding="utf-8"))
            limit = data["provider"]["9router"]["models"]["m"]["limit"]
            self.assertEqual(limit, {"context": 100, "output": 20})


if __name__ == "__main__":
    unittest.main()
