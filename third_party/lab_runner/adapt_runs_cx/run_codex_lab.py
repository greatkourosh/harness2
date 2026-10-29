#!/usr/bin/env python3
"""Run one LAB task through a registered external harness, or the Codex CLI.

With no --harness-id this is the codex cell: codex_session.jsonl, config.json, output/
and metrics.json last as the completion record, so a dead model chain is never cached
as a finished rollout.

With --harness-id it drives ANY registered harness (see harness2-harness) by running the
argv the caller built from the harness manifest, in the same LAB workspace. The workspace,
document staging, read_doc.py install, grading copy and metrics layout are identical; only
the argv, the temp config home, the session filename and the per-harness environment differ.
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

RUNNER_DIR = Path(__file__).resolve().parent
REPO = RUNNER_DIR.parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from external.tts_lab import config as C  # noqa: E402

SCRATCH_BASE = Path(os.environ.get("ADAPT_LABCX_SCRATCH")
                    or (RUNNER_DIR / "scratch"))
RESULTS_ROOT = Path(os.environ.get("ADAPT_LABCX_RESULTS", str(RUNNER_DIR / "results")))
CODEX_BIN = Path(os.environ.get("ADAPT_LABCX_CODEX_BIN",
                                str(Path.home() / ".local" / "bin" / "codex")))
BASE_URL = os.environ.get("ADAPT_LABCX_BASE_URL", "")
VERTEX_PROXY_KEY = os.environ.get("VERTEX_PROXY_KEY", "")
DEFAULT_MODEL = os.environ.get("ADAPT_LABCX_MODEL", "")

READ_DOC = REPO / "opencode" / "scripts" / "read_doc.py"

# The adapter builds an external harness's argv with ExternalCliSubstrate.argv, which
# substitutes the manifest's {prompt} slot. The prompt embeds the workspace's ABSOLUTE
# paths, and only this runner knows the workspace (it is a per-run scratch dir), so the
# adapter passes this sentinel in the prompt slot and the runner substitutes the real
# prompt here. Keeping the substitution here means argv placement stays in ONE place.
PROMPT_SENTINEL = "@@HARNESS2_PROMPT@@"

# The codex_root surface; sub_agents/ never reaches the workspace (c5 is gated off on
# this substrate).
HARNESS_ENTRIES = ("AGENTS.md", ".agents", "scripts", ".codex")

CONFIG_TOML_TEMPLATE = """\
# Per-run CODEX_HOME (throwaway; codex writes a [projects] trust entry into this file).
# Endpoint and model are supplied by the adapter from user configuration.
model = "{model}"
model_provider = "harness2"

[model_providers.harness2]
name = "Configured Responses endpoint"
base_url = "{base_url}"
wire_api = "responses"
env_key = "VERTEX_PROXY_KEY"

