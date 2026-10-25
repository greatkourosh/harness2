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

"""9router as an OpenCode provider.

9router is an OpenAI-compatible gateway that fronts many upstream providers and
exposes them under stable ids. OpenCode reaches it through a provider block in its
global config, because the benchmark runners write a workspace `opencode.json`
of their own and the global one is the only layer a run cannot overwrite.

The provider block is written to `$XDG_CONFIG_HOME/opencode/opencode.json` and
selected with `OPENCODE_CONFIG_DIR`-free global config discovery; the runners
set `XDG_CONFIG_HOME` per run, so the block is written where that run will
look. `OPENCODE_CONFIG` (a single file) is deliberately not used: it merges as
"custom" *after* the global config and, in the pinned OpenCode, a provider
declared there does not resolve its own models.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import config

# The OpenCode model ids this provider exposes. 9router serves a model under
# the id it was given, so a request for `9router/All` must forward "All".
# OpenCode only accepts a model that the provider block declares, so the block
# must list every id a run may name. The curated set below is the fallback
# when the gateway's model list cannot be read.
MODELS: dict[str, dict] = {
    "All": {"name": "All", "tool_call": True, "reasoning": True},
    "Safe": {"name": "Safe", "tool_call": True, "reasoning": True},
    "Coding": {"name": "Coding", "tool_call": True, "reasoning": True},
    "General": {"name": "General", "tool_call": True, "reasoning": True},
    "Fast": {"name": "Fast", "tool_call": True},
    "Reasoning": {"name": "Reasoning", "tool_call": True, "reasoning": True},
    "Vision": {"name": "Vision", "tool_call": True, "reasoning": True},
    "local": {"name": "local", "tool_call": True},
    "claude-sonnet-4-6": {"name": "claude-sonnet-4-6", "tool_call": True, "reasoning": True},
    "claude-opus-4-6": {"name": "claude-opus-4-6", "tool_call": True, "reasoning": True},
    "claude-haiku-4-5": {"name": "claude-haiku-4-5", "tool_call": True},
    "claude-sonnet-4-5": {"name": "claude-sonnet-4-5", "tool_call": True, "reasoning": True},
    "claude-opus-4-5": {"name": "claude-opus-4-5", "tool_call": True, "reasoning": True},
    "claude-opus-4-7": {"name": "claude-opus-4-7", "tool_call": True, "reasoning": True},
}


def _models_cache_path() -> Path:
    return config.DEPENDENCIES / ".9router-models.json"


def _parse_models(payload: dict) -> dict[str, dict]:
    models: dict[str, dict] = {}
    for entry in payload.get("data") or []:
        mid = entry.get("id")
        if not mid:
            continue
        caps = entry.get("capabilities") or {}
        spec: dict = {"name": mid}
        if caps.get("tools", True):
            spec["tool_call"] = True
        if caps.get("reasoning"):
            spec["reasoning"] = True
        context = entry.get("context_length")
        output = entry.get("max_completion_tokens")
        if context and output:
            # OpenCode's `limit` requires both fields.
            spec["limit"] = {"context": int(context), "output": int(output)}
        models[mid] = spec
    return models


def _models_from_admin(timeout: int) -> dict[str, dict]:
    """The catalog via the dashboard endpoint, which is the fast one.

    `/v1/models` (the model API) renders the entire upstream catalog and has
    been observed to take tens of seconds or time out; `/api/models` (the
    dashboard API) answers the same 1600-odd ids in well under a second.
    """
    status, payload = _admin_call("GET", "/api/models", timeout=timeout)
    if status != 200 or not isinstance(payload, dict):
        return {}
    models: dict[str, dict] = {}
    for entry in payload.get("models") or []:
        mid = entry.get("routedModel") or entry.get("id")
        if not mid:
            continue
        caps = entry.get("capabilities") or {}
        spec: dict = {"name": str(mid)}
        if caps.get("tools", True):
            spec["tool_call"] = True
        if caps.get("reasoning"):
            spec["reasoning"] = True
        context = entry.get("context_length") or caps.get("contextWindow")
        output = entry.get("max_completion_tokens") or caps.get("maxOutput")
        if context and output:
            spec["limit"] = {"context": int(context), "output": int(output)}
        models[str(mid)] = spec
    return models


def _models_from_admin(timeout: int) -> dict[str, dict]:
    """The catalog via the dashboard endpoint, which is the fast one.

    Two sources make up what the gateway can serve: the individual models under
    `/api/models` (1690-odd, sub-second), and the routing *combos* under
    `/api/combos` (16 bare ids like `All`, `Safe`, `claude-sonnet-4-6`). A
    combo is what most harnesses here are configured with, and it does NOT
    appear in `/api/models`, so both are merged.

    `/v1/models` (the model API) is deliberately not used: it renders the whole
    catalog and has been observed to take 90s or time out.
    """
    models: dict[str, dict] = {}
    status, payload = _admin_call("GET", "/api/models", timeout=timeout)
    if status == 200 and isinstance(payload, dict):
        for entry in payload.get("models") or []:
            mid = entry.get("routedModel") or entry.get("id")
            if not mid:
                continue
            caps = entry.get("capabilities") or {}
            spec: dict = {"name": str(mid)}
            if caps.get("tools", True):
                spec["tool_call"] = True
            if caps.get("reasoning"):
                spec["reasoning"] = True
            context = entry.get("context_length") or caps.get("contextWindow")
            output = entry.get("max_completion_tokens") or caps.get("maxOutput")
            if context and output:
                spec["limit"] = {"context": int(context), "output": int(output)}
            models[str(mid)] = spec

    status, payload = _admin_call("GET", "/api/combos", timeout=timeout)
    if status == 200:
        combos = payload.get("combos") if isinstance(payload, dict) else payload
        for entry in combos or []:
            if not isinstance(entry, dict):
                continue
            mid = entry.get("name") or entry.get("id")
            if not mid:
                continue
            # A combo routes across models, so it always carries tools and
            # inherits no fixed context window.
            models.setdefault(str(mid), {"name": str(mid), "tool_call": True, "reasoning": True})
    return models


def fetch_models(timeout: int = 20, *, cache_ttl: int = 3600, refresh: bool = False):
    """Every model id the gateway can serve, as OpenCode model entries.

    9router serves a model under the id it was given, so the OpenCode model id
    and the gateway model id are the same string. The set is the union of
    individual models and routing combos; see `_models_from_admin`.

    Source order: the fast dashboard endpoint (needs the admin password), then
    a disk cache younger than `cache_ttl`, then the curated MODELS. The cache
    exists because even the fast endpoint costs a login round-trip, and a
    control-plane command reads the catalog on every `apply`.
    """
    import time

    cache = _models_cache_path()
    if not refresh and cache.is_file():
        try:
            blob = json.loads(cache.read_text(encoding="utf-8"))
            if time.time() - float(blob.get("at", 0)) < cache_ttl and blob.get("models"):
                return {k: dict(v) for k, v in blob["models"].items()}
        except (OSError, ValueError, TypeError):
            pass

    models: dict[str, dict] = {}
    if config.NINEROUTER_ADMIN_PASSWORD:
        models = _models_from_admin(timeout)
    if not models:
        models = _models_from_models_api(timeout)
    if models:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(
                json.dumps({"at": time.time(), "models": models}), encoding="utf-8"
            )
        except OSError:
            pass
        return models
    return dict(MODELS)


def _models_from_models_api(timeout: int) -> dict[str, dict]:
    """Fallback: the model API's own list, when no admin password is set."""
    import urllib.error
    import urllib.request

    base = config.NINEROUTER_BASE_URL.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    req = urllib.request.Request(
        base + "/v1/models", headers={"Authorization": f"Bearer {config.NINEROUTER_API_KEY}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return {}
    out = _parse_models(payload)
    # The curated fallback is a last resort, not a real answer.
    return out if len(out) > len(MODELS) else {}


def enabled() -> bool:
    """Whether 9router is configured as a backend."""
    return bool(config.NINEROUTER_BASE_URL and config.NINEROUTER_API_KEY)


def config_home() -> Path:
    """The XDG config home Harness2 gives OpenCode.

    It is deliberately not the user's real `~/.config`: the pinned OpenCode
    rejects a global config written for a newer schema, and that file would
    abort every run before the first model call.
    """
    return config.DEPENDENCIES / "opencode-config"


def provider_block(models: dict[str, dict] | None = None) -> dict:
    """The `provider` fragment for an OpenCode config."""
    return {
        config.NINEROUTER_PROVIDER_ID: {
            "npm": "@ai-sdk/openai-compatible",
            "name": "9router",
            "options": {
                "baseURL": config.NINEROUTER_BASE_URL,
                "apiKey": config.NINEROUTER_API_KEY,
            },
            "models": {mid: dict(spec) for mid, spec in (models or MODELS).items()},
        }
    }


def config_path(config_home: Path) -> Path:
    return Path(config_home) / "opencode" / "opencode.json"


def write_provider_config(config_home: Path, models: dict[str, dict] | None = None) -> Path:
    """Write the provider block into OpenCode's global config for `config_home`.

    Returns the file path written. The block is merged into any existing
    content rather than replacing it, so a user's own global settings survive.
    """
    path = config_path(config_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    if not isinstance(data, dict):
        data = {}
    providers = data.get("provider")
    if not isinstance(providers, dict):
        providers = {}
    providers.update(provider_block(models))
    data["provider"] = providers
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def env_for_run(base_env: dict[str, str], *, config_home: Path) -> dict[str, str]:
    """Environment for a run that should reach 9router.

    `XDG_CONFIG_HOME` points at the directory holding the provider block, and
    `XDG_DATA_HOME`/`XDG_STATE_HOME` are left to the caller: the benchmark
    runners already isolate them per task.
    """
    env = dict(base_env)
    env["XDG_CONFIG_HOME"] = str(config_home)
    return env


def model_id(model: str) -> str:
    """Strip the `9router/` prefix from a harness2 model id.

    `9router/All` -> `All`; anything else is returned unchanged.
    """
    prefix = config.NINEROUTER_PROVIDER_ID + "/"
    return model[len(prefix) :] if model.startswith(prefix) else model


def is_9router_model(model: str) -> bool:
    return model.startswith(config.NINEROUTER_PROVIDER_ID + "/")


def judge_model_id(model: str) -> str:
    """The id to send to the 9router judge endpoint.

    The judges call a bare model id; 9router serves combos and upstream ids
    under the same namespace the solver uses.
    """
    return model_id(model)


def _admin_call(method: str, path: str, body: dict | None = None, *, timeout: int = 30):
    """Call 9router's own dashboard API with a logged-in session cookie.

    The admin API is not the model API: it answers on the gateway root and is
    authenticated by the dashboard password, not by a model key. Returns
    (status, parsed-json-or-text). Raises ConfigError when no password is set.
    """
    import http.cookiejar
    import urllib.error
    import urllib.request

    if not config.NINEROUTER_ADMIN_PASSWORD:
        raise config.ConfigError(
            "Set HARNESS2_9ROUTER_ADMIN_PASSWORD to manage 9router keys "
            "(the gateway dashboard password)"
        )
    base = config.NINEROUTER_BASE_URL.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    login = urllib.request.Request(
        base + "/api/auth/login",
        data=json.dumps({"password": config.NINEROUTER_ADMIN_PASSWORD}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        opener.open(login, timeout=timeout).read()
    except urllib.error.HTTPError as exc:
        return exc.code, f"login failed: HTTP {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        return None, f"unreachable: {exc}"

    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        base + path,
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method=method,
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw[:400]
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read().decode("utf-8")[:400]
        except OSError:
            return exc.code, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        return None, f"unreachable: {exc}"


def list_keys() -> list[dict]:
    """Every access key the gateway holds, each with its `access` allow-list."""
    status, payload = _admin_call("GET", "/api/keys")
    if status != 200 or not isinstance(payload, dict):
        raise config.ConfigError(f"could not list 9router keys: {status} {payload}")
    return payload.get("keys") or []


def find_key(name: str) -> dict | None:
    """The key named `name`, or None."""
    for entry in list_keys():
        if entry.get("name") == name:
            return entry
    return None


def create_key(name: str, *, allow: list[str] | None = None) -> dict:
    """Create a key named `name`, optionally with a model allow-list.

    The allow-list is recorded for the gateway dashboard, but 9router 0.5.109
    does NOT enforce it on model calls (verified: a key restricted to `All`
    still served `Safe`). Per-harness model isolation therefore lives in each
    harness's own config; this only records intent.
    """
    body: dict = {"name": name}
    if allow is not None:
        body["access"] = {"restricted": True, "allow": list(allow)}
    status, payload = _admin_call("POST", "/api/keys", body)
    if status not in (200, 201) or not isinstance(payload, dict):
        raise config.ConfigError(f"could not create key {name!r}: {status} {payload}")
    return payload


def update_key(key_id: str, *, allow: list[str]) -> dict:
    """Set `key_id`'s allow-list (PUT is the only accepted method; PATCH is 405)."""
    status, payload = _admin_call(
        "PUT", f"/api/keys/{key_id}", {"access": {"restricted": True, "allow": list(allow)}}
    )
    if status != 200:
        raise config.ConfigError(f"could not update key {key_id}: {status} {payload}")
    return payload


def delete_key(key_id: str) -> None:
    status, payload = _admin_call("DELETE", f"/api/keys/{key_id}")
    if status != 200:
        raise config.ConfigError(f"could not delete key {key_id}: {status} {payload}")


def ensure_key(name: str, *, allow: list[str] | None = None) -> dict:
    """The key named `name`, created if absent and re-pointed if it exists."""
    existing = find_key(name)
    if existing is None:
        return create_key(name, allow=allow)
    if allow is not None and existing.get("access", {}).get("allow") != list(allow):
        update_key(existing["id"], allow=allow)
        return find_key(name) or existing
    return existing


def get_settings() -> dict:
    status, payload = _admin_call("GET", "/api/settings")
    if status != 200 or not isinstance(payload, dict):
        raise config.ConfigError(f"could not read 9router settings: {status} {payload}")
    return payload


def set_request_logs(enabled: bool) -> dict:
    """Record the request-log intent on the gateway, best-effort.

    LIMITATION, verified against 9router 0.5.109: `PATCH /api/settings` answers
    200 and echoes the settings object, but the change does not survive — a
    following GET still reports the old value, and there is no API that reads
    per-request records back. The gateway's own request lines are what a trace
    reads instead; see `recent_requests`. This call is kept because the intent
    is worth recording and a future 9router may honour it.
    """
    status, payload = _admin_call("PATCH", "/api/settings", {"enableRequestLogs": bool(enabled)})
    if status not in (200, 201):
        raise config.ConfigError(f"could not set request logs: {status} {payload}")
    return {"requested": bool(enabled), "persisted": get_settings().get("enableRequestLogs")}


def recent_requests(limit: int = 40, *, container: str = "router9") -> list[dict]:
    """The gateway's recent request lines, parsed.

    Read from the gateway container's stdout, which is where 9router writes one
    line per upstream call. A line reads, in the fields this parses:

        [PROXY] <PROVIDER> | <model> | conn=<account> | pool=<id> | url=<e>
        ERROR <code> · <route>
        Trying model <n>/<m> : <id>

    Returns one dict per recognised line, newest last. Never raises: a missing
    docker binary or container yields an empty list, because tracing is
    diagnostic and must not break a command that already did its real work.
    """
    import re
    import shutil
    import subprocess

    if shutil.which("docker") is None:
        return []
    try:
        proc = subprocess.run(
            ["docker", "logs", "--tail", str(max(limit * 4, 200)), container],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    text = (proc.stdout or "") + (proc.stderr or "")

    proxy = re.compile(r"\[PROXY\]\s+(\S+)\s*\|\s*([^\s|]+)(?:\s*\|\s*conn=(\S+))?")
    trying = re.compile(r"Trying model\s+\d+/\d+\s*:\s*(\S+)")
    errored = re.compile(r"\bERROR\s+(\d{3})\s*·\s*([^\s·]+)")
    out: list[dict] = []
    for line in text.splitlines():
        if m := proxy.search(line):
            out.append(
                {"kind": "route", "provider": m.group(1), "model": m.group(2), "account": m.group(3) or ""}
            )
        elif m := trying.search(line):
            out.append({"kind": "attempt", "model": m.group(1)})
        elif m := errored.search(line):
            out.append({"kind": "error", "code": m.group(1), "route": m.group(2)})
    return out[-limit:]


def check_reachable(timeout: int = 30) -> str:
    """Return "" when 9router answers, else the reason it did not.

    Uses urllib so the check has no dependency beyond the standard library.
    The health endpoint is preferred: the model list is megabytes and takes
    tens of seconds to render.
    """
    import urllib.error
    import urllib.request

    base = config.NINEROUTER_BASE_URL.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    health = base + "/api/health"
    try:
        with urllib.request.urlopen(health, timeout=timeout) as resp:
            resp.read()
        return ""
    except urllib.error.HTTPError:
        pass  # the health route may be absent; fall through to the model list
    except (urllib.error.URLError, OSError) as exc:
        return f"9router unreachable at {config.NINEROUTER_BASE_URL}: {exc}"

    url = base + "/v1/models"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {config.NINEROUTER_API_KEY}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        return f"9router returned HTTP {exc.code} for {url}"
    except (urllib.error.URLError, OSError) as exc:
        return f"9router unreachable at {url}: {exc}"
    if not body:
        return "9router returned an empty model list"
    return ""


if __name__ == "__main__":  # pragma: no cover - manual check
    print(json.dumps(provider_block(), indent=2))
    print("reachable:", check_reachable() or "yes")
    print("XDG_CONFIG_HOME:", os.environ.get("XDG_CONFIG_HOME", "(unset)"))
