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
import re
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
    # The flag that resumes this harness's previous conversation in the same
    # working directory, or "" when it has none. A panel task that continues an
    # earlier one is only possible where this is set.
    continue_flag: str = ""
    trace_glob: str = ""
    skills_dir: Path | None = None
    can_run: bool = True
    env: dict[str, str] = field(default_factory=dict)
    adapter: str = ""
    # Routing: which task words this harness is good for, and how it ranks.
    # `routes` is matched case-insensitively against the task text; a harness
    # with no match still runs when a task matches nothing (see `route`).
    routes: tuple[str, ...] = ()
    priority: int = 0
    default: bool = False
    default_model: str = ""

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
        continue_flag=str(raw.get("continue_flag", "")),
        trace_glob=str(raw.get("trace_glob", "")),
        skills_dir=Path(str(skills)).expanduser() if skills else None,
        can_run=bool(raw.get("can_run", True)),
        env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
        adapter=str(raw.get("adapter", raw["config_format"])),
        routes=tuple(str(r).lower() for r in _paths(raw, "routes")),
        priority=int(raw.get("priority", 0)),
        default=bool(raw.get("default", False)),
        default_model=str(raw.get("default_model", "")),
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


@dataclass(frozen=True)
class Route:
    """One harness chosen for a task, and the reason it won."""

    spec: HarnessSpec
    matched: tuple[str, ...]
    reason: str

def _mentions(text: str, word: str) -> bool:
    """Whether `word` appears in `text` as a whole word.

    A substring test would fire `git` inside `digital` and `web` inside `website`;
    the boundary check is what makes a keyword list trustworthy.
    """
    return re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text, re.IGNORECASE) is not None

def candidates(specs: list[HarnessSpec]) -> list[HarnessSpec]:
    """Runnable harnesses, best first: more keyword hits, then higher priority.

    A harness that is not installed or has no headless mode cannot run and is
    left out entirely, so a task is never routed to something that would fail
    for a reason the router could have seen.
    """
    runnable = [s for s in specs if s.installed() and s.can_run]
    return sorted(runnable, key=lambda s: (-s.priority, s.id))

def route(task: str, specs: list[HarnessSpec], *, preferred: str = "") -> Route:
    """Pick the harness for `task`.

    Order: an explicit `preferred` id, then the harness whose `routes` words the
    task mentions most (ties by `priority`, then id). A task matching nothing
    falls back to the harness marked `default`, and failing that to the first
    candidate — routing always answers, it never refuses.
    """
    pool = candidates(specs)
    if not pool:
        raise KeyError("no runnable harness: none is installed with a headless mode")
    if preferred:
        chosen = next((s for s in pool if s.id == preferred), None)
        if chosen is None:
            known = ", ".join(s.id for s in pool)
            raise KeyError(f"harness {preferred!r} is not runnable; runnable: {known}")
        return Route(chosen, (), f"explicitly requested ({preferred})")

    scored: list[tuple[int, HarnessSpec, tuple[str, ...]]] = []
    for spec in pool:
        hits = tuple(w for w in spec.routes if _mentions(task, w))
        if hits:
            scored.append((len(hits), spec, hits))
    if scored:
        best = max(scored, key=lambda row: (row[0], row[1].priority))
        top = [row for row in scored if row[0] == best[0]]
        if len(top) > 1:
            winner = max(top, key=lambda row: (row[1].priority, row[1].id))[1]
            hits = next(row[2] for row in top if row[1] is winner)
            others = ", ".join(sorted(r[1].id for r in top if r[1] is not winner))
            reason = f"matched {', '.join(hits)} (beat {others} on priority)"
        else:
            winner, hits = best[1], best[2]
            reason = f"matched {', '.join(hits)}"
        return Route(winner, tuple(hits), reason)

    fallback = next((s for s in pool if s.default), pool[0])
    why = "declared default" if fallback.default else "first of the runnable harnesses"
    return Route(fallback, (), f"no keyword matched; {why}")
