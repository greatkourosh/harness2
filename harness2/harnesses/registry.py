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

"""Harness manifests: one small TOML file per framework, read from outside.

A manifest is the *only* place a harness is described, so adding a sixth
harness means adding a TOML file, not editing Python. It carries where the
harness keeps its config, how to run it headlessly, which gateway key it uses,
and which model groups it may see.
"""

from __future__ import annotations

import fnmatch
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

MANIFEST_DIR = Path(__file__).resolve().parent / "manifests"


@dataclass(frozen=True)
class HarnessSpec:
    """One harness, as declared by its manifest."""

    id: str
    label: str
    config_path: Path
    config_format: str
    key_name: str
    allowed_models: tuple[str, ...] = ()
    binary: str = ""
    headless: tuple[str, ...] = ()
    model_flag: str = "--model"
    trace_glob: str = ""
    skills_dir: Path | None = None
    can_run: bool = True
    env: dict[str, str] = field(default_factory=dict)
    adapter: str = ""

    def installed(self) -> bool:
        """Whether this harness is actually present on this machine."""
        return self.config_path.expanduser().exists()

    def trace_dir(self) -> Path | None:
        """The deepest existing directory in `trace_glob`.

        The glob is a pattern (`~/.pi/agent/sessions/**/*.jsonl`), so the
        literal path does not exist; the walk stops at the first segment that
        carries a wildcard and returns what is above it.
        """
        if not self.trace_glob:
            return None
        parts = Path(self.trace_glob).expanduser().parts
        static: list[str] = []
        for part in parts:
            if any(ch in part for ch in "*?["):
                break
            static.append(part)
        candidate = Path(*static) if static else None
        while candidate is not None and not candidate.is_dir():
            if candidate == candidate.parent:
                return None
            candidate = candidate.parent
        return candidate


def _paths(raw: dict, key: str) -> tuple[str, ...]:
    value = raw.get(key, [])
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value)


def _from_dict(raw: dict, *, source: Path) -> HarnessSpec:
    for required in ("id", "label", "config_path", "config_format", "key_name"):
        if not raw.get(required):
            raise ValueError(f"{source.name}: manifest is missing {required!r}")
    skills = raw.get("skills_dir")
    return HarnessSpec(
        id=str(raw["id"]),
        label=str(raw["label"]),
        config_path=Path(str(raw["config_path"])).expanduser(),
        config_format=str(raw["config_format"]),
        key_name=str(raw["key_name"]),
        allowed_models=_paths(raw, "allowed_models"),
        binary=str(raw.get("binary", "")),
        headless=_paths(raw, "headless"),
        model_flag=str(raw.get("model_flag", "--model")),
        trace_glob=str(raw.get("trace_glob", "")),
        skills_dir=Path(str(skills)).expanduser() if skills else None,
        can_run=bool(raw.get("can_run", True)),
        env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
        adapter=str(raw.get("adapter", raw["config_format"])),
    )


def load_all(manifest_dir: Path | None = None) -> list[HarnessSpec]:
    """Every manifest, in file-name order, regardless of installation."""
    directory = manifest_dir or MANIFEST_DIR
    specs = []
    for path in sorted(directory.glob("*.toml")):
        with path.open("rb") as handle:
            specs.append(_from_dict(tomllib.load(handle), source=path))
    return specs


def load_one(harness_id: str, manifest_dir: Path | None = None) -> HarnessSpec:
    for spec in load_all(manifest_dir):
        if spec.id == harness_id:
            return spec
    known = ", ".join(s.id for s in load_all(manifest_dir))
    raise KeyError(f"no harness {harness_id!r}; known: {known}")


def match_models(spec: HarnessSpec, model_ids: list[str]) -> list[str]:
    """The gateway model ids this harness may use, filtered by its glob groups.

    An empty `allowed_models` means the manifest declares no restriction, so
    every id is returned. Patterns are shell globs (`qwen*`, `claude-*`), which
    is how a manifest expresses a *group* of models.
    """
    if not spec.allowed_models:
        return list(model_ids)
    return [
        mid
        for mid in model_ids
        if any(fnmatch.fnmatch(mid, pat) for pat in spec.allowed_models)
    ]
