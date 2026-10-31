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

from harness2.benchmarks import external_runner
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
                store.join(timeout=30)
                final = store.get(job["id"])
            self.assertEqual(final["state"], "failed")
            self.assertEqual(final["exit_code"], 1)
            self.assertIn("1", final["error"])

    def test_a_prose_answer_is_a_done_task(self):
        """A question answered in prose is FINISHED work, not a failure.

        "hey" writes no file; the reply is the deliverable. The job must end
        `done` and carry that reply, so the panel can show what was said instead
        of reporting an empty run and reading as a broken tool.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "proj").mkdir()

            def answered(**kw):
                run_dir = Path(kw["results_root"]) / kw["run_id"]
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "metrics.json").write_text(
                    json.dumps(
                        {"exit_code": 0, "delivered": [], "answer": "Hey. What you need?"}
                    ),
                    encoding="utf-8",
                )
                return run_dir

            store = self._store(root / "runs")
            with patch.object(jobs, "PROJECTS_ROOT", root), patch.object(
                jobs.ER, "run_one", side_effect=answered
            ):
                job = store.submit("hey", "proj")
                store.join(timeout=30)
                final = store.get(job["id"])
            self.assertEqual(final["state"], "done")
            self.assertEqual(final["answer"], "Hey. What you need?")
            self.assertEqual(final["error"], "", "a clean run must not carry an error")

    def test_exit_zero_with_no_answer_and_no_file_is_not_a_failure(self):
        """Silent but clean still ran: `no_files`, never `failed`."""
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
                store.join(timeout=30)
                final = store.get(job["id"])
            self.assertEqual(final["state"], "no_files")
            self.assertEqual(final["exit_code"], 0)

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
                # Wait for it to be RUNNING, not finished: the run_one stub holds
                # the job open until `release`, which is what makes cancel meaningful.
                for _ in range(200):
                    if store.get(job["id"])["state"] == "running":
                        break
                    time.sleep(0.02)
                self.assertFalse(store.forget(job["id"]), "a running job is not forgettable")
                self.assertTrue(store.cancel(job["id"]))
                release.set()
                store.join(timeout=30)
            self.assertTrue(store.forget(job["id"]))
            self.assertIsNone(store.get(job["id"]))

    def test_cancel_actually_kills_the_harness_process(self):
        """Cancel must stop the process, not only mark the job failed.

        The harness is started in its own session with its pid recorded, so the
        panel can signal the group from the HTTP thread. A cancel that reports
        success while the process keeps spending quota is the failure this
        guards.
        """
        import subprocess
        import time

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "proj").mkdir()
        store = self._store(root / "runs")
        seen: dict = {}

        def fake_run_one(**kw):
            run_dir = Path(kw["results_root"]) / kw["run_id"]
            run_dir.mkdir(parents=True, exist_ok=True)
            # A stand-in harness: a real process in its own session, like the runner.
            proc = subprocess.Popen(["sleep", "120"], start_new_session=True)
            (run_dir / "pid").write_text(str(proc.pid), encoding="utf-8")
            seen["pid"] = proc.pid
            time.sleep(30)
            return run_dir

        with patch.object(jobs, "PROJECTS_ROOT", root), patch.object(
            jobs.ER, "run_one", side_effect=fake_run_one
        ):
            job = store.submit("do something slow", "proj")
            for _ in range(200):
                if store.get(job["id"])["state"] == "running" and "pid" in seen:
                    break
                time.sleep(0.05)
            self.assertTrue(external_runner._alive(seen["pid"]), "stand-in should be running")

            self.assertTrue(store.cancel(job["id"]))
            time.sleep(0.3)

            self.assertFalse(
                external_runner._alive(seen["pid"]),
                "cancel must kill the harness process, not just mark the job",
            )
            final = store.get(job["id"])
            self.assertEqual(final["state"], "failed")
            self.assertTrue(final["cancelled"])
            self.assertIn("stopped", final["error"])
            subprocess.run(["pkill", "-f", "sleep 120"], check=False)

    def test_a_cancelled_job_is_not_flipped_back_to_done(self):
        """The run thread must not report success for work the operator stopped."""
        import time

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "proj").mkdir()
        store = self._store(root / "runs")

        def slow_then_success(**kw):
            run_dir = Path(kw["results_root"]) / kw["run_id"]
            run_dir.mkdir(parents=True, exist_ok=True)
            time.sleep(0.4)  # long enough for the cancel below to land first
            (run_dir / "metrics.json").write_text(
                json.dumps({"exit_code": 0, "delivered": ["late.txt"]}), encoding="utf-8"
            )
            return run_dir

        with patch.object(jobs, "PROJECTS_ROOT", root), patch.object(
            jobs.ER, "run_one", side_effect=slow_then_success
        ):
            job = store.submit("will be cancelled", "proj")
            # Running, not finished: cancel must land while the work is in flight.
            for _ in range(200):
                if store.get(job["id"])["state"] == "running":
                    break
                time.sleep(0.02)
            store.cancel(job["id"])
            store.join(timeout=30)  # let the run thread try to write `done`
            final = store.get(job["id"])
        self.assertEqual(final["state"], "failed", "a cancelled job must stay failed")
        self.assertTrue(final["cancelled"])


class ContinuationTests(unittest.TestCase):
    """Continuing a task runs it where the earlier one worked, and resumes its
    conversation.

    Two things have to line up and neither is visible from one place: the new
    task must work in the directory the earlier run's files are in (not the clone
    it was seeded from), and run_one must not try to copy that directory into
    itself. Both were wrong on the first attempt and only running it showed that.
    """

    def _store(self, root: Path) -> jobs.JobStore:
        return jobs.JobStore(root=root)

    def _quiet(self, store: jobs.JobStore) -> None:
        """Wait for every job thread, so a temp dir can be removed safely.

        Each submit starts a thread. Letting a temp dir be cleaned up while one
        is still writing makes the test flaky with `OSError: Directory not
        empty`, which says nothing about the behaviour under test.
        """
        store.join(timeout=30)

    def _seed_parent(self, store: jobs.JobStore, root: Path, tmp: Path) -> dict:
        """A finished job whose run dir holds a workspace with a file in it."""
        (root / "proj").mkdir(exist_ok=True)

        def success(**kw):
            run_dir = Path(kw["results_root"]) / kw["run_id"]
            (run_dir / "workspace" / "output").mkdir(parents=True, exist_ok=True)
            (run_dir / "workspace" / "output" / "plan.txt").write_text("step 2 here")
            (run_dir / "metrics.json").write_text(
                json.dumps({"exit_code": 0, "delivered": ["plan.txt"], "answer": "ok"}),
                encoding="utf-8",
            )
            return run_dir

        with patch.object(jobs, "PROJECTS_ROOT", root), patch.object(
            jobs.ER, "run_one", side_effect=success
        ):
            job = store.submit("write a plan", "proj")
            self._quiet(store)
        return store.get(job["id"])

    def test_continue_works_in_the_parents_run_workspace(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        store = self._store(root / "runs")
        parent = self._seed_parent(store, root, Path(tmp.name))

        seen: dict = {}

        def capture(**kw):
            seen.update(kw)
            run_dir = Path(kw["results_root"]) / kw["run_id"]
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "metrics.json").write_text(
                json.dumps({"exit_code": 0, "delivered": [], "answer": "did it"}),
                encoding="utf-8",
            )
            return run_dir

        with patch.object(jobs, "PROJECTS_ROOT", root), patch.object(
            jobs.ER, "run_one", side_effect=capture
        ):
            child = store.submit("now do step 2", "", continue_from=parent["id"])
            self._quiet(store)

        # The directory the parent actually worked in, where its file lives.
        self.assertEqual(Path(seen["workspace"]), Path(parent["run_dir"]) / "workspace")
        self.assertTrue(seen["continue_last"], "the harness must be told to continue")
        self.assertEqual(seen["task_dir"], Path(parent["run_dir"]) / "workspace")
        self.assertEqual(store.get(child["id"])["continues"], parent["id"])

    def test_continue_inherits_project_and_harness(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        store = self._store(root / "runs")
        parent = self._seed_parent(store, root, Path(tmp.name))
        child = store.submit("follow up", "", continue_from=parent["id"])
        self._quiet(store)
        self.assertEqual(child["project"], parent["project"])
        self.assertEqual(child["harness"], parent["harness"])

    def test_continuing_an_unknown_task_is_refused(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "proj").mkdir()
        store = self._store(root / "runs")
        with self.assertRaises(KeyError):
            store.submit("x", "proj", continue_from="nope00000000")


class PersistenceTests(unittest.TestCase):
    """The task list survives a panel restart.

    This is not only convenience: continuing a task needs the earlier task in the
    list, so a restart used to take that ability away while leaving the
    workspaces (and their files) on disk.
    """

    def _store(self, root: Path) -> jobs.JobStore:
        return jobs.JobStore(root=root)

    def _finished(self, store: jobs.JobStore, root: Path, tmp: Path) -> dict:
        (root / "proj").mkdir(exist_ok=True)

        def success(**kw):
            run_dir = Path(kw["results_root"]) / kw["run_id"]
            (run_dir / "workspace").mkdir(parents=True, exist_ok=True)
            (run_dir / "metrics.json").write_text(
                json.dumps(
                    {"exit_code": 0, "delivered": ["notes.txt"], "answer": "all done"}
                ),
                encoding="utf-8",
            )
            return run_dir

        with patch.object(jobs, "PROJECTS_ROOT", root), patch.object(
            jobs.ER, "run_one", side_effect=success
        ):
            job = store.submit("write notes", "proj")
            store.join(timeout=30)
        return store.get(job["id"])

    def test_a_restarted_store_reloads_finished_tasks(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        first = self._store(root / "runs")
        before = self._finished(first, root, Path(tmp.name))

        # A brand-new store over the same root: what a panel restart does.
        after = self._store(root / "runs")
        reloaded = after.get(before["id"])
        self.assertIsNotNone(reloaded, "the finished task must survive a restart")
        self.assertEqual(reloaded["state"], "done")
        self.assertEqual(reloaded["delivered"], ["notes.txt"])
        self.assertEqual(reloaded["answer"], "all done")
        self.assertEqual(reloaded["task"], before["task"])

    def test_a_task_can_be_continued_after_a_restart(self):
        """The reason persistence matters: the earlier task is still addressable."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        before = self._finished(self._store(root / "runs"), root, Path(tmp.name))

        after = self._store(root / "runs")

        def capture(**kw):
            run_dir = Path(kw["results_root"]) / kw["run_id"]
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "metrics.json").write_text(
                json.dumps({"exit_code": 0, "delivered": [], "answer": "ok"}),
                encoding="utf-8",
            )
            return run_dir

        with patch.object(jobs, "PROJECTS_ROOT", root), patch.object(
            jobs.ER, "run_one", side_effect=capture
        ):
            child = after.submit("follow up", "", continue_from=before["id"])
            after.join(timeout=30)
        self.assertEqual(after.get(child["id"])["continues"], before["id"])

    def test_a_task_left_running_is_not_reported_as_running(self):
        """A process that is gone cannot still be running its task."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        store = self._store(root / "runs")
        (root / "runs").mkdir(parents=True, exist_ok=True)
        (root / "runs" / "index.json").write_text(
            json.dumps([{
                "id": "abcd1234abcd", "task": "was mid-flight", "project": "proj",
                "project_path": str(root / "proj"), "harness": "claude-code",
                "model": "", "routed_by": "", "workspace": str(root / "ws"),
                "cloned": False, "run_dir": str(root / "runs" / "abcd1234abcd"),
                "state": "running", "exit_code": None, "delivered": [],
            }]),
            encoding="utf-8",
        )
        reloaded = self._store(root / "runs").get("abcd1234abcd")
        self.assertEqual(reloaded["state"], "interrupted")

    def test_a_torn_index_starts_empty_rather_than_failing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "runs").mkdir(parents=True, exist_ok=True)
        (root / "runs" / "index.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self._store(root / "runs").list(), [])


if __name__ == "__main__":
    unittest.main()
