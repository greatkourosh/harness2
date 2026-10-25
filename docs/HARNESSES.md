# Harness control plane

`harness2-harness` manages the agent frameworks installed on this machine as
**independent peers** and points them all at one model gateway. They are never
nested: each keeps its own config file, its own CLI, its own skills, and its own
model group. The only thing they share is where their traffic meets.

```bash
harness2-harness list                # every registered harness
harness2-harness show pi             # one harness's config and model group
harness2-harness models pi           # the gateway models it may see
harness2-harness apply pi            # write its model group into its config
harness2-harness run pi "task"       # one headless run through the gateway
harness2-harness trace pi            # where that harness's own log lives
harness2-harness keys                # the gateway's access keys
harness2-harness sync-keys           # ensure one key per installed harness
```

## How a harness is described

One TOML manifest per harness in `harness2/harnesses/manifests/` is the *only*
place a harness is described, so adding a sixth means adding a file, not editing
Python:

```toml
id = "pi"
label = "pi (Earendil Works)"
config_path = "~/.pi/agent/models.json"
config_format = "pi-provider"
adapter = "pi"
key_name = "pi_harness"

binary = "pi"
headless = ["--print", "{prompt}"]
model_flag = "--model"
trace_glob = "~/.pi/agent/sessions/**/*.jsonl"

# A model group. Globs match gateway ids; an empty list means unrestricted.
allowed_models = ["All", "Safe", "Coding", "General", "Fast", "Reasoning", "Vision", "local"]
```

An **adapter** (`harness2/harnesses/adapters.py`) knows that harness's config
shape and nothing else. Writes are always merge-with-backup: a harness config is
the user's own file, often carrying settings this package knows nothing about.

## Registered harnesses

| id | config | headless | model group |
|---|---|---|---|
| `pi` | `~/.pi/agent/models.json` | `pi --print` | routing combos |
| `opencode` | `~/.config/opencode/opencode.json` | `opencode run` | Claude |
| `deepseek` (dsh) | `~/.dsh/profiles/web/cordis.patch.yml` | `dsh --profile headless` | `qwen*`/`glm*`/`deepseek*` |
| `claude-code` | `~/.claude/settings.json` | `claude -p` | Claude |
| `claude-desktop` | `~/.config/Claude/claude_desktop_config.json` | *(none)* | Gemini |

`claude-desktop` is inventory-only: the Electron app has no flag that takes a
prompt and prints an answer. Its Code sessions run through the `claude` CLI, so
they are the `claude-code` harness.

The deepseek adapter needs `pyyaml` (`uv pip install -e '.[harnesses]'`).

## Model isolation

Two layers, and only one of them actually isolates:

- **The harness config is the real fence.** `apply` writes only that harness's
  model group into its own config. A model outside the group is absent from the
  config, which the harness then refuses:

  ```console
  $ harness2-harness apply pi
  pi: 8 models -> /home/kourosh/.pi/agent/models.json
    backup /home/kourosh/.pi/agent/models.json.bak-20261025-072619

  $ pi --print "hi" --model 9router/gemini-2.5-flash
  Warning: Model "gemini-2.5-flash" not found for provider "9router"...
  404: {"message":"No active credentials for provider: gemini",...}
  ```

- **The gateway key's allow-list does not enforce anything.** `sync-keys` records
  a model allow-list on each harness's key, for the 9router dashboard only.
  Against 9router 0.5.109 it was verified that a key restricted to `All` still
  served `Safe` with HTTP 200, so do not rely on it. It is written because the
  intent is worth recording and a later 9router may honour it.

## Reading the gateway catalog

`/v1/models` — the model API's own list — renders the entire upstream catalog and
has been observed to take tens of seconds or time out. The dashboard API answers
the same set quickly, and it is the source `harness2-harness models` uses:

| Endpoint | Latency | Contents |
|---|---|---|
| `/api/models` | ~0.4s | ~1690 individual models |
| `/api/combos` | fast | 16 routing combos (`All`, `Safe`, `claude-sonnet-4-6`, …) |

Both are merged by `ninerouter.fetch_models()`, which caches the result on disk
(`dependencies/.9router-models.json`). Combos do **not** appear in `/api/models`,
so a harness configured with `All` would otherwise match nothing.

## Tracing a run

`trace` reports two things: the harness's own session log (attributed, and the
one that identifies the caller), and the gateway's recent request lines (global).

```console
$ harness2-harness trace pi --limit 2 --tail 2
pi (Earendil Works): /home/kourosh/.pi/agent/sessions
  …/2026-10-25T06-06-09-611Z_01a171aa-….jsonl
  --- last session: …jsonl ---
  {"type":"message","message":{"role":"user",…"text":"Reply with exactly: PONG"}}
  {"type":"message","message":{"role":"assistant",…"text":"PONG"}}
  --- gateway (all harnesses; 9router logs do not name the caller) ---
  OPENAI-COMPATIBLE-CHAT-…  deepseek-ai/DeepSeek-V4.1-Flash via kouroshvega
```

Two limits worth knowing:

- 9router's log lines carry **no caller attribution** — only the upstream account
  (`conn=…`) and pool. There is no way to tell from the gateway which harness
  made a request, which is why the harness's own session log is the useful half.
- `HARNESS2_9ROUTER_ADMIN_PASSWORD` is needed for the key and settings APIs. It
  is the dashboard password and is never used for a model call.
