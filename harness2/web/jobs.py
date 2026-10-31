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

"""Background tasks for the panel: several harnesses running at once.

The panel is a *view* over this store; nothing here knows about HTTP. A task
runs in its own thread so the page never blocks on a rollout, and the store is
readable while tasks are in flight — that is what lets the operator switch
between projects without waiting for one to finish.

Isolation matters here in a way it does not in the CLI. `do` runs one task and
the operator watches it; the panel accepts several at once against real
project directories, so a run must not be able to scribble over a working
tree. A git project is therefore cloned to a scratch directory and the harness
runs there (`git clone --shared` for speed); a non-git directory is used in
place, since there is no cheap way to copy it safely and the operator chose a
directory the tool can see.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from ..benchmarks import external_runner as ER
from ..harnesses import registry

PROJECTS_ROOT = Path("/media/kourosh/DEVNVME/projects")

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

@dataclass
class Job:
    """One task. Mutable because its whole life is a state transition."""

    id: str
    task: str
    project: str
    project_path: Path
    harness: str
    model: str
    routed_by: str
    workspace: Path
    cloned: bool
    state: str = "queued"  # queued | running | done | no_files | failed
    exit_code: int | None = None
    delivered: list[str] = field(default_factory=list)
    # The harness's closing message. A task that asks a question has no file to
    # point at, so its answer is what the panel must show.
    answer: str = ""
    # The job this one continues, if any; its workspace is shared with that job.
    continues: str = ""
    cancelled: bool = False
    error: str = ""
    started: str = ""
    finished: str = ""
    run_dir: Path | None = None

    def view(self) -> dict:
        """A JSON-safe snapshot; the page never sees a Path or a lock."""
        return {
            "id": self.id,
            "task": self.task,
            "project": self.project,
            "project_path": str(self.project_path),
            "harness": self.harness,
            "model": self.model,
            "routed_by": self.routed_by,
            "state": self.state,
            "exit_code": self.exit_code,
            "delivered": list(self.delivered),
            "answer": self.answer,
            "continues": self.continues,
            "cancelled": self.cancelled,
            "error": self.error,
            "started": self.started,
            "finished": self.finished,
            "cloned": self.cloned,
            "workspace": str(self.workspace),
            "run_dir": str(self.run_dir) if self.run_dir else "",
        }

class JobStore:
    """The running set, behind a lock, plus the thread that fills it.

    The list is persisted to `index.json` under the runs root, so restarting the
    panel no longer loses it. That matters for more than convenience: continuing a
    task needs the earlier task to still be in the list, and a restart used to
    take that ability away while leaving the workspaces on disk.
    """

    INDEX_NAME = "index.json"

    def __init__(self, *, root: Path | None = None):
        self.root = Path(root or config.RUNS / "web")
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._load()

    def join(self, timeout: float | None = None) -> None:
        """Wait for every job thread to finish.

        A caller that is about to remove the runs root -- a test's temp dir, say
        -- needs this: a thread still writing makes the removal fail, and the
        error says nothing about what was being tested.
        """
        for thread in list(self._threads.values()):
            if thread.is_alive():
                thread.join(timeout=timeout)

    # -- persistence ------------------------------------------------------

    @property
    def _index_path(self) -> Path:
        return self.root / self.INDEX_NAME

    def _load(self) -> None:
        """Read the index written by an earlier run. A missing or torn file is
        not an error: the panel simply starts with an empty list, as before."""
        try:
            raw = json.loads(self._index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, list):
            return
        for entry in raw:
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            try:
                job = Job(
                    id=str(entry["id"]),
                    task=str(entry.get("task") or ""),
                    project=str(entry.get("project") or ""),
                    project_path=Path(str(entry.get("project_path") or ".")),
                    harness=str(entry.get("harness") or ""),
                    model=str(entry.get("model") or ""),
                    routed_by=str(entry.get("routed_by") or ""),
                    workspace=Path(str(entry.get("workspace") or ".")),
                    cloned=bool(entry.get("cloned")),
                    run_dir=Path(entry["run_dir"]) if entry.get("run_dir") else None,
                    continues=str(entry.get("continues") or ""),
                )
            except (KeyError, TypeError, ValueError):
                continue
            # The rest of the record is restored from the saved view, so a
            # finished task keeps its answer and files across a restart.
            job.state = str(entry.get("state") or "done")
            job.exit_code = entry.get("exit_code")
            job.delivered = list(entry.get("delivered") or [])
            job.answer = str(entry.get("answer") or "")
            job.error = str(entry.get("error") or "")
            job.cancelled = bool(entry.get("cancelled"))
            job.started = str(entry.get("started") or "")
            job.finished = str(entry.get("finished") or "")
            # A task cannot have been left running by a process that is gone.
            if job.state in ("running", "queued"):
                job.state = "interrupted"
            self._jobs[job.id] = job
            self._order.append(job.id)

    def _save(self) -> None:
        """Write the index atomically; called with `_lock` held."""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self._index_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps([self._jobs[j].view() for j in self._order], indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._index_path)
        except OSError:
            # The list is a convenience, never a reason to fail a task.
            pass

    # -- reading ---------------------------------------------------------

    def list(self) -> list[dict]:
        with self._lock:
            return [self._jobs[j].view() for j in reversed(self._order)]

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.view() if job else None

    def log(self, job_id: str) -> str:
        """The harness's captured stream, read fresh so it streams as it grows."""
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None or job.run_dir is None:
            return ""
        for name in (f"{job.harness}_session.jsonl", f"{job.harness}_stderr.log"):
            path = job.run_dir / name
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")
                if text.strip():
                    return text[-40_000:]
        return ""

    # -- writing ---------------------------------------------------------

    def submit(
        self,
        task: str,
        project: str,
        *,
        harness: str = "",
        model: str = "",
        timeout: int = 3600,
        continue_from: str = "",
    ) -> dict:
        """Queue a task and start its thread; returns the job's first view.

        `continue_from` names an earlier job to continue: the new task runs in
        that job's workspace, with the files it produced still there, and the
        harness resumes its previous conversation there. A thread is the chain of
        jobs that carry the same workspace forward.
        """
        parent: Job | None = None
        if continue_from:
            with self._lock:
                parent = self._jobs.get(continue_from)
            if parent is None:
                raise KeyError(f"no task {continue_from!r} to continue")

        # Continuing keeps the project and harness of the task being continued:
        # a different project would have a different tree, and a different
        # harness has no conversation to resume.
        project_path = parent.project_path if parent else self._project_path(project)
        specs = registry.load_all()
        if parent:
            harness = parent.harness
        if harness:
            decision = registry.route(task, specs, preferred=harness)
        else:
            decision = registry.route(task, specs)

        job_id = uuid.uuid4().hex[:12]
        if parent:
            # Continue in the directory the earlier run ACTUALLY worked in, not
            # the clone it started from. A fresh run is seeded from the clone into
            # its own `<root>/<id>/workspace`, and every file it wrote is in there;
            # resuming the conversation somewhere else finds neither the files nor
            # the harness's own session, which it looks up by working directory.
            workspace, cloned = parent.run_dir / "workspace", parent.cloned
            project = parent.project  # the child shows the project it runs in
        else:
            workspace, cloned = self._prepare(project_path, job_id)
        run_dir = self.root / job_id
        job = Job(
            id=job_id,
            task=task,
            project=project,
            project_path=project_path,
            harness=decision.spec.id,
            model=model or self._model_for(decision.spec),
            routed_by=decision.reason,
            workspace=workspace,
            cloned=cloned,
            run_dir=run_dir,
            continues=parent.id if parent else "",
        )
        with self._lock:
            self._jobs[job_id] = job
            self._order.append(job_id)
            self._save()

        thread = threading.Thread(
            target=self._run, args=(job_id, timeout), name=f"job-{job_id}", daemon=True
        )
        with self._lock:
            self._threads[job_id] = thread
        thread.start()
        return job.view()

    def cancel(self, job_id: str) -> bool:
        """Stop a running job: kill its harness process group, then mark it failed.

        The runner records the harness pid in the run directory and starts it in
        its own session, so the group can be signalled from here even though the
        process belongs to another thread. SIGTERM goes first — the CLIs flush
        their session transcript on it, where SIGKILL would truncate the stream.
        """
        from ..benchmarks.external_runner import kill_run

        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.state not in ("queued", "running") or job.cancelled:
                return False
            run_dir = job.run_dir
            # Claim it before killing. `kill_run` waits for the process to die,
            # and during that wait the run thread finishes and marks the job
            # `failed` itself; a second state check afterwards would then see
            # `failed` and report the cancel as a no-op even though it worked.
            job.cancelled = True

        killed = False
        if run_dir is not None:
            killed = kill_run(run_dir)

        with self._lock:
            if job.state in ("queued", "running"):
                job.state = "failed"
            job.error = (
                "cancelled from the panel; the harness process was stopped"
                if killed
                else "cancelled from the panel; no harness process was running"
            )
            job.finished = _now()
            return True

    def forget(self, job_id: str) -> bool:
        with self._lock:
            if job_id not in self._jobs:
                return False
            job = self._jobs[job_id]
            if job.state in ("queued", "running"):
                return False
            del self._jobs[job_id]
            self._order.remove(job_id)
            self._save()
        return True

    # -- helpers ---------------------------------------------------------

    def _project_path(self, project: str) -> Path:
        """Resolve a project name under PROJECTS_ROOT, refusing to escape it."""
        root = PROJECTS_ROOT.resolve()
        candidate = (root / project).resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError(f"project {project!r} is outside {root}")
        if not candidate.is_dir():
            raise ValueError(f"no such project directory: {candidate}")
        return candidate

    def _prepare(self, project_path: Path, job_id: str) -> tuple[Path, bool]:
        """The directory a run works in: a clone for a git project, else in place."""
        if not (project_path / ".git").exists():
            return project_path, False
        dest = self.root / "_ws" / job_id
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)
        try:
            subprocess.run(
                ["git", "clone", "--shared", "--quiet", str(project_path), str(dest)],
                check=True,
                capture_output=True,
                timeout=120,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            raise RuntimeError(
                f"could not clone {project_path} to a scratch tree: {exc}"
            ) from exc
        return dest, True

    def _model_for(self, spec: registry.HarnessSpec) -> str:
        from ..cli.harness import _resolve_model

        return _resolve_model(spec, "")

    def _run(self, job_id: str, timeout: int) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.state = "running"
            job.started = _now()
            self._save()
            harness = job.harness
            model = job.model
            workspace = job.workspace
            run_dir = job.run_dir
            continues = job.continues

        try:
            ER.run_one(
                harness=harness,
                run_id=job_id,
                results_root=self.root,
                # A continuing task works IN the directory the earlier one left,
                # so its files are still there; a fresh task's own directory is
                # the source that run_one copies from. Either way the agent ends
                # up in `<root>/<id>/workspace`.
                task_dir=workspace,
                prompt=job.task,
                model=model,
                timeout=timeout,
                workspace=workspace if continues else None,
                continue_last=bool(continues),
            )
        except (ValueError, KeyError, OSError, RuntimeError) as exc:
            with self._lock:
                job.state = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
                job.finished = _now()
                self._save()
            return

        metrics = {}
        meta = Path(run_dir) / "metrics.json"
        if meta.is_file():
            try:
                metrics = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                metrics = {}

        delivered = metrics.get("delivered") or []
        exit_code = metrics.get("exit_code")
        answer = metrics.get("answer") or ""
        # A clean run is a SUCCESS whether or not it wrote a file. "hey" is
        # answered in prose, and that prose is the deliverable; a task that names
        # a file must also produce one. So: exit != 0 (or no metrics) is the only
        # failure, and an exit-0 run is `done` when it either delivered a file or
        # produced a reply. The reply is kept on the job so the panel can show
        # what was actually said instead of reporting an empty run.
        with self._lock:
            # A cancel already decided this job's outcome, and it may have done
            # so while this thread was still unwinding: the run directory can
            # hold files written before the kill. Letting the success path run
            # would report `done` for work the operator stopped, so a cancelled
            # job is left as the cancel wrote it.
            if job.cancelled:
                return
            job.exit_code = exit_code
            job.delivered = list(delivered)
            job.answer = answer
            if exit_code == 0 and (delivered or answer):
                job.state = "done"
            elif exit_code == 0:
                # Ran cleanly but said nothing and wrote nothing: surfaced as its
                # own state, not a failure, because nothing actually went wrong.
                job.state = "no_files"
            else:
                job.state = "failed"
                if not job.error:
                    job.error = (
                        f"exit {exit_code}" if exit_code is not None else "produced no metrics"
                    )
            job.finished = _now()
            self._save()

def projects() -> list[dict]:
    """Every project directory the panel can offer, newest activity first."""
    if not PROJECTS_ROOT.is_dir():
        return []
    rows = []
    for path in sorted(PROJECTS_ROOT.iterdir()):
        if not path.is_dir() or path.name.startswith("."):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        rows.append(
            {
                "name": path.name,
                "path": str(path),
                "git": (path / ".git").exists(),
                "mtime": mtime,
            }
        )
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    for row in rows:
        row["mtime"] = time.strftime("%Y-%m-%d", time.localtime(row["mtime"]))
    return rows

def harnesses() -> list[dict]:
    """Runnable harnesses, for the panel's picker."""
    return [
        {"id": s.id, "label": s.label, "default": s.default, "priority": s.priority}
        for s in registry.candidates(registry.load_all())
    ]
