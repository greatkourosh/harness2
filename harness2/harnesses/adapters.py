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

"""One adapter per harness: it knows that harness's config shape and nothing else.

Every adapter reads and writes a *single* file and never touches another
harness's. That is what keeps the harnesses peers rather than a nesting: there
is no shared state except the gateway they all point at, and no adapter
imports another.

Writes are always merge-with-backup. A harness config is the user's own file
(`~/.pi/agent/models.json`, `~/.claude/settings.json`), often carrying settings
this package knows nothing about; replacing it wholesale would destroy them.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from .registry import HarnessSpec, match_models

# Nine-router is reached under this id inside every harness's own config.
GATEWAY_ID = config.NINEROUTER_PROVIDER_ID


@dataclass
class Result:
    """What an apply or a run produced."""

    ok: bool
    detail: str
    path: Path | None = None
    backup: Path | None = None
    stdout: str = ""


class ConfigError(config.ConfigError):
    """A harness config could not be read or written in the shape its adapter expects."""


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"{path} is not readable JSON: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _backup(path: Path) -> Path | None:
    """Copy `path` aside before a write; None when there was nothing to back up."""
    if not path.is_file():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = path.with_suffix(path.suffix + f".bak-{stamp}")
    shutil.copy2(path, dest)
    return dest


def _write_json(path: Path, data: dict) -> Path | None:
    backup = _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return backup


class Adapter:
    """Base adapter: read/write one config, describe one harness."""

    def __init__(self, spec: HarnessSpec):
        self.spec = spec

    # -- reading ---------------------------------------------------------

    def current(self) -> dict:
        raise NotImplementedError

    def configured_models(self) -> list[str]:
        """Model ids this harness currently lists for the gateway."""
        raw = self.current().get(GATEWAY_ID) or {}
        models = raw.get("models") or []
        out = []
        for entry in models:
            out.append(entry if isinstance(entry, str) else str(entry.get("id", "")))
        return [m for m in out if m]

    # -- writing ---------------------------------------------------------

    def apply(self, *, allow: list[str] | None = None) -> Result:
        raise NotImplementedError

    # -- running ---------------------------------------------------------

    def run(self, prompt: str, *, model: str, cwd: Path, timeout: int = 600) -> Result:
        if not self.spec.can_run:
            return Result(False, f"{self.spec.label} has no non-interactive mode")
        if not self.spec.binary or shutil.which(self.spec.binary) is None:
            return Result(False, f"{self.spec.binary or 'binary'} not on PATH")
        argv = [self.spec.binary]
        for part in self.spec.headless:
            argv.append(prompt if part == "{prompt}" else part)
        if model and self.spec.model_flag:
            argv += [self.spec.model_flag, model]
        env = {**os.environ, **self.spec.env}
        for leak in self.run_env_drop():
            env.pop(leak, None)
        env.update(self.run_env_set())
        try:
            proc = subprocess.run(
                argv, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Result(False, f"{type(exc).__name__}: {exc}")
        out = (proc.stdout or "").strip()
        return Result(proc.returncode == 0, f"exit {proc.returncode}", stdout=out)

    def run_env_set(self) -> dict[str, str]:
        """Env a harness needs to reach the gateway; empty when its config suffices."""
        return {}

    def run_env_drop(self) -> tuple[str, ...]:
        """Ambient vars that would override the harness's own config."""
        return ()

    # -- reporting -------------------------------------------------------

    def describe(self) -> dict:
        return {
            "id": self.spec.id,
            "label": self.spec.label,
            "installed": self.spec.installed(),
            "config": str(self.spec.config_path),
            "format": self.spec.config_format,
            "can_run": self.spec.can_run,
            "configured": self.configured_models() if self.spec.installed() else [],
        }


class PiAdapter(Adapter):
    """pi: {"providers": {"9router": {baseUrl, api, apiKey, models: [{id}]}}}."""

    def current(self) -> dict:
        data = _read_json(self.spec.config_path)
        return {"providers": data.get("providers") or {}}

    def configured_models(self) -> list[str]:
        prov = (self.current()["providers"] or {}).get(GATEWAY_ID) or {}
        return [str(m.get("id")) for m in (prov.get("models") or []) if m.get("id")]

    def apply(self, *, allow: list[str] | None = None) -> Result:
        data = _read_json(self.spec.config_path)
        providers = data.get("providers")
        if not isinstance(providers, dict):
            providers = {}
        existing = providers.get(GATEWAY_ID) or {}
        models = allow or list(existing_models(existing))
        providers[GATEWAY_ID] = {
            **existing,
            "baseUrl": config.NINEROUTER_BASE_URL,
            "api": existing.get("api", "openai-completions"),
            "apiKey": config.NINEROUTER_API_KEY,
            "models": [{"id": mid} for mid in models],
        }
        data["providers"] = providers
        backup = _write_json(self.spec.config_path, data)
        return Result(True, f"{len(models)} models", self.spec.config_path, backup)


