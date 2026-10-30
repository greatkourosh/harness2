# Harness control plane

> فارسی: [راهنمای فارسی](راهنمای-فارسی.md)
>
> Panel: [task panel](WEB-PANEL.md)

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
| `deepseek` (dsh) | `~/.dsh/profiles/headless/cordis.patch.yml` | `dsh --profile headless` | `qwen*`/`glm*`/`deepseek*` |
| `claude-code` | `~/.claude/settings.json` | `claude -p … --permission-mode auto --output-format stream-json --verbose` | Claude |
| `claude-desktop` | `~/.config/Claude/claude_desktop_config.json` | *(none)* | Gemini |

`claude-desktop` is inventory-only: the Electron app has no flag that takes a
prompt and prints an answer. Its Code sessions run through the `claude` CLI, so
they are the `claude-code` harness.

The deepseek adapter needs `pyyaml` (`uv pip install -e '.[harnesses]'`).

Note the two dsh facts the manifest encodes, both verified by running it:

- **Each profile has its own config file** under `~/.dsh/profiles/<name>/`. The
  one that runs headlessly is `headless`; `web` belongs to the browser UI. A
  model group written to the wrong profile has no effect on a `--profile
  headless` run.
- **`dsh --profile headless` takes no `--model` flag.** The model comes from the
  profile's `agent-default-model` plugin, so `model_flag` is empty and `apply`
  writes that entry — a provider block alone would leave the run with no model.

## Getting the binaries on PATH

`harness2-harness run` and `bench` exec the harness's `binary` by name, so it
must be resolvable on `PATH`. `harness2-harness list` shows `runs: no` for any
harness whose binary is missing.

`claude` installs itself on `PATH`. The other three do not, and each needs a
different treatment:

```bash
# pi and dsh ship an executable file with a `#!/usr/bin/env node` shebang,
# so a symlink is enough.
ln -sf ~/projects/pi_harness/packages/coding-agent/dist/bundle/cli.js ~/.local/bin/pi
ln -sf ~/projects/deepseek_harness/apps/cli/lib/bin.js           ~/.local/bin/dsh

# opencode needs a wrapper, not a symlink: the pinned checkout has no built
# dist, and its `bin/opencode` is CommonJS inside an ESM package, which fails
# with "ReferenceError: require is not defined". Harness2 runs it from source
# through bun, so the wrapper mirrors that.
```

```bash
cat > ~/.local/bin/opencode <<'EOF'
#!/bin/bash
exec /path/to/harness2/dependencies/tools/node_modules/.bin/bun \
  run --cwd /path/to/harness2/dependencies/opencode/packages/opencode \
  --conditions=browser src/index.ts "$@"
EOF
chmod +x ~/.local/bin/opencode
```

Verify with `pi --version`, `dsh --version`, `opencode --version`.

Alternatively, set `binary` in the manifest to an absolute path and skip
`PATH` entirely — the runner takes either.

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

## Images and vision

A task with an image works, but **which combo you send it to decides whether the
image survives**. Verified against the gateway on 2026-10-26 with a rendered
digit:

| Combo | Routed to | Read the image? |
|---|---|---|
| `claude-sonnet-4-6` | `deepseek-ai/DeepSeek-V4.1-Flash` | yes |
| `General` | `gemini-3.8-flash-n` | yes |
| `Safe` | `claude-opus-4-6-thinking` | **no** — "no image attached" |
| `Reasoning` | a Cloudflare-hosted Qwen distill | **no** |
| `All`, `Coding`, `Vision` | (timed out at 45s) | unknown |

Both wire protocols carry the image: the Anthropic Messages shape
(`/v1/messages`, which claude code and claude desktop use) and the OpenAI shape
(`/v1/chat/completions`) each returned the right digit. So the protocol is not
the variable — the *route the combo lands on* is. A combo resolves over many
upstreams, and the one that answers may not accept image content.

The practical consequence: if a task has an image, name a route known to work
(`9router/claude-sonnet-4-6`) rather than leaving it to a combo such as `Safe`
or `Reasoning`, and do not assume that a combo which works for text works for an
image.

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

## Routing a task to a harness

`do` takes a task, decides which harness should run it, runs it, and falls back
if that harness fails:

```bash
harness2-harness do "fix the auth bug in parser.py"
harness2-harness do "draft a legal memo about this contract" --task-dir ./matter
```

```
→ pi  (model 9router/All)
  ok — delivered ['answer.txt']
  run dir: runs/do/t1/pi
  routed by: matched bug
