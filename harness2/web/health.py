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

"""Liveness probes behind the panel's "Test all" button.

Two tiers are probed separately because they fail for different reasons: a
local model spends the machine's own GPU, a remote one spends the gateway's
credentials. A harness is healthy when its binary runs AND at least one tier
answers, so a dead remote route does not hide a working local one.

*Ponytail:* this probes the tiers through the gateway and each binary with
``--version``; it does not launch every harness against both models. Ten real
rollouts take minutes, several hang (dsh 403s, claude needs /login), and the
button is meant to answer "is it up" in seconds. Upgrade path: add a per-harness
one-token rollout when a harness can pass ``--version`` yet still fail to run.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from .. import config
from ..harnesses import registry

# The two probe targets. Chosen by running them: the local one is the fastest
# chat model the gateway serves from ollama, and claude-haiku-4-5 is the only
# remote id answering while sonnet/opus time out.
LOCAL_MODEL = "ollama-local/qwen3:8b-ctx32k"
REMOTE_MODEL = "claude-haiku-4-5"
MODEL_TIMEOUT = 45
# qwen3 emits reasoning_content first; a tight cap returns an empty answer and
# reads as a dead route.
MAX_TOKENS = 256
BINARY_TIMEOUT = 20


def _ask(model: str) -> tuple[bool, str]:
    """One gateway completion. Returns (ok, why-not)."""
    base = (config.NINEROUTER_BASE_URL or "").rstrip("/")
    if not base:
        return False, "no HARNESS2_9ROUTER_BASE_URL"
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
            "max_tokens": MAX_TOKENS,
            # The gateway streams even when this is absent; asking for it keeps
            # the read chunked so a hung provider fails on our timeout, not TCP's.
            "stream": True,
        }
    ).encode()
    req = urllib.request.Request(
        base + "/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.NINEROUTER_API_KEY}",
        },
    )
    try:
        raw = urllib.request.urlopen(req, timeout=MODEL_TIMEOUT).read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:160].decode("utf-8", "replace")
        return False, f"HTTP {exc.code}: {detail}"
    except Exception as exc:  # noqa: BLE001 - any transport failure is the verdict
        return False, f"{type(exc).__name__}: {exc}"
    if not raw.strip():
        return False, "empty response"
    return True, ""


def _binary_ok(spec: registry.HarnessSpec) -> tuple[bool, str]:
    """Whether the harness's CLI actually executes."""
    if not spec.binary:
        return False, "no binary declared"
    try:
        done = subprocess.run(
            [spec.binary, "--version"],
            capture_output=True,
            timeout=BINARY_TIMEOUT,
        )
    except FileNotFoundError:
        return False, f"{spec.binary} not on PATH"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}"
    if done.returncode != 0:
        err = (done.stderr or done.stdout)[:120].decode("utf-8", "replace").strip()
        return False, f"exit {done.returncode}" + (f": {err}" if err else "")
    return True, ""


def probe(specs: list[registry.HarnessSpec] | None = None) -> dict:
    """Probe both tiers and every harness. Returns the payload the panel stores."""
    specs = specs if specs is not None else registry.load_all()
    local_ok, local_why = _ask(LOCAL_MODEL)
    remote_ok, remote_why = _ask(REMOTE_MODEL)
    tiers = {
        "local": {"model": LOCAL_MODEL, "ok": local_ok, "error": local_why},
        "remote": {"model": REMOTE_MODEL, "ok": remote_ok, "error": remote_why},
    }
    any_tier = local_ok or remote_ok
    rows = {}
    for spec in specs:
        installed = spec.installed()
        runs, why = _binary_ok(spec) if installed else (False, "not installed")
        healthy = installed and runs and any_tier and spec.can_run
        rows[spec.id] = {
            "healthy": healthy,
            "installed": installed,
            "binary": runs,
            "error": "" if healthy else (why or "no model tier answers"),
            # Which tiers this harness can actually reach right now.
            "local": local_ok,
            "remote": remote_ok,
        }
    return {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "tiers": tiers, "harnesses": rows}


def load(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save(path: Path, payload: dict) -> None:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(path).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        # Status is a convenience; a full disk must not break the panel.
        pass