[projects."{workspace}"]
trust_level = "trusted"
"""


def _require_scratch(p: Path, what: str) -> Path:
    """Refuse to place any temp artifact outside the configured scratch root."""
    rp = Path(p).resolve()
    base = SCRATCH_BASE.resolve()
    if base != rp and base not in rp.parents:
        raise SystemExit(f"refusing to run: {what} resolves to {rp}, outside the scratch root "
                         f"{base} -- temp state must live under ADAPT_LABCX_SCRATCH")
    return rp


def scratch_paths(label: str, pid: int | None = None) -> tuple[Path, Path, Path]:
    """(tmp_root, workspace, codex_home) for one rollout, all under the scratch root."""
    pid = os.getpid() if pid is None else pid
    tmp_root = _require_scratch(SCRATCH_BASE / "lab" / f"{label}__{pid}", "workspace root")
    codex_home = _require_scratch(SCRATCH_BASE / "codex_home" / f"{label}__{pid}",
                                  "CODEX_HOME")
    return tmp_root, tmp_root / "ws", codex_home


def run_dir_for(run_id: str) -> Path:
    """<results_root>/<run_id>, with path escapes refused."""
    rid = str(run_id).strip()
    if not rid or rid.startswith("/") or ".." in rid.split("/"):
        raise SystemExit(f"bad --run-id {run_id!r}: must be a relative, escape-free id")
    root = Path(RESULTS_ROOT).resolve()
    if str(root).startswith("/tmp/"):
        raise SystemExit(f"refusing results root under /tmp: {root}")
    d = (root / rid).resolve()
    if root not in d.parents:
        raise SystemExit(f"run id escapes the results root: {run_id!r}")
    return d


def build_prompt(task_id: str, workspace: Path) -> str:
    """The assignment message: the instructions verbatim plus the concrete absolute paths."""
    instructions = str(C.task_json(task_id).get("instructions", "")).strip()
    return (
        f"=== WORKSPACE ===\n{workspace}\n\n"
        f"=== DOCUMENTS (read-only) ===\n{workspace / 'documents'}\n\n"
        f"=== OUTPUT DIRECTORY (deliverables go here) ===\n{workspace / 'output'}\n\n"
        f"=== ASSIGNMENT ===\n{instructions}"
    )


def install_harness(ws: Path, harness_dir: Path | None) -> list[str]:
    """Merge the rendered harness into the workspace root; return the installed paths.

    The runtime's read_doc.py is installed last so no harness file can shadow it.
    """
    installed: list[str] = []
    if harness_dir is not None:
        hd = Path(harness_dir)
        for entry in HARNESS_ENTRIES:
            src = hd / entry
            if not src.exists():
                continue
            if src.is_file():
                shutil.copy2(src, ws / entry)
                installed.append(entry)
                continue
            for f in sorted(src.rglob("*")):
                if not f.is_file() or "__pycache__" in f.parts:
                    continue
                rel = f.relative_to(hd)
                dest = ws / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest)
                installed.append(str(rel))
    (ws / "scripts").mkdir(parents=True, exist_ok=True)
    if not READ_DOC.is_file():
        raise SystemExit(f"runtime document reader missing: {READ_DOC} -- without it every "
                         f"binary document 'read' silently returns noise")
    shutil.copy2(READ_DOC, ws / "scripts" / "read_doc.py")   # LAST: runtime wins
    return installed


def _int_or_zero(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _iter_codex_events(raw: str):
    """Yield codex's own event objects: only lines that lead with `{"type":`.

    Codex serialises `type` first, so this is its exact shape and stays as it was.
    Claude does NOT (its `result` event leads with `duration_api_ms`), which is why
    the claude branch below reads with `_iter_any_events` instead.
    """
    for ln in raw.replace("\x00", "").splitlines():
        ln = ln.strip()
        if not ln.startswith('{"type":'):
            continue
        try:
            ev = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            yield ev


def _iter_any_events(raw: str):
    """Yield every JSON object line regardless of field order."""
    for ln in raw.replace("\x00", "").splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            ev = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            yield ev


def _looks_like_claude_stream(raw: str) -> bool:
    """Whether this stream is `claude -p --output-format stream-json` rather than codex.

    The two are told apart by their terminal and assistant events: codex emits
    `item.completed`/`turn.completed` payloads, claude a `result` event and
    `assistant` events that carry a nested `message` object. Nothing else differs
    enough to be worth keying on.
    """
    for ev in _iter_any_events(raw):
        et = ev.get("type")
        if et == "result":
            return True
        if et == "assistant" and isinstance(ev.get("message"), dict):
            return True
    return False


def _parse_codex_events(raw: str) -> dict:
    """Summarise a `codex exec --json` stream (last state per item wins).

    Codex reports usage per turn and omits it from its items, so summing every
    `turn.completed` is the only way to total it.
    """
    events: dict[str, dict] = {}
    order: list[str] = []
    turns = 0
    usage_sums = {"input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0,
                  "cached_input_tokens": 0}
    n_other = 0
    for ev in _iter_codex_events(raw):
        et = ev.get("type", "")
        item = ev.get("item")
        if isinstance(item, dict) and item.get("id") is not None:
            iid = str(item["id"])
            if iid not in events:
                order.append(iid)
            events[iid] = item
            continue
        n_other += 1
        if et == "turn.completed":
            turns += 1
            u = ev.get("usage") or {}
            for k in usage_sums:
                usage_sums[k] += _int_or_zero(u.get(k))
    items = [events[i] for i in order]
    commands = [str(it.get("command") or "") for it in items
                if it.get("type") == "command_execution"]
    final_text = ""
    for it in items:
        if it.get("type") == "agent_message" and str(it.get("text") or "").strip():
            final_text = str(it["text"])
    return {
        "n_events": len(items) + n_other,
        "n_items": len(items),
        "tool_calls": len(commands),
        "commands": commands,
        "errors": sum(1 for it in items if it.get("type") == "error"),
        "turns": turns,
        "final_text": final_text,
        **usage_sums,
    }


def _parse_claude_events(raw: str) -> dict:
    """Summarise a `claude -p --output-format stream-json` stream.

    Usage and turn count come from the single terminal `result` event, which is the
    only event carrying them: the per-assistant events report an all-zero usage
    object, so summing them (as the codex branch does per turn) would report zero,
    and summing them alongside `result` would double-count. `usage_present` is False
    when the stream ended without such an event -- a run cut short mid-turn -- so a
    caller can say the figures are ABSENT rather than let zeroes read as real.
    """
    tool_calls = 0
    errors = 0
    n_events = 0
    final_text = ""
    result_ev: dict | None = None
    for ev in _iter_any_events(raw):
        n_events += 1
        et = ev.get("type")
        if et == "assistant":
            msg = ev.get("message")
            if isinstance(msg, dict):
                for block in msg.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        tool_calls += 1
                    elif block.get("type") == "text":
                        txt = str(block.get("text") or "")
                        if txt.strip():
                            final_text = txt
            # An auth or transport failure arrives as an `error` field on the event.
            if ev.get("error"):
                errors += 1
            continue
        if et == "result":
            result_ev = ev
            continue
        if et == "error" or isinstance(ev.get("error"), (dict, str)):
            errors += 1

    usage_sums = {"input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0,
                  "cached_input_tokens": 0}
    turns = 0
    usage_present = False
    if isinstance(result_ev, dict):
        turns = _int_or_zero(result_ev.get("num_turns"))
        u = result_ev.get("usage")
        if isinstance(u, dict):
            usage_present = True
            details = u.get("output_tokens_details")
            usage_sums["input_tokens"] = _int_or_zero(u.get("input_tokens"))
            usage_sums["output_tokens"] = _int_or_zero(u.get("output_tokens"))
            usage_sums["reasoning_output_tokens"] = _int_or_zero(
                (details or {}).get("thinking_tokens") if isinstance(details, dict) else 0)
            # Both cache figures are input the model read back, so they belong together.
            usage_sums["cached_input_tokens"] = (
                _int_or_zero(u.get("cache_read_input_tokens"))
                + _int_or_zero(u.get("cache_creation_input_tokens")))
        txt = result_ev.get("result")
        if isinstance(txt, str) and txt.strip():
            final_text = txt
        if result_ev.get("is_error"):
            errors += 1
    return {
        "n_events": n_events,
        "n_items": 0,
        "tool_calls": tool_calls,
        "commands": [],
        "errors": errors,
        "turns": turns,
        "final_text": final_text,
        "usage_present": usage_present,
        **usage_sums,
    }


def parse_session(session_path: Path) -> dict:
    """Summarise a session stream for metrics.json, codex or claude shaped."""
    try:
        raw = session_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raw = ""
    if _looks_like_claude_stream(raw):
        return _parse_claude_events(raw)
    return _parse_codex_events(raw)


def doc_coverage(summary: dict, docs_dir: Path, session_text: str) -> tuple[int, int]:
    """How many task documents the agent touched, by path substring in the raw session."""
    if not docs_dir.is_dir():
        return 0, 0
    rels = [str(p.relative_to(docs_dir)) for p in sorted(docs_dir.rglob("*")) if p.is_file()]
    read = sum(1 for r in rels if r in session_text)
    return read, len(rels)


def reap(hours: float) -> int:
    """Remove orphaned per-rollout scratch dirs older than `hours` (kill -9 leftovers)."""
    cutoff = time.time() - hours * 3600
    n = 0
    for sub in ("lab", "codex_home"):
        base = SCRATCH_BASE / sub
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    _require_scratch(d, "reap target")
                    shutil.rmtree(d, ignore_errors=True)
                    n += 1
                    print(f"reaped {d}")
            except OSError:
                continue
    return n


def main(args) -> int:
    if args.reap_hours is not None:
        print(f"reaped {reap(args.reap_hours)} stale rollout dirs under {SCRATCH_BASE}")
        return 0
    if not args.task or not args.run_id:
        raise SystemExit("--task and --run-id are required (or use --reap-hours)")

    # An external harness brings its own argv (built from its manifest by the adapter)
    # and reaches the gateway through its own already-applied config, so neither the
    # codex binary nor a throwaway CODEX_HOME is involved.
    harness_id = (args.harness_id or "codex").strip()
    harness_argv = json.loads(args.harness_argv_json) if args.harness_argv_json else None
    harness_env = json.loads(args.harness_env_json) if args.harness_env_json else {}
    if harness_argv is not None and not (
        isinstance(harness_argv, list)
        and harness_argv
        and all(isinstance(a, str) for a in harness_argv)
    ):
        raise SystemExit("--harness-argv-json must be a non-empty JSON array of strings")
    external = harness_argv is not None
    if not external and not CODEX_BIN.is_file():
        raise SystemExit(f"codex binary not found: {CODEX_BIN} (ADAPT_LABCX_CODEX_BIN)")

    label = args.run_id.replace("/", "__")
    run_dir = run_dir_for(args.run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    tmp_root, ws, codex_home = scratch_paths(label)

    def _die(signum, _frame):
        raise SystemExit(128 + signum)
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, _die)

    docs_src = C.documents_dir(args.task)
    started = datetime.now(timezone.utc).isoformat()
    t0 = time.time()
    try:
        for d in (tmp_root, codex_home):
            shutil.rmtree(d, ignore_errors=True)
        (ws / "output").mkdir(parents=True)
        if docs_src.is_dir():
            shutil.copytree(docs_src, ws / "documents")
            for p in (ws / "documents").rglob("*"):
                if p.is_file():
                    p.chmod(0o444)
        else:
            (ws / "documents").mkdir()
        harness_dir = Path(args.harness_dir).resolve() if args.harness_dir else None
        installed = install_harness(ws, harness_dir)

        if external:
            config = {
                "runner": harness_id,
                "harness_argv": harness_argv,
                "model": args.model,
                "task": args.task,
                "run_id": args.run_id,
                "timeout": args.timeout,
                "harness_dir": str(harness_dir) if harness_dir else None,
                "harness_files": installed,
                "doc_parser": args.doc_parser,
                "workspace": str(ws),
                "started_at": started,
            }
            (run_dir / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")

            env = os.environ.copy()
            env.pop("XDG_DATA_HOME", None)   # read_doc.py sandbox mode needs podman's store
            env.update(harness_env)
            env.update({
                "LAB_DOC_PARSER": args.doc_parser,
                "LAB_BENCH_ROOT": str(REPO),
            })
            if args.model_flag:
                env["HARNESS2_MODEL"] = args.model
            built_prompt = build_prompt(args.task, ws)
            cmd = [
                "timeout", "--kill-after=30s", str(int(max(60, args.timeout))),
                *(built_prompt if a == PROMPT_SENTINEL else a for a in harness_argv),
            ]
            session_path = run_dir / f"{harness_id}_session.jsonl"
            stderr_path = run_dir / f"{harness_id}_stderr.log"
        else:
            codex_home.mkdir(parents=True)
            (codex_home / "config.toml").write_text(
                CONFIG_TOML_TEMPLATE.format(model=args.model, base_url=BASE_URL, workspace=ws),
                encoding="utf-8")

            config = {
                "runner": "codex",
                "codex_bin": str(CODEX_BIN),
                "codex_version": "codex-cli 0.144.6",
                "model": args.model,
                "model_chain": f"codex -> {BASE_URL} (Responses API)",
                "reasoning_effort": args.reasoning_effort,
                "task": args.task,
                "run_id": args.run_id,
                "timeout": args.timeout,
                "harness_dir": str(harness_dir) if harness_dir else None,
                "harness_files": installed,
                "doc_parser": args.doc_parser,
                "workspace": str(ws),
                "codex_home": str(codex_home),
                "started_at": started,
            }
            (run_dir / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")

            env = os.environ.copy()
            env.pop("OPENAI_BASE_URL", None)     # would flip codex auth validation; unused anyway
            env.pop("XDG_DATA_HOME", None)       # read_doc.py sandbox mode needs podman's store
            env.update({
                "CODEX_HOME": str(codex_home),
                "VERTEX_PROXY_KEY": VERTEX_PROXY_KEY,
                "LAB_DOC_PARSER": args.doc_parser,
                "LAB_BENCH_ROOT": str(REPO),
            })
            cmd = [
                "timeout", "--kill-after=30s", str(int(max(60, args.timeout))),
                str(CODEX_BIN), "exec", build_prompt(args.task, ws),
                "--model", args.model,
                "-c", f'model_reasoning_effort="{args.reasoning_effort}"',
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
                # without this flag .codex/hooks.json is silently skipped
                "--dangerously-bypass-hook-trust",
                "--ephemeral",
                "--json",
                "-C", str(ws),
                "--add-dir", str(ws),
            ]
            session_path = run_dir / "codex_session.jsonl"
            stderr_path = run_dir / "codex_stderr.log"
        with open(session_path, "w", encoding="utf-8") as out, \
             open(stderr_path, "w", encoding="utf-8") as err:
            rc = subprocess.run(cmd, cwd=str(ws), env=env, stdin=subprocess.DEVNULL,
                                stdout=out, stderr=err).returncode
        wall = time.time() - t0

        graded = run_dir / "output"
        graded.mkdir(parents=True, exist_ok=True)
        for src in (ws / "output").rglob("*"):
            if src.is_file():
                dest = graded / src.relative_to(ws / "output")
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
        delivered = sorted(str(p.relative_to(graded)) for p in graded.rglob("*") if p.is_file())

        summary = parse_session(session_path)
        session_text = session_path.read_text(encoding="utf-8", errors="replace")
        # codex --json always emits parseable events, so an empty stream there means the
        # chain never started and must NOT be cached. An external harness prints its own
        # shape (claude -p prints prose unless it is asked for JSON), so empty metrics is
        # expected there and the rollout is graded on what it actually delivered.
        if summary["n_events"] == 0 and not external:
            print(f"[run_codex_lab] NO parseable events in {session_path}; "
                  f"NOT writing metrics.json (rc={rc})", file=sys.stderr)
            return 2
        if rc == 0 and not delivered:
            rc = 1   # exit 0 with an empty output/ is a failure, not a success
        docs_read, docs_total = doc_coverage(summary, ws / "documents", session_text)
        scratch_left = sorted(str(p.relative_to(ws)) for p in ws.rglob("*")
                              if p.is_file() and not str(p.relative_to(ws)).startswith(
                                  ("documents/", "output/")))[:200]
        metrics = {
            "runner": harness_id,
            "model": args.model,
            "task": args.task,
            "run_id": args.run_id,
            "turn_count": summary["turns"],
            "tool_calls": summary["tool_calls"],
            "stream_errors": summary["errors"],
            "input_tokens": summary["input_tokens"],
            "cached_input_tokens": summary["cached_input_tokens"],
            "output_tokens": summary["output_tokens"],
            "reasoning_tokens": summary["reasoning_output_tokens"],
            "total_tokens": summary["input_tokens"] + summary["output_tokens"],
            "wall_clock_seconds": round(wall, 1),
            "finished_cleanly": rc == 0,
            "exit_code": rc,
            "deliverables": delivered,
            "documents_read": docs_read,
            "total_documents": docs_total,
            "harness_dir": str(harness_dir) if harness_dir else None,
            "harness_files": installed,
            "workspace_scratch_files": scratch_left,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        if not external:
            metrics["input_tokens_note"] = "As reported by the configured Codex endpoint."
        elif summary.get("usage_present") is False or summary["n_events"] == 0:
            # Say so on the record rather than let zeroes read as real figures. This
            # covers a harness that emits no parseable event at all (n_events == 0, the
            # prose case) AND a parseable stream that stopped before its terminal event
            # (a rollout killed mid-turn) -- exactly when reporting 0 tokens is a lie.
            # An external harness emitting the CODEX shape reports usage the codex way,
            # so it has no `usage_present` key and is correctly left unannotated.
            metrics["metrics_note"] = (
                f"no usage figures in {session_path.name}: either {harness_id} does not "
                f"emit a readable event shape, or the session ended before its terminal "
                f"event, so turn/token counts are absent (deliverables are unaffected)"
            )
        (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
        print(f"[run_codex_lab] {args.run_id}: runner={harness_id} rc={rc} "
              f"turns={summary['turns']} tools={summary['tool_calls']} wall={wall:.0f}s "
              f"deliverables={', '.join(delivered) or 'NONE'}")
        return rc
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
        shutil.rmtree(codex_home, ignore_errors=True)


parser = argparse.ArgumentParser(description="Run one LAB task through Codex CLI")
parser.add_argument("--task", default=None, help="Task ID (e.g. corporate-ma/<slug>)")
parser.add_argument("--run-id", default=None, help="Run identifier (namespaced, required)")
parser.add_argument("--model", default=DEFAULT_MODEL,
                    help="Proxy model id resolved by CODEX_HOME config.toml "
                         "(default: %(default)s)")
parser.add_argument("--reasoning-effort", default="high",
                    help="codex model_reasoning_effort (default: %(default)s)")
parser.add_argument("--timeout", type=int, default=int(os.environ.get(
                    "ADAPT_LABCX_TIMEOUT", "3600")),
                    help="Wall-clock budget in seconds (default: %(default)s)")
parser.add_argument("--harness-dir", default=None,
                    help="Rendered codex_root harness tree to install at the workspace root "
                         "(AGENTS.md, .agents/, scripts/, .codex/). Omit for a bare workspace.")
parser.add_argument("--harness-id", default=None,
                    help="Registered harness id to record in metrics.json (default: codex). "
                         "Set this together with --harness-argv-json to run an external harness.")
parser.add_argument("--harness-argv-json", default=None,
                    help="JSON array of strings: the exact argv to run, built from the harness "
                         "manifest by the adapter (harness2.substrate.ExternalCliSubstrate.argv). "
                         "Omit for the codex cell.")
parser.add_argument("--harness-env-json", default=None,
                    help="JSON object of extra environment variables for the harness "
                         "(ExternalCliSubstrate.exec_env); merged over os.environ.")
parser.add_argument("--model-flag", default=None,
                    help="The manifest's model_flag; when set, HARNESS2_MODEL is exported so a "
                         "harness that reads the model from the environment can pick it up.")
parser.add_argument("--doc-parser", choices=["sandbox", "host"],
                    default=os.environ.get("LAB_DOC_PARSER", "sandbox"),
                    help="Where scripts/read_doc.py parses binary documents "
                         "(default: %(default)s)")
parser.add_argument("--reap-hours", type=float, default=None,
                    help="Instead of running a task: remove per-rollout scratch dirs under "
                         "$ADAPT_LABCX_SCRATCH/{lab,codex_home} older than this many hours")

if __name__ == "__main__":
    sys.exit(main(parser.parse_args()))