```

A task need not produce a file. Asking a question is a task, and the reply is
its result, so `do` reports an answered run as the success it is and prints the
answer:

```
→ claude-code  (model claude-sonnet-4-6)
  ok — answered (no file written)
  answer:
    The parser drops the trailing token because …
```

`route` shows the decision without running anything:

```bash
harness2-harness route "translate these support emails in bulk"
```

### How the decision is made

Each manifest declares the task words it is good for, and its rank:

```toml
routes = ["code", "coding", "debug", "bug", "refactor", "test"]
priority = 10
default = false          # the fallback when nothing matches
```

The router scores every **runnable** harness (installed, with a headless mode)
by how many of its `routes` words the task mentions, breaks ties on `priority`,
and picks the winner. An unmatched task goes to the harness marked `default`,
or failing that to the highest-priority candidate — routing always answers.

Keywords match **whole words**. A substring test would fire `git` inside
`digital` and `web` inside `website`, which is what makes a keyword list
untrustworthy; the boundary check is why it does not.

Override the decision with `--harness pi`, or pin a model with `--model`.

### Falling back

A harness that exits non-zero is not a success, so `do` tries the next candidate
in priority order — the task is what matters, not which agent ran it. A clean
run counts as a success whether it delivered a file or answered in prose; only a
non-zero exit (or no metrics at all) sends `do` on to the next candidate.
Every attempt gets its own run directory
(`<results-root>/<run-id>/<harness-id>/`), and the attempts are listed at the
end. `--no-fallback` runs only the chosen harness.

Fallback costs a full run per attempt, and a slow gateway can make each one
expensive; pass `--no-fallback` when you want a single, predictable call.

## Running a task through a named harness

`bench` runs one task through a registered harness and writes a *substrate run
directory* — the same layout the OpenCode and Codex runners produce, so a
downstream reader does not care which harness ran:

```bash
harness2-harness bench claude-code "Summarize documents/report.pdf" \
  --run-id demo/claude-code/base1 \
  --results-root runs/lab/results/claude-code \
  --task-dir /path/to/task_folder \
  --model claude-sonnet-4-6
```

```
<results-root>/<run-id>/
    workspace/                the task's own files, plus output/, which is graded
    output/                   the graded copy of workspace/output
    config.json               harness, model and resolved argv
    <harness>_session.jsonl   the captured stream
    metrics.json              turn/token/wall figures, written LAST as the completion mark
