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

"""Regressions for the task panel: isolation, job lifecycle, project listing."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness2.web import jobs


class ProjectListingTests(unittest.TestCase):
    def test_projects_lists_directories_with_a_git_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "alpha").mkdir()
            (root / "beta").mkdir()
            (root / "beta/.git").mkdir()
            (root / ".hidden").mkdir()
            (root / "afile").write_text("x", encoding="utf-8")
            with patch.object(jobs, "PROJECTS_ROOT", root):
                rows = jobs.projects()
            names = {r["name"] for r in rows}
            self.assertEqual(names, {"alpha", "beta"})
            self.assertNotIn(".hidden", names, "dot-directories are not projects")
            self.assertTrue(next(r for r in rows if r["name"] == "beta")["git"])
            self.assertFalse(next(r for r in rows if r["name"] == "alpha")["git"])

    def test_projects_is_empty_when_the_root_is_absent(self):
        with patch.object(jobs, "PROJECTS_ROOT", Path("/nonexistent-root-xyz")):
            self.assertEqual(jobs.projects(), [])

class JobStoreTests(unittest.TestCase):
    def _store(self, root: Path) -> jobs.JobStore:
        return jobs.JobStore(root=root)

    def test_project_name_cannot_escape_the_root(self):
        """A traversal in the project field must not resolve outside the root."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = self._store(root / "runs")
            with patch.object(jobs, "PROJECTS_ROOT", root):
                with self.assertRaises(ValueError):
                    store._project_path("../etc")

    def test_unknown_project_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = self._store(root / "runs")
            with patch.object(jobs, "PROJECTS_ROOT", root):
                with self.assertRaises(ValueError):
                    store._project_path("nope")

    def test_a_git_project_is_cloned_and_the_original_is_untouched(self):
        """The whole point of the clone: a run cannot write to the real tree."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "proj"
            project.mkdir()
            subprocess.run(["git", "init", "-q", str(project)], check=True)
            (project / "keep.txt").write_text("original", encoding="utf-8")

            store = self._store(root / "runs")
            workspace, cloned = store._prepare(project, "job1")
            self.assertTrue(cloned)
            self.assertNotEqual(workspace, project)

            # What a harness would do: write into its workspace.
            (workspace / "written.txt").write_text("by the agent", encoding="utf-8")
            self.assertFalse(
                (project / "written.txt").exists(),
                "the agent's file must not appear in the real project",
            )
            self.assertEqual((project / "keep.txt").read_text(encoding="utf-8"), "original")

    def test_a_non_git_project_runs_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plain = root / "plain"
            plain.mkdir()
            store = self._store(root / "runs")
            workspace, cloned = store._prepare(plain, "job1")
            self.assertFalse(cloned)
            self.assertEqual(workspace, plain)

    def test_submit_records_the_job_before_the_thread_runs(self):
        """The page must see a queued job immediately, not after it finishes."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "proj").mkdir()
            store = self._store(root / "runs")
            with patch.object(jobs, "PROJECTS_ROOT", root), patch.object(
                jobs.ER, "run_one", side_effect=lambda **kw: Path(kw["results_root"]) / kw["run_id"]
            ):
                job = store.submit("do a thing", "proj")
                self.assertIn(job["id"], [j["id"] for j in store.list()])
                self.assertEqual(job["project"], "proj")
                self.assertFalse(job["cloned"])

    def test_a_failed_run_is_marked_failed_not_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "proj").mkdir()

            def fake_run_one(**kw):
                run_dir = Path(kw["results_root"]) / kw["run_id"]
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "metrics.json").write_text(
                    json.dumps({"exit_code": 1, "delivered": []}), encoding="utf-8"
                )
                return run_dir

            store = self._store(root / "runs")
            with patch.object(jobs, "PROJECTS_ROOT", root), patch.object(
                jobs.ER, "run_one", side_effect=fake_run_one
            ):
                job = store.submit("fail please", "proj")
                for _ in range(200):
                    state = store.get(job["id"])["state"]
                    if state in ("done", "failed"):
                        break
                    import time

                    time.sleep(0.02)
                final = store.get(job["id"])
            self.assertEqual(final["state"], "failed")
            self.assertEqual(final["exit_code"], 1)
            self.assertIn("1", final["error"])

    def test_success_needs_both_exit_zero_and_a_deliverable(self):
        """Exit 0 with nothing delivered is the silent-success case; not a pass."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "proj").mkdir()

            def empty_success(**kw):
                run_dir = Path(kw["results_root"]) / kw["run_id"]
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "metrics.json").write_text(
                    json.dumps({"exit_code": 0, "delivered": []}), encoding="utf-8"
                )
                return run_dir

            store = self._store(root / "runs")
            with patch.object(jobs, "PROJECTS_ROOT", root), patch.object(
                jobs.ER, "run_one", side_effect=empty_success
            ):
                job = store.submit("nothing doing", "proj")
                for _ in range(200):
                    if store.get(job["id"])["state"] in ("done", "failed"):
                        break
                    import time

                    time.sleep(0.02)
                final = store.get(job["id"])
            self.assertEqual(final["state"], "failed")

    def test_forget_removes_a_finished_job_only(self):
        """A job still in flight must not be dropped from the list under the UI."""
        import time

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "proj").mkdir()
            store = self._store(root / "runs")
            release = __import__("threading").Event()

            def slow(**kw):
                release.wait(5)  # held so the job stays running while we forget
                return Path(kw["results_root"]) / kw["run_id"]

            with patch.object(jobs, "PROJECTS_ROOT", root), patch.object(
                jobs.ER, "run_one", side_effect=slow
            ):
                job = store.submit("x", "proj")
                for _ in range(200):
                    if store.get(job["id"])["state"] == "running":
                        break
                    time.sleep(0.02)
                self.assertFalse(store.forget(job["id"]), "a running job is not forgettable")
                self.assertTrue(store.cancel(job["id"]))
                release.set()
                for _ in range(200):
                    if store.get(job["id"])["state"] == "failed":
                        break
                    time.sleep(0.02)
            self.assertTrue(store.forget(job["id"]))
            self.assertIsNone(store.get(job["id"]))

if __name__ == "__main__":
    unittest.main()
