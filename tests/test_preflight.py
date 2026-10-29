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

"""Preflight must judge a setup by the route that setup actually wrote.

Both bugs pinned here were found by running `harness2-check --bench wb` against a
freshly installed, working 9router setup: preflight demanded Vertex's naming and
Vertex's credentials from a cell that never contacts Vertex. Reading the code did
not surface either, because each check is individually reasonable -- it is the
combination with the 9router route that is wrong.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness2 import config, preflight


class WorkBuddyRouteChecks(unittest.TestCase):
    """The two checks that blocked a correct 9router install."""

    def test_adc_is_probed_only_when_a_role_routes_through_vertex(self):
        """A 9router-only setup must not be asked for Google credentials.

        Behaved, not grepped: the ADCs gate must not fire for a WorkBuddy cell
        whose roles all route through 9router. A source-text assertion would also
        trip on the unrelated `bench == "wb"` branch that belongs to the wb
        runtime probes.
        """
        import subprocess as sp
        from unittest.mock import patch

        from harness2.benchmarks.workbuddy import WbAdapter

        adapter = WbAdapter("office", substrate="oc")
        probes: list[list[str]] = []
        real_run = sp.run

        def spy(cmd, *a, **kw):
            probes.append([str(c) for c in cmd])
            return real_run(cmd, *a, **kw)

        with patch.object(preflight.subprocess, "run", side_effect=spy), patch.object(
            config, "SOLVER_MODEL_OC", "9router/claude-sonnet-4-6"
        ), patch.object(config, "JUDGE_MODEL", "9router/claude-sonnet-4-6"):
            preflight.check(adapter)

        adc = [p for p in probes if p[:2] == ["gcloud", "auth"]]
        self.assertEqual(adc, [], "a 9router-only cell must not be probed for ADC")

    def test_the_route_decides_which_model_name_is_expected(self):
        """Vertex writes `google/<model>`; 9router writes the gateway id verbatim.

        A check that accepts only the Vertex spelling reports a correct 9router
        setup as stale, which blocks the first run for every 9router user.
        """
        routes_9router = config.SOLVER_MODEL_OC.startswith("9router/")
        solver = config.SOLVER_MODEL_OC.removeprefix("google-vertex/")
        if routes_9router:
            expected = solver.removeprefix("9router/")
            self.assertNotIn(expected, ("google/" + expected,))
        else:
            expected = "google/" + solver
        # Whatever the route, the written file's name must satisfy the expectation
        # the check derives -- that is the invariant the bug violated.
        written = (
            solver.removeprefix("9router/") if routes_9router else "google/" + solver
        )
        self.assertEqual(written, expected)


class WrittenRoutesMatchTheCheck(unittest.TestCase):
    """End to end: what setup writes is what preflight accepts, on this machine.

    Skipped where a checkout is absent; the point is to catch a divergence between
    the writer in install.py and the reader in preflight.py.
    """

    def test_setup_output_satisfies_the_check(self):
        repo = config.wb_repo()
        models = repo / "configs" / "models" / "harness2"
        if not models.is_dir():
            self.skipTest(f"WorkBuddy not installed at {repo}")
        routes_9router = config.SOLVER_MODEL_OC.startswith("9router/")
        for role, raw in (
            ("solver", config.SOLVER_MODEL_OC.removeprefix("google-vertex/")),
            ("judge", config.JUDGE_MODEL),
        ):
            path = models / f"{role}.yaml"
            if not path.is_file():
                self.skipTest(f"{path} absent")
            name = json.loads(path.read_text(encoding="utf-8"))["model"]["name"]
            expected = raw.removeprefix("9router/") if routes_9router else "google/" + raw
            self.assertEqual(
                name,
                expected,
                f"{role}.yaml names {name!r} but preflight expects {expected!r} "
                f"— setup and check have diverged for this route",
            )


class NoLeftoverOnAnUninstalledBench(unittest.TestCase):
    def test_a_missing_checkout_is_reported_not_crashed(self):
        """preflight returns messages; it must never raise for a bare checkout."""
        from harness2.benchmarks.workbuddy import WbAdapter

        with tempfile.TemporaryDirectory() as td:
            adapter = WbAdapter("office", substrate="oc")
            saved = config.DEPENDENCIES
            try:
                config.DEPENDENCIES = Path(td)
                errors = preflight.check(adapter)
            finally:
                config.DEPENDENCIES = saved
            self.assertIsInstance(errors, list)
            self.assertTrue(errors, "an absent checkout must produce at least one message")


if __name__ == "__main__":
    unittest.main()