```

`metrics.json` also carries **`answer`**, the harness's own closing message.
A task need not write a file — a question is answered in prose, and that reply
is the deliverable — so the closing message is recorded here for the task panel
to show, and a clean run that produced only an answer counts as `done` rather
than as an empty run. It is read from wherever the harness puts it: claude's
terminal `result.result`, codex's last `agent_message` item.

`harness2.substrate.ExternalCliSubstrate` is the underlying class. It is driven
by the manifest: the argv comes from `headless`, the binary from `binary`, the
environment from `env` plus the adapter's `run_env_set`/`run_env_drop` hooks.
The editable tree is the **AGENTS.md + `.agents/skills/`** convention, which pi,
codex and claude code all read, so one surface covers every external harness.

Two things worth knowing about this path:

- **`--substrate <harness-id>` is accepted by the LAB benchmark only.**
  `harness2-run --bench lab --substrate claude-code` (and `pi`, `deepseek`, …)
  runs a registered harness as a real LAB cell: it is scored by LAB's own judge
  and appears in reports and namespaces like the in-tree `oc`/`cx` cells.

  It works because an external harness is codex-shaped to every consumer that
  branches on the surface (`ExternalCliSubstrate.surface()` returns the same
  `AGENTS.md` surface `cx` uses), so `lab.py` shares one launcher body, base
  tree and result path with the cx cell and differs only in the argv — which
  comes from the harness manifest. A manifest `default_model` is what the run
  uses, so no gateway round trip is needed to resolve it.

  `jobbench.py` and `workbuddy.py` still hardcode the two-way `oc`/`cx` split
  and refuse an external id with a message saying so: jb's run directories and
  `TASK_IDS`-driven launchers need a bridge that does not exist yet, and wb
  resolves a Harbor harness *slug* against its own config tree and runs trials
  in Docker, where a foreign CLI has none. Do not add an id to `SUBSTRATES` to
  "make it work" — an id accepted there would silently build a `CodexSubstrate`.

  The doc-reader default is podman sandbox mode; on a host with only Docker,
  pass `LAB_DOC_PARSER=host` / `--doc-parser host` or the agent's binary-document
  reads return noise.

  **Trajectory and token metrics for a non-codex harness.** The capture is the
  harness's own stdout, which only the codex renderer understood, so `claude -p`
  used to render as "(no parseable events)" with a `metrics_note` saying the
  counts were absent — leaving the improver and selector, which read
  trajectories, blind for such a harness. Grading was never affected:
  deliverables are read from `output/`, not from the stream.

  Closed for claude code by asking the harness for structured output rather than
  teaching the renderer only: its manifest's `headless` now carries
  `--output-format stream-json --verbose` (the latter is required under
  `--print`, not decorative), and two small branches read that schema —
  `parse_session` in the LAB runner and `render_events` in
  `harness2/substrate/codex.py`. Two facts that shape the code:

  - **Usage lives only on the terminal `result` event.** The per-assistant events
    carry an all-zero `usage`, so summing them reports zero; the totals
    (`input_tokens`, `output_tokens`, `cached_input_*`, `num_turns`) are read from
    `result` alone. A stream that ends before that event reports its usage as
    *absent* (`usage_present: false`, plus the `metrics_note`) rather than as
    zeroes, so a rollout killed mid-turn cannot be mistaken for a cheap one.
    Both outcomes are verified on real runs: a finished one reports
    `turns=17 in=832808 out=5811` matching its raw `result` event, while one killed
    at the wall reports `tool_calls: 141` with tokens absent and the note naming the
    incomplete session.
  - **Claude writes `type` last.** Its `result` event begins
    `{"duration_api_ms":...`, so a reader that keys on the codex `{"type":` line
    prefix silently drops it. The claude branch parses by the `{` prefix instead;
    codex's own reading rule is unchanged.

  A harness that emits neither shape still gets the honest `metrics_note`. The
  renderer is not a per-harness registry — a third shape would earn one, the
  second did not.

  `harness2-harness bench` writes the same stream to `<harness>_session.jsonl`,
  and `harness2/benchmarks/external_runner.py` summarises it with its own reader;
  that reader applies the same rule. Measured on a two-line probe: with the flag,
  a bench run reported `turn_count: 1, input_tokens: 30952, output_tokens: 1`
  matching its raw `result` event, where before the fix `turn_count` was null
  (the totals only looked right by luck, because the assistant events are zero).
- **Headless delivery needs a permission mode that lets Bash run, and
  `acceptEdits` is not one.** With a prompting mode `claude -p` holds every
  unlisted action for approval, has nobody to ask, and the agent burns its whole
  wall retrying: one measured rollout spent 5400s on 322 denials and delivered a
  scratch file. `acceptEdits` auto-approves only edits and a small safe-command
  allowlist — `echo` passes, `python3 -c`, `unzip -l`, `pandoc` and `awk` do not
  (0 allowed / 1 denied on a straight probe). LAB cannot work under that, because
  reading a binary document *is* `python3 scripts/read_doc.py <path>`. The
  manifest therefore carries `--permission-mode auto` (3/3 on the same probe),
  which lets Bash run while keeping the classifier in the loop; the alternative
  `bypassPermissions` is what the codex cell effectively does already
  (`--dangerously-bypass-approvals-and-sandbox`). The workspace is a per-run
  scratch directory either way.