def existing_models(provider: dict) -> list[str]:
    return [str(m.get("id")) for m in (provider.get("models") or []) if m.get("id")]


class OpencodeAdapter(Adapter):
    """opencode: {"provider": {"9router": {npm, options:{baseURL,apiKey}, models}}}.

    `provider` is singular on purpose — v1.14.18 rejects `providers`, which is
    the key the newer desktop config uses.
    """

    def current(self) -> dict:
        data = _read_json(self.spec.config_path)
        return {"provider": data.get("provider") or {}}

    def configured_models(self) -> list[str]:
        prov = (self.current()["provider"] or {}).get(GATEWAY_ID) or {}
        return [str(k) for k in (prov.get("models") or {})]

    def apply(self, *, allow: list[str] | None = None) -> Result:
        data = _read_json(self.spec.config_path)
        providers = data.get("provider")
        if not isinstance(providers, dict):
            providers = {}
        existing = providers.get(GATEWAY_ID) or {}
        models = allow if allow is not None else list(existing.get("models") or {})
        providers[GATEWAY_ID] = {
            "npm": existing.get("npm", "@ai-sdk/openai-compatible"),
            "name": existing.get("name", "9router"),
            "options": {
                **(existing.get("options") or {}),
                "baseURL": config.NINEROUTER_BASE_URL,
                "apiKey": config.NINEROUTER_API_KEY,
            },
            "models": {mid: {"name": mid, "tool_call": True} for mid in models},
        }
        data["provider"] = providers
        backup = _write_json(self.spec.config_path, data)
        return Result(True, f"{len(models)} models", self.spec.config_path, backup)


class DeepseekAdapter(Adapter):
    """dsh: a YAML patch array; the gateway entry id is `llm-pi-ai`.

    Credentials here are a CredentialRef (`apiKeyEnv` names an env var), so this
    adapter never writes a secret — it writes the var *name* and the base URL.
    """

    def current(self) -> dict:
        try:
            import yaml  # noqa: PLC0415 - optional, only this adapter needs it
        except ImportError:
            return {"_note": "pyyaml not installed; install it to read the dsh patch"}
        if not self.spec.config_path.is_file():
            return {}
        data = yaml.safe_load(self.spec.config_path.read_text(encoding="utf-8")) or []
        for entry in data if isinstance(data, list) else []:
            if entry.get("id") == "llm-pi-ai":
                return {"llm-pi-ai": entry.get("config") or {}}
        return {}

    def configured_models(self) -> list[str]:
        cfg = self.current().get("llm-pi-ai") or {}
        prov = (cfg.get("providers") or {}).get(GATEWAY_ID) or {}
        return [str(m.get("id")) for m in (prov.get("models") or []) if m.get("id")]

    def apply(self, *, allow: list[str] | None = None) -> Result:
        try:
            import yaml  # noqa: PLC0415
        except ImportError as exc:
            raise ConfigError(
                "the deepseek adapter needs pyyaml: uv pip install pyyaml"
            ) from exc
        data = []
        if self.spec.config_path.is_file():
            data = yaml.safe_load(self.spec.config_path.read_text(encoding="utf-8")) or []
        if not isinstance(data, list):
            raise ConfigError(f"{self.spec.config_path} is not a YAML patch array")
        models = allow if allow is not None else self.configured_models()
        entry = next((e for e in data if e.get("id") == "llm-pi-ai"), None)
        if entry is None:
            entry = {"id": "llm-pi-ai", "name": "@deepseek-ai/dsh-llm-pi-ai", "config": {}}
            data.append(entry)
        cfg = entry.setdefault("config", {})
        providers = cfg.setdefault("providers", {})
        existing = providers.get(GATEWAY_ID) or {}
        providers[GATEWAY_ID] = {
            **existing,
            "apiKeyEnv": "NINEROUTER_API_KEY",
            "api": existing.get("api", "openai-completions"),
            "baseURL": config.NINEROUTER_BASE_URL,
            "models": [{"id": mid} for mid in models],
        }
        backup = _backup(self.spec.config_path)
        self.spec.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.spec.config_path.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        return Result(True, f"{len(models)} models", self.spec.config_path, backup)

    def run_env_set(self) -> dict[str, str]:
        return {"NINEROUTER_API_KEY": config.NINEROUTER_API_KEY}


