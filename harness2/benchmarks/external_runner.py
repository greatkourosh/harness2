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

"""Run one task through a registered harness and write a substrate run directory.

    python -m harness2.benchmarks.external_runner \\
        --harness pi --run-id demo/pi/base1 \\
        --results-root runs/lab/results/pi \\
        --task-dir /path/to/task_folder --prompt "…" --model 9router/All

Produces the same layout the OpenCode and Codex runners do, so a downstream
reader (`trajectory`, `delivered`, `metrics.json`) does not care which harness
ran:

    <results-root>/<run-id>/
        workspace/          the task's own files, plus output/, which is graded
        workspace/documents/ read-only source material
        output/             the graded copy of workspace/output (what was delivered)
        config.json         the harness, model and resolved argv for this run
        <harness>_session.jsonl   the captured stream
        <harness>_stderr.log
        metrics.json        turn/token/wall figures, written LAST as the completion mark

Deliberately schema-light: harnesses differ in how they print events, so the
stream is captured raw and the substrate's renderer makes what it can of it.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from ..harnesses import registry as HREG
from ..substrate.external_cli import ExternalCliSubstrate

# Prepended to every task prompt so the deliverable lands where grading reads it.
WORKSPACE_NOTE = (
    "=== WORKSPACE ===\n{workspace}\n\n"
    "=== OUTPUT DIRECTORY (deliverables go here) ===\n{output}\n\n"
)

def build_prompt(prompt: str, workspace: Path) -> str:
    return WORKSPACE_NOTE.format(workspace=workspace, output=workspace / "output") + prompt

def _read_metrics_stream(session: Path, harness: str) -> dict:
    """Best-effort turn/token counts from a harness's JSONL stream.

    Field names differ per harness, so each key is looked up under the several
    names the harnesses here use, and a miss is None rather than an error --
    metrics are diagnostics, never a pass/fail signal.
    """
    if not session.is_file():
        return {}
    turns = 0
    tokens_in = tokens_out = 0
    # claude's terminal `result` event carries the authoritative totals for the whole
    # session (`num_turns` plus one usage object); its per-assistant events report an
    # all-zero usage and no turn marker. Reading those totals first keeps the figures
    # right AND avoids double-counting should a harness report both.
    declared_turns = None
    final_usage = None
    for line in session.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type") or ev.get("event") or ""
        if etype in ("step_finish", "turn.completed", "message_stop"):
            turns += 1
        if isinstance(ev.get("num_turns"), int):
            declared_turns = ev["num_turns"]
        usage = ev.get("usage") or (ev.get("part") or {}).get("tokens")
        if isinstance(usage, dict):
            tokens_in += int(usage.get("input") or usage.get("input_tokens") or 0)
            tokens_out += int(usage.get("output") or usage.get("output_tokens") or 0)
            if etype == "result":
                final_usage = (
                    int(usage.get("input") or usage.get("input_tokens") or 0),
                    int(usage.get("output") or usage.get("output_tokens") or 0),
                )
    if final_usage is not None:
        tokens_in, tokens_out = final_usage
    return {
        "turn_count": declared_turns or turns or None,
        "input_tokens": tokens_in or None,
        "output_tokens": tokens_out or None,
        "answer": _final_text(session) or None,
    }


def _final_text(session: Path) -> str:
    """The harness's own closing message, whatever it calls it.

    A task need not write a file: "hey" is answered in prose, and that prose IS
    the deliverable. Without this the panel can only report the file names a run
    produced, so a conversational task looks like it produced nothing. Every
    shape here carries its closing text in a different place, so each is read
    where it actually lives rather than guessed at.
    """
    if not session.is_file():
        return ""
    raw = session.read_text(encoding="utf-8", errors="replace")
    last_result, last_item_text = "", ""
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type") or ev.get("event") or ""
        # claude: the terminal result event's `result` is the closing message.
        if etype == "result" and isinstance(ev.get("result"), str) and ev["result"].strip():
            last_result = ev["result"]
            continue
        # codex and opencode: a thread item / part carrying text.
        item = ev.get("item") if isinstance(ev.get("item"), dict) else ev.get("part")
        if isinstance(item, dict):
            itype = item.get("type", "")
            text = item.get("text") or item.get("content")
            if itype in ("agent_message", "text", "message") and isinstance(text, str) \
                    and text.strip():
                last_item_text = text
    return (last_result or last_item_text).strip()

def run_one(
    *,
    harness: str,
    run_id: str,
    results_root: Path,
    task_dir: Path,
    prompt: str,
    model: str,
    timeout: int = 3600,
) -> Path:
    """Run `prompt` through `harness` in a fresh workspace; return the run dir."""
    spec = HREG.load_one(harness)
    if not spec.can_run:
        raise ValueError(f"{harness} has no non-interactive mode")
    substrate = ExternalCliSubstrate(harness)

    run_dir = Path(results_root) / run_id
    workspace = run_dir / "workspace"
    output_dir = workspace / "output"
    run_dir.mkdir(parents=True, exist_ok=True)

    if task_dir.is_dir():
        shutil.copytree(task_dir, workspace, dirs_exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    full_prompt = build_prompt(prompt, workspace)
    argv = substrate.argv(full_prompt, workspace, model)
    env = substrate.exec_env(os.environ)

    session = run_dir / f"{harness}_session.jsonl"
    stderr_log = run_dir / f"{harness}_stderr.log"
    config_json = {
        "runner": harness,
        "label": Path(run_id).name,
        "model": model,
        "run_id": run_id,
        "argv": argv,
        "workspace": str(workspace),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    (run_dir / "config.json").write_text(json.dumps(config_json, indent=2), encoding="utf-8")

    started = time.time()
    with session.open("w", encoding="utf-8") as out, stderr_log.open("w", encoding="utf-8") as err:
        try:
            # start_new_session puts the harness in a process group of its own so
            # a cancel can kill the whole tree (the CLI plus anything it spawns)
            # without touching this process. The pid is recorded so the panel can
            # find that group again from another thread.
            proc = subprocess.Popen(
                argv,
                cwd=str(workspace),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
            (run_dir / "pid").write_text(str(proc.pid), encoding="utf-8")
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                # Give it SIGTERM, then reap. `wait` again briefly: a killed child
                # is a zombie until reaped, and without this the pid file would
                # point at a process the panel can no longer signal.
                _kill_group(proc.pid, signal.SIGTERM)
                try:
                    code = proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    _kill_group(proc.pid, signal.SIGKILL)
                    code = proc.wait(timeout=30)
                code = 124
        except OSError as exc:
            code = 127
            err.write(f"could not exec {argv[0]}: {exc}\n")

    # The graded copy: what the task actually delivered, mirroring the other runners.
    graded = run_dir / "output"
    graded.mkdir(parents=True, exist_ok=True)
    for src in output_dir.rglob("*"):
        if src.is_file():
            dest = graded / src.relative_to(output_dir)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)

    metrics = {
        "runner": harness,
        "model": model,
        "run_id": run_id,
        "wall_clock_seconds": round(time.time() - started, 1),
        "finished_cleanly": code == 0,
        "exit_code": code,
        "delivered": sorted(p.name for p in graded.rglob("*") if p.is_file()),
        **_read_metrics_stream(session, harness),
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    # Written last: its presence is the completion mark readers check.
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    return run_dir

def _kill_group(pid: int, sig: int) -> bool:
    """Signal the process group led by `pid`. True when a signal was delivered.

    The harness was started with `start_new_session=True`, so its pid is also
    its process-group id. SIGTERM first: the CLIs here flush their session log
    and exit on it, where SIGKILL would leave a truncated stream.
    """
    try:
        os.killpg(os.getpgid(pid), sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False

def kill_run(run_dir: Path, *, force: bool = False) -> bool:
    """Stop the harness a run directory belongs to. False when nothing was running.

    Reads the pid the runner recorded, so this works from any process — the
    panel cancels a task from an HTTP handler while the run is in another
    thread. The transcript is written before the process ends only if the CLI
    flushes on SIGTERM, which the ones here do.
    """
    pid_file = Path(run_dir) / "pid"
    if not pid_file.is_file():
        return False
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False

    if not _kill_group(pid, signal.SIGTERM if not force else signal.SIGKILL):
        return False
    if force:
        return True
    # Give it a moment to exit cleanly, then insist.
    for _ in range(30):
        time.sleep(0.1)
        if not _alive(pid):
            return True
    _kill_group(pid, signal.SIGKILL)
    return True

def _alive(pid: int) -> bool:
    """Whether `pid` is a running process.

    A killed child stays visible in /proc as a zombie until its parent reaps it,
    and the parent here is the caller's own thread, which is busy polling. A
    zombie is dead for our purposes — reporting it alive would make the caller
    wait out its full grace period and then send a SIGKILL to nothing.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            fields = handle.read().rsplit(b") ", 1)
        if len(fields) == 2 and fields[1][:1] == b"Z":
            return False
        return True
    except FileNotFoundError:
        return False
    except OSError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but belongs to another user; it is alive.
        return True
    except OSError:
        return False

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--harness", required=True, choices=[s.id for s in HREG.load_all()])
    p.add_argument("--run-id", required=True)
    p.add_argument("--results-root", required=True, type=Path)
    p.add_argument("--task-dir", type=Path, help="copied into the workspace; may be empty")
    p.add_argument("--prompt", required=True)
    p.add_argument(
        "--model",
        default="",
        help="gateway model id; default is the harness's first allowed model",
    )
    p.add_argument("--timeout", type=int, default=3600)
    args = p.parse_args(argv)

    spec = HREG.load_one(args.harness)
    model = args.model or (spec.allowed_models[0] if spec.allowed_models else "")
    if not model:
        print(f"{args.harness}: no model given and its manifest allows none", file=sys.stderr)
        return 2
    if not spec.can_run:
        print(f"{args.harness}: no non-interactive mode", file=sys.stderr)
        return 2
    run_dir = run_one(
        harness=args.harness,
        run_id=args.run_id,
        results_root=args.results_root,
        task_dir=args.task_dir or Path("/nonexistent"),
        prompt=args.prompt,
        model=model,
        timeout=args.timeout,
    )
    print(f"run dir: {run_dir}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
