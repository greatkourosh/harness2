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

"""Benchmark dependency and runtime readiness checks."""

from __future__ import annotations

import json
import shutil
import subprocess

from . import config, ninerouter
from .install import PATCHES, missing_runners
from .substrate.opencode import OpencodeSubstrate


def check(adapter, *, judge=False) -> list[str]:
    missing = missing_runners()
    if missing:
        return [missing]
    errors = []

    def probe(label, cmd, *, cwd=None, remedy="run harness2-setup for this benchmark"):
        try:
            r = subprocess.run(
                [str(c) for c in cmd], cwd=cwd, capture_output=True, text=True, timeout=45
            )
            if r.returncode:
                errors.append(f"{label} failed; {remedy}")
            return r.returncode == 0
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"{label}: {type(exc).__name__}; see README.md")
            return False

    for validate in (
        lambda: config.validate_models(judge=judge),
        adapter.tasks,
        OpencodeSubstrate().preflight,
        adapter.preflight,
    ):
        try:
            validate()
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(str(exc))

    bench = adapter.spec.name.split("-", 1)[0]
    name = {"jb": "job-bench-eval", "lab": "harvey-labs", "wb": "workbuddy-bench"}[bench]
    repo = config.DEPENDENCIES / name
    for dependency in (name, "opencode"):
        target = config.DEPENDENCIES / dependency
        if (target / ".git").exists():
            try:
                head = subprocess.check_output(
                    ["git", "-C", str(target), "rev-parse", "HEAD"], text=True
                ).strip()
                if head != config.DEPENDENCY_PINS[dependency]:
                    errors.append(f"{dependency}: checkout differs from the supported pin")
            except subprocess.CalledProcessError:
                errors.append(f"{dependency}: cannot read checkout revision")
        patch = config.THIRD_PARTY / "patches" / PATCHES[dependency]
        probe(
            f"{dependency} patch",
            ["git", "-C", target, "apply", "--reverse", "--check", patch],
        )
    if not (config.OPENCODE_DIR / "node_modules").is_dir():
        errors.append(f"OpenCode dependencies missing; run harness2-setup {bench}")

    if ninerouter.enabled():
        problem = ninerouter.check_reachable()
        if problem:
            errors.append(problem)
        provider = ninerouter.config_path(ninerouter.config_home())
        if not provider.is_file():
            errors.append(
                f"9router provider block missing at {provider}; run harness2-setup {bench}"
            )

    # ADC is a Vertex requirement, so it is checked only when some role actually
    # routes through Vertex. The `bench == "wb"` clause used to force it for every
    # WorkBuddy cell, which demanded Google credentials from a 9router-only setup
    # that never contacts Vertex -- a first-run blocker for that whole config.
    vertex_roles = (config.SOLVER_MODEL_OC, config.IMPROVER_MODEL, config.JUDGE_MODEL)
    if any(m.startswith("google-vertex") for m in vertex_roles):
        probe(
            "Google Application Default Credentials",
            ["gcloud", "auth", "application-default", "print-access-token"],
        )
    if bench == "lab":
        for binary in ("pandoc", "pdftoppm"):
            if not shutil.which(binary):
                errors.append(f"{binary} missing; install pandoc and poppler-utils (README.md)")
        probe(
            "LAB document and judge dependencies",
            [config.LAB_PYTHON, "-c", "import evaluation.scoring; import google.auth, anthropic"],
            cwd=repo,
        )
    elif bench == "jb" and judge:
        probe(
            "JobBench document and judge dependencies",
            [
                config.JB_JUDGE_PYTHON,
                "-c",
                "import pandas, openpyxl, docx, mammoth, pdfplumber, pptx, openai",
            ],
        )
        if not config.JB_JUDGE_API_BASE:
            errors.append(
                "Set GOOGLE_CLOUD_PROJECT or HARNESS2_JUDGE_BASE_URL for JobBench judging"
            )
        if (
            config.JUDGE_MODEL
            and not config.JB_JUDGE_API_KEY
            and not config.JUDGE_MODEL.startswith("gemini")
        ):
            errors.append(
                "A non-Gemini JobBench judge requires HARNESS2_JUDGE_BASE_URL and HARNESS2_JUDGE_API_KEY"
            )
    elif bench == "wb":
        probe("Docker daemon", ["docker", "info", "--format", "{{.ServerVersion}}"])
        if adapter.spec.name == "wb-office":
            probe(
                "WorkBuddy document rendering",
                [config.WB_RENDER_PY, "-c", "import openpyxl, pptx"],
                remedy="install 'harness2[render]' in the main environment",
            )
        probe(
            "WorkBuddy runtime",
            [
                repo / ".venv/bin/python",
                "-c",
                "import harbor; from workbuddy_bench.runner.harness_adapters import HARNESS_ADAPTERS; "
                "assert all(k in HARNESS_ADAPTERS for k in ('opencode', 'oc-harness', 'codex-harness'))",
            ],
            cwd=repo,
        )
        # The written name depends on the route: a Vertex route is namespaced
        # `google/<model>` while a 9router route carries the gateway's own id
        # verbatim. Accepting only the Vertex form reported a correct 9router
        # setup as stale, which blocks every 9router user before their first run.
        routes_9router = config.SOLVER_MODEL_OC.startswith("9router/")
        for role, expected in (
            ("solver", config.SOLVER_MODEL_OC.removeprefix("google-vertex/")),
            ("judge", config.JUDGE_MODEL),
        ):
            if routes_9router:
                expected = expected.removeprefix("9router/")
                acceptable = {expected}
            else:
                acceptable = {"google/" + expected}
            path = repo / "configs/models/harness2" / f"{role}.yaml"
            try:
                if json.loads(path.read_text())["model"]["name"] not in acceptable:
                    errors.append(f"WorkBuddy {role} configuration is stale; rerun setup")
            except (OSError, ValueError, KeyError, TypeError):
                errors.append(f"WorkBuddy {role} configuration missing; rerun setup")
    return errors


def require_ready(adapter, *, judge=False):
    """Raise before a caller spends anything when this benchmark is not ready to run.

    `judge=True` also demands what grading will need: `HARNESS2_JUDGE_MODEL` set, and,
    where a benchmark grades in its own environment, that environment installed and
    pointed at an endpoint. Those are offline presence and import checks — they catch an
    unset or unbuilt judge, not an account that cannot serve the judge model, which is
    still first learned when the judge actually calls it.
    """
    errors = check(adapter, judge=judge)
    if errors:
        raise config.ConfigError(
            "Setup incomplete; nothing has run yet:\n  - " + "\n  - ".join(errors)
        )
