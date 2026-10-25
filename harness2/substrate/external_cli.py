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

"""ExternalCliSubstrate: a registered harness driven as a solver substrate.

Where OpencodeSubstrate and CodexSubstrate each wrap one known framework, this
one is driven by a *manifest*: the argv comes from the harness's own
`headless` template, its binary from the manifest, and its environment from
`spec.env`. Adding a harness to the benchmark matrix is a TOML file plus a
launcher body the adapter supplies, never a new substrate class.

The editable tree is the codex surface: `AGENTS.md` plus `.agents/skills/`,
which is the convention pi, codex and claude code all read. A harness whose
model group is expressed as OpenCode routing combos still runs through the same
surface -- the surface is what the *agent* sees, not how the model is reached.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Mapping

from .. import config, ninerouter
from ..core import runmark as RM
from ..core import tree as HT
from ..core.tree import SurfaceSpec
from ..harnesses import adapters as HAD
from ..harnesses import registry as HREG
from .base import Install, Launcher, RunRequest
from .codex import CODEX_SURFACE, render_events

# The codex surface is the shared "AGENTS.md + .agents/skills" convention, which
# every external harness here reads. Reusing it means one surface to validate
# instead of one per harness.
EXTERNAL_SURFACE: SurfaceSpec = CODEX_SURFACE

EXTERNAL_ROOT_ENTRIES = ("AGENTS.md", ".agents", "scripts")

class ExternalCliSubstrate:
    """One registered harness behind the Substrate interface.

    Constructed with the harness id; everything else comes from its manifest.
    """

    def __init__(self, harness_id: str, launcher: Launcher | None = None):
        self.harness_id = harness_id
        self._launcher = launcher
        try:
            self.spec = HREG.load_one(harness_id)
        except KeyError as exc:
            raise ValueError(str(exc)) from None

    @property
    def name(self) -> str:
        """The substrate token: the harness id, so `--substrate pi` selects it."""
        return self.harness_id

    def surface(self) -> SurfaceSpec:
        return EXTERNAL_SURFACE

    def materialise(self, tree_dir: Path, spec, ctx: Mapping[str, str]) -> Install:
        """Stage the AGENTS.md-rooted tree for a copy to the workspace root."""
        tree_dir = Path(tree_dir)
        if not HT.is_tree(EXTERNAL_SURFACE, tree_dir):
            raise ValueError(
                f"{tree_dir} is not an external-harness tree (no AGENTS.md); "
                f"{self.harness_id} reads AGENTS.md and .agents/skills/"
            )
        staged = Path(tempfile.mkdtemp(prefix=f"adapt-{self.harness_id}-root-"))
        for entry in EXTERNAL_ROOT_ENTRIES:
            src = tree_dir / entry
            if not src.exists():
                continue
            if src.is_dir():
                shutil.copytree(src, staged / entry)
            else:
                shutil.copy2(src, staged / entry)
        return Install(
            staging_dir=staged,
            fingerprint=RM.fingerprint(tree_dir),
            extras={
                "install_mode": spec.install_mode,
                "harness": self.harness_id,
                **{k: v for k, v in ctx.items() if not k.startswith("file:")},
            },
        )

    def argv(self, prompt: str, workspace: Path, model: str) -> list[str]:
        """The reference headless argv from the manifest's template.

        `{prompt}` is substituted, and `{model}` too when the manifest's headless
        list carries it; otherwise the model is appended after `model_flag`.
        """
        if not self.spec.can_run:
            raise ValueError(f"{self.spec.label} has no non-interactive mode")
        argv = [self.spec.binary or self.harness_id]
        placed_model = False
        for part in self.spec.headless:
            if part == "{prompt}":
                argv.append(prompt)
            elif part == "{model}":
                argv.append(model)
                placed_model = True
            else:
                argv.append(part)
        if model and not placed_model and self.spec.model_flag:
            argv += [self.spec.model_flag, model]
        return argv

    def exec_env(self, base_env: Mapping[str, str]) -> dict[str, str]:
        """The manifest's env plus the gateway credential a harness may need.

        Each harness reaches the gateway through its own config (see
        `harness2.harnesses.adapters`), so this only supplies the variables a
        harness resolves at runtime rather than from a file -- the deepseek
        harness, for instance, reads `NINEROUTER_API_KEY` as a CredentialRef.
        """
        env = dict(base_env)
        adapter = self.adapter()
        # The harness's own adapter knows which ambient variables its config
        # must win over; a Claude Code session's exported model aliases are the
        # worked example.
        for leak in adapter.run_env_drop():
            env.pop(leak, None)
        for key, value in self.spec.env.items():
            env[key] = str(Path(value).expanduser()) if value.startswith(("~", "/")) else value
        # Applied AFTER the drops: a value the adapter supplies is meant to win
        # over the ambient environment it just cleared.
        env.update(adapter.run_env_set())
        if config.NINEROUTER_API_KEY:
            env.setdefault("NINEROUTER_API_KEY", config.NINEROUTER_API_KEY)
        return env

    def adapter(self) -> HAD.Adapter:
        """The harness adapter, for config reads and writes on this harness."""
        return HAD.get_adapter(self.spec)

    def execute(
        self, task_ref: str, install: Install, run_ids: list[str], timeout: int
    ) -> list[Path]:
        if self._launcher is None:
            raise RuntimeError(
                f"ExternalCliSubstrate({self.harness_id!r}) was constructed without a "
                f"launcher body; the benchmark adapter owns runner choice, env wiring "
                f"and read-back locations"
            )
        return self._launcher(
            RunRequest(task_ref=task_ref, run_ids=tuple(run_ids), timeout=int(timeout)), install
        )

    def read_trajectory(
        self, stream_path: Path, *, obs_trunc: int, max_chars: int, args_trunc: int = 2000
    ) -> str:
        """Render the captured stream with the shared renderer.

        The codex renderer is schema-tolerant (agent_message, function_call, and
        message payloads), which is the subset the JSONL harnesses here emit.
        """
        return render_events(
            Path(stream_path), obs_trunc=obs_trunc, max_chars=max_chars, args_trunc=args_trunc
        )

    def preflight(self) -> None:
        """Check the manifest names an installed, runnable harness on the gateway."""
        if not self.spec.installed():
            raise RuntimeError(
                f"{self.harness_id} is not installed: no config at {self.spec.config_path}"
            )
        if not self.spec.can_run:
            raise RuntimeError(
                f"{self.harness_id} has no non-interactive mode; it cannot be a solver "
                f"substrate (see its manifest's `can_run`)"
            )
        if not self.spec.binary or shutil.which(self.spec.binary) is None:
            raise RuntimeError(
                f"{self.harness_id}: {self.spec.binary or 'binary'} is not on PATH; "
                f"install it or set `binary` in its manifest to an absolute path"
            )
        problem = ninerouter.check_reachable()
        if problem:
            raise RuntimeError(problem)


def substrate_for(harness_id: str, launcher: Launcher | None = None) -> ExternalCliSubstrate:
    """The one place a harness id becomes a substrate."""
    return ExternalCliSubstrate(harness_id, launcher=launcher)


EXTERNAL_HARNESS_IDS = tuple(
    s.id for s in HREG.load_all() if s.can_run
)

__all__ = [
    "EXTERNAL_HARNESS_IDS",
    "EXTERNAL_ROOT_ENTRIES",
    "EXTERNAL_SURFACE",
    "ExternalCliSubstrate",
    "substrate_for",
]