class ClaudeCodeAdapter(Adapter):
    """claude code: `~/.claude/settings.json`, pointed at the gateway via `env`.

    The CLI reads ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN, and 9router serves
    the Anthropic Messages protocol, so no proxy is needed. Only the `env` block
    is touched; hooks, plugins and marketplaces survive untouched.
    """

    def current(self) -> dict:
        data = _read_json(self.spec.config_path)
        return {"env": data.get("env") or {}}

    def configured_models(self) -> list[str]:
        env = self.current().get("env") or {}
        return [v for k, v in env.items() if k.startswith("ANTHROPIC_DEFAULT_") and v]

    def apply(self, *, allow: list[str] | None = None) -> Result:
        data = _read_json(self.spec.config_path)
        env = data.get("env")
        if not isinstance(env, dict):
            env = {}
        models = allow if allow is not None else self.configured_models()
        env.update(
            {
                "ANTHROPIC_BASE_URL": config.NINEROUTER_BASE_URL.removesuffix("/v1"),
                # A gateway key is a bearer token, not an upstream Anthropic key.
                # Setting ANTHROPIC_API_KEY as well makes the CLI choose between
                # two credentials and hang; the token alone authenticates.
                "ANTHROPIC_AUTH_TOKEN": config.NINEROUTER_API_KEY,
            }
        )
        env.pop("ANTHROPIC_API_KEY", None)
        # The three aliases must keep their meanings: SONNET is the workhorse,
        # OPUS the hardest tasks, HAIKU the cheap background model. Assigning by
        # list position would give them whatever the glob sort produced, so each
        # alias is matched to an id that actually carries its tier name.
        for alias, *needles in (
            ("ANTHROPIC_DEFAULT_SONNET_MODEL", "sonnet"),
            ("ANTHROPIC_DEFAULT_OPUS_MODEL", "opus"),
            ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "haiku"),
        ):
            match = next((m for m in models if any(n in m for n in needles)), None)
            if match:
                env[alias] = match
            else:
                env.pop(alias, None)
        data["env"] = env
        backup = _write_json(self.spec.config_path, data)
        return Result(True, f"{len(models)} models", self.spec.config_path, backup)

    def run_env_set(self) -> dict[str, str]:
        """Environment that keeps the CLI on the gateway."""
        env = {"ANTHROPIC_AUTH_TOKEN": config.NINEROUTER_API_KEY}
        if config.NINEROUTER_BASE_URL:
            env["ANTHROPIC_BASE_URL"] = config.NINEROUTER_BASE_URL.removesuffix("/v1")
        return env

    def run_env_drop(self) -> tuple[str, ...]:
        """Vars the ambient environment may carry that would override the config.

        A Claude Code session exports its own ANTHROPIC_* aliases (the session
        this was written in had `claude-opus-5[1m]`, which the gateway does not
        serve). Inherited, they beat settings.json and the run dies on a model
        that does not exist, so they are removed and the CLI re-reads them.
        ANTHROPIC_API_KEY goes too: two credentials make the CLI hang.
        """
        return (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        )


class ClaudeDesktopAdapter(Adapter):
    """claude desktop: no endpoint of its own, so it is inventory-only.

    Its config holds UI preferences and MCP servers. The model surface it uses
    belongs to the `claude` CLI (see claude-code.toml), so this adapter reports
    rather than writes.
    """

    def current(self) -> dict:
        return _read_json(self.spec.config_path)

    def configured_models(self) -> list[str]:
        return []

    def apply(self, *, allow: list[str] | None = None) -> Result:
        return Result(
            False,
            "claude desktop has no model endpoint of its own; it runs Code sessions "
            "through the claude CLI — configure the claude-code harness instead",
            self.spec.config_path,
        )


ADAPTERS: dict[str, type[Adapter]] = {
    "pi": PiAdapter,
    "opencode": OpencodeAdapter,
    "deepseek": DeepseekAdapter,
    "claude-code": ClaudeCodeAdapter,
    "claude-desktop": ClaudeDesktopAdapter,
}


def get_adapter(spec: HarnessSpec) -> Adapter:
    """The adapter named by the manifest, defaulting to the config format's."""
    key = spec.adapter or spec.config_format
    cls = ADAPTERS.get(key)
    if cls is None:
        cls = ADAPTERS.get(spec.config_format)
    if cls is None:
        raise ConfigError(
            f"no adapter for harness {spec.id!r} (adapter={key!r}); "
            f"known: {', '.join(sorted(ADAPTERS))}"
        )
    return cls(spec)


__all__ = ["ADAPTERS", "Adapter", "ConfigError", "Result", "get_adapter", "match_models"]
