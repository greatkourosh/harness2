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

"""Inspect and drive the harnesses registered on this machine.

    harness2-harness list                 every manifest, installed or not
    harness2-harness show <id>            one harness's config and model group
    harness2-harness models <id>          the gateway models it may see
    harness2-harness apply <id>           write its model group into its config
    harness2-harness keys                 the gateway keys, per harness
    harness2-harness sync-keys            register a gateway key per harness
    harness2-harness run <id> <prompt>    one headless run through the gateway
    harness2-harness bench <id> <prompt>  one task, writing a substrate run directory
    harness2-harness route <task>         which harness a task would go to, and why
    harness2-harness do <task>            pick a harness, run it, fall back if it fails
    harness2-harness trace <id>           where that harness's own log lives

`apply` and `sync-keys` change files on disk; a config write keeps a timestamped
backup beside the original.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from harness2 import config, ninerouter
from harness2.harnesses import adapters, registry


def _specs() -> list[registry.HarnessSpec]:
    return registry.load_all()


def _pick(harness_id: str) -> registry.HarnessSpec:
    try:
        return registry.load_one(harness_id)
    except KeyError as exc:
        raise SystemExit(f"{exc}") from None


def _resolve_model(spec: registry.HarnessSpec, override: str) -> str:
    """The concrete model id to run `spec` with.

    `allowed_models` holds pattern *groups* (`qwen*`, `claude-*`), so the first
    entry is a pattern and not a usable id. A manifest may name one concrete
    `default_model` to avoid asking the gateway; otherwise the group is resolved
    against the live catalog and the first real id wins.
    """
    if override:
        return override
    if spec.default_model:
        return spec.default_model
    if not spec.allowed_models:
        return ""
    if not ninerouter.enabled():
        # No gateway to ask; a bare pattern would be sent as a model name and 404.
        return spec.allowed_models[0]
    matched = registry.match_models(spec, list(ninerouter.fetch_models()))
    return matched[0] if matched else spec.allowed_models[0]


def cmd_list(args) -> int:
    print(f"{'id':<16} {'installed':<10} {'runs':<6} label")
    for spec in _specs():
        print(
            f"{spec.id:<16} {'yes' if spec.installed() else 'no':<10} "
            f"{'yes' if spec.can_run else 'no':<6} {spec.label}"
        )
    return 0


def cmd_show(args) -> int:
    spec = _pick(args.harness)
    adapter = adapters.get_adapter(spec)
    print(f"{spec.label}  ({spec.id})")
    print(f"  config     {spec.config_path}{'' if spec.installed() else '  [not present]'}")
    print(f"  format     {spec.config_format}  via {type(adapter).__name__}")
    print(f"  key        {spec.key_name}")
    print(f"  headless   {' '.join(spec.headless) or '(none)'}")
    print(f"  allowed    {', '.join(spec.allowed_models) or '(unrestricted)'}")
    if spec.installed():
        current = adapter.configured_models()
        print(f"  configured {', '.join(current) or '(none)'}")
        if not args.quiet:
            print("  --- current ---")
            for key, value in (adapter.current() or {}).items():
                print(f"  {key}: {value}")
    return 0


def cmd_models(args) -> int:
    spec = _pick(args.harness)
    if not ninerouter.enabled():
        print("9router is not configured; set HARNESS2_9ROUTER_* in .env", file=sys.stderr)
        return 1
    available = list(ninerouter.fetch_models())
    allowed = registry.match_models(spec, available)
    print(f"{spec.label}: {len(allowed)} of {len(available)} gateway models")
    for mid in allowed[: args.limit]:
        print(f"  {mid}")
    if len(allowed) > args.limit:
        print(f"  … {len(allowed) - args.limit} more (--limit to see)")
    return 0


def cmd_apply(args) -> int:
    spec = _pick(args.harness)
    adapter = adapters.get_adapter(spec)
    if not spec.installed():
        print(f"{spec.config_path} is not present; is {spec.id} installed?", file=sys.stderr)
        return 1
    allow = None
    if spec.allowed_models and ninerouter.enabled():
        allow = registry.match_models(spec, list(ninerouter.fetch_models()))
        if not allow:
            # A glob group can match nothing; fall back to the raw patterns so
            # the config still names what the manifest asked for.
            allow = list(spec.allowed_models)
    result = adapter.apply(allow=allow)
    if not result.ok:
        print(f"{spec.id}: {result.detail}", file=sys.stderr)
        return 1
    print(f"{spec.id}: {result.detail} -> {result.path}")
    if result.backup:
        print(f"  backup {result.backup}")
    return 0


def cmd_keys(args) -> int:
    for entry in ninerouter.list_keys():
        access = entry.get("access") or {}
        scope = f"restricted {access.get('allow')}" if access.get("restricted") else "unrestricted"
        print(f"  {entry['name']:<22} {scope}")
    return 0


def cmd_sync_keys(args) -> int:
    """Register one gateway key per installed harness, named by its manifest."""
    created = 0
    for spec in _specs():
        if not spec.installed():
            print(f"  {spec.id:<16} skipped (not installed)")
            continue
        allow = list(spec.allowed_models) or None
        ninerouter.ensure_key(spec.key_name, allow=allow)
        print(f"  {spec.id:<16} key {spec.key_name}")
        created += 1
    print(f"{created} harness key(s) present on the gateway")
    return 0


def cmd_run(args) -> int:
    spec = _pick(args.harness)
    adapter = adapters.get_adapter(spec)
    model = args.model or (spec.allowed_models[0] if spec.allowed_models else "")
    result = adapter.run(
        args.prompt, model=model, cwd=Path(args.cwd or ".").resolve(), timeout=args.timeout
    )
    if result.stdout:
        print(result.stdout)
    if not result.ok:
        print(f"{spec.id}: {result.detail}", file=sys.stderr)
        return 1
    return 0


def cmd_bench(args) -> int:
    """Run one task through a harness and write a substrate run directory.

    This is the path a benchmark uses, so the output has the same layout the
    OpenCode and Codex runners produce (workspace/, output/, metrics.json).
    """
    from harness2.benchmarks import external_runner as ER

    spec = _pick(args.harness)
    model = _resolve_model(spec, args.model)
    run_dir = ER.run_one(
        harness=spec.id,
        run_id=args.run_id,
        results_root=Path(args.results_root),
        task_dir=Path(args.task_dir) if args.task_dir else Path("/nonexistent"),
        prompt=args.prompt,
        model=model,
        timeout=args.timeout,
    )
    metrics_path = run_dir / "metrics.json"
    if metrics_path.is_file():
        import json as _json

        metrics = _json.loads(metrics_path.read_text(encoding="utf-8"))
        print(f"{spec.id}: exit {metrics['exit_code']} | delivered {metrics['delivered']}")
    print(f"run dir: {run_dir}")
    return 0


def cmd_route(args) -> int:
    """Show which harness a task would go to, and why. Runs nothing."""
    decision = registry.route(args.task, _specs(), preferred=args.prefer or "")
    print(f"{decision.spec.id}  ({decision.spec.label})")
    print(f"  because  {decision.reason}")
    if decision.matched:
        print(f"  matched  {', '.join(decision.matched)}")
    order = [s.id for s in registry.candidates(_specs()) if s.id != decision.spec.id]
    if order:
        print(f"  fallback {', '.join(order)}")
    return 0


def cmd_do(args) -> int:
    """Give a task; pick a harness, run it, and fall back if it fails.

    The task text decides the harness (see `route`), unless `--harness` names
    one. A candidate that exits non-zero or delivers nothing is not a success,
    so the next candidate is tried — the task is what matters, not which agent
    ran it. Every attempt gets its own run directory.
    """
    from harness2.benchmarks import external_runner as ER

    specs = _specs()
    if args.harness:
        first = registry.route(args.task, specs, preferred=args.harness)
    else:
        first = registry.route(args.task, specs)
    chain = [first.spec] + [
        s for s in registry.candidates(specs) if s.id != first.spec.id
    ]
    if args.no_fallback:
        chain = chain[:1]

    results_root = Path(args.results_root)
    attempts: list[tuple[str, str]] = []
    for spec in chain:
        model = _resolve_model(spec, args.model)
        run_id = f"{args.run_id}/{spec.id}"
        print(f"→ {spec.id}  (model {model or '(profile default)'})", flush=True)
        try:
            run_dir = ER.run_one(
                harness=spec.id,
                run_id=run_id,
                results_root=results_root,
                task_dir=Path(args.task_dir) if args.task_dir else Path("/nonexistent"),
                prompt=args.task,
                model=model,
                timeout=args.timeout,
            )
        except (ValueError, KeyError, OSError) as exc:
            attempts.append((spec.id, f"could not start: {exc}"))
            print(f"  {spec.id}: {exc}", file=sys.stderr)
            continue

        metrics = {}
        meta = run_dir / "metrics.json"
        if meta.is_file():
            metrics = json.loads(meta.read_text(encoding="utf-8"))
        delivered = metrics.get("delivered") or []
        answer = metrics.get("answer") or ""
        # A clean run is a success whether it wrote a file or answered in prose.
        # Requiring a deliverable made `do "what does this file do?"` report
        # "every harness failed" for a run that exited 0 and gave a good answer.
        if metrics.get("exit_code") == 0 and (delivered or answer):
            if delivered:
                print(f"  ok — delivered {delivered}")
            else:
                print("  ok — answered (no file written)")
            if answer:
                print("  answer:")
                for line in answer.splitlines():
                    print(f"    {line}")
            print(f"  run dir: {run_dir}")
            print(f"  routed by: {first.reason}")
            for hid, note in attempts[:-1]:
                print(f"  (tried {hid}: {note})")
            attempts.append((spec.id, "answered" if answer else f"delivered {delivered}"))
            return 0
        why = (
            f"exit {metrics.get('exit_code')}"
            if metrics
            else "produced no metrics"
        )
        attempts.append((spec.id, why))
        print(f"  {spec.id}: {why}", file=sys.stderr)

    print("every harness failed:", file=sys.stderr)
    for hid, note in attempts:
        print(f"  {hid}: {note}", file=sys.stderr)
    return 1



def cmd_trace(args) -> int:
    spec = _pick(args.harness)
    directory = spec.trace_dir()
    print(f"{spec.label}: {directory or '(no trace location configured)'}")
    if directory and directory.exists():
        entries = sorted(directory.rglob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
        files = [p for p in entries if p.is_file()]
        for path in files[: args.limit]:
            print(f"  {path}")
        if len(files) > args.limit:
            print(f"  … {len(files) - args.limit} more")
        if args.tail and files:
            latest = files[0]
            print(f"  --- last session: {latest.name} ---")
            for line in latest.read_text(encoding="utf-8", errors="replace").splitlines()[
                -args.tail :
            ]:
                print(f"  {line[:160]}")

    rows = ninerouter.recent_requests(args.limit)
    print("  --- gateway (all harnesses; 9router logs do not name the caller) ---")
    seen: set[tuple] = set()
    for row in rows:
        key = tuple(sorted(row.items()))
        if key in seen:
            continue
        seen.add(key)
        if row["kind"] == "route":
            where = f" via {row['account']}" if row["account"] else ""
            print(f"  {row['provider']:<52} {row['model']}{where}")
        elif row["kind"] == "attempt":
            print(f"  trying {row['model']}")
        else:
            print(f"  error {row['code']} {row['route']}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="harness2-harness", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="every registered harness").set_defaults(fn=cmd_list)
    sub.add_parser("keys", help="gateway keys, per harness").set_defaults(fn=cmd_keys)
    sub.add_parser("sync-keys", help="ensure a gateway key per harness").set_defaults(
        fn=cmd_sync_keys
    )

    p = sub.add_parser("show", help="one harness's config and model group")
    p.add_argument("harness")
    p.add_argument("--quiet", action="store_true", help="skip the raw config dump")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("models", help="gateway models this harness may see")
    p.add_argument("harness")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(fn=cmd_models)

    p = sub.add_parser("apply", help="write the model group into the harness config")
    p.add_argument("harness")
    p.set_defaults(fn=cmd_apply)

    p = sub.add_parser("run", help="one headless run through the gateway")
    p.add_argument("harness")
    p.add_argument("prompt")
    p.add_argument("--model", default="", help="default: the harness's first allowed model")
    p.add_argument("--cwd", default=".")
    p.add_argument("--timeout", type=int, default=600)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("trace", help="where this harness's own log lives")
    p.add_argument("harness")
    p.add_argument("--limit", type=int, default=5, help="session files to list")
    p.add_argument("--tail", type=int, default=0, help="also print the last N lines of the newest")
    p.set_defaults(fn=cmd_trace)

    p = sub.add_parser(
        "bench",
        help="run one task through a harness, writing a substrate run directory",
    )
    p.add_argument("harness")
    p.add_argument("prompt")
    p.add_argument("--run-id", required=True, help="e.g. demo/pi/base1")
    p.add_argument("--results-root", required=True, help="where the run directory is written")
    p.add_argument("--task-dir", default="", help="copied into the workspace; optional")
    p.add_argument("--model", default="", help="default: the harness's first allowed model")
    p.add_argument("--timeout", type=int, default=3600)
    p.set_defaults(fn=cmd_bench)

    p = sub.add_parser("route", help="which harness a task would go to, and why")
    p.add_argument("task")
    p.add_argument("--prefer", default="", help="force this harness if it is runnable")
    p.set_defaults(fn=cmd_route)

    p = sub.add_parser(
        "do",
        help="give a task: pick a harness, run it, fall back if it fails",
    )
    p.add_argument("task")
    p.add_argument("--harness", default="", help="skip routing and use this harness first")
    p.add_argument("--run-id", default="task", help="run directory name; the harness id is appended")
    p.add_argument("--results-root", default="runs/do", help="where run directories are written")
    p.add_argument("--task-dir", default="", help="copied into the workspace; optional")
    p.add_argument("--model", default="", help="override the model for every attempt")
    p.add_argument("--timeout", type=int, default=3600)
    p.add_argument("--no-fallback", action="store_true", help="try only the chosen harness")
    p.set_defaults(fn=cmd_do)

    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except (config.ConfigError, adapters.ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
