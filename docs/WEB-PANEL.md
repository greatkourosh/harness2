# Task panel

A local web panel for running tasks against several projects at once, and
switching between them while they run.

```bash
harness2-web                     # http://127.0.0.1:8090
harness2-web --port 9000
```

There is also a launcher: **Harness2 Tasks** in the application menu starts the
panel and opens it in the browser.

### Running it as a service

On this machine the panel runs as a user unit rather than a background process,
so it survives a logout and comes back after a reboot:

```bash
systemctl --user status harness2-web.service
systemctl --user restart harness2-web.service
journalctl --user -u harness2-web.service -f
```

The unit sets `WorkingDirectory` to the repo and a `PATH` (see below) and nothing
else: `harness2/config.py` loads `.env` itself (`load_dotenv` uses `setdefault`,
so the shell still wins), which is what puts the 9router base URL, key and models
in the panel's environment. That is worth knowing because a unit that starts with
a bare environment would otherwise route every task to a harness with no
gateway — the task would fail at the first model call, not at startup.

`Linger` is on for this user, so the unit keeps running with no login session.

The menu launcher (`~/.local/bin/harness2-web-ui`) opens the panel and starts it
only if it is not already answering; it used to start a second copy
unconditionally, which then lost the race for port 8090 and printed "Address
already in use" instead of opening anything.

#### The unit needs `~/.local/bin` on `PATH`

```ini
Environment=PATH=/home/kourosh/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
```

The panel spawns each harness's `binary` from its manifest by name, and they all
live in `~/.local/bin` (`opencode` and `t3` as wrappers, `pi` and `dsh` as
symlinks, `claude` under nvm at `~/.nvm/versions/node/<ver>/bin/claude`). A
login shell puts that directory on `PATH` through `~/.bashrc` and `~/.profile`, but systemd
sources neither and falls back to its own compiled-in `PATH`
(`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/games:
/usr/local/games:/snap/bin:/snap/bin`), which omits it. The failure is not at
startup and not at the model call: the task starts, then every run dies at
`exec` with **exit 127** and `could not exec claude: [Errno 2] No such file or
directory: 'claude'` in the run's `<harness>_stderr.log`. It affects every
harness, not just the one named in the message.

To check a running panel before touching manifests or the repo:

```bash
tr '\0' '\n' < /proc/$(systemctl --user show -p MainPID --value harness2-web.service)/environ | grep ^PATH
```

Note the unit file lives only in `~/.config/systemd/user/` and is not in this
repo, so an edit there is not version-controlled. Do not reach for
`harness2-harness list` to confirm this: its `runs` column reflects the
manifest's `can_run`, not whether the binary resolves, so it reports `yes` even
when nothing on `PATH` would exec. Use `command -v <binary>` under the unit's
`PATH`, or the `/proc` line above. The manifest's `binary` also takes an absolute
path, which skips `PATH` resolution entirely (see
[harnesses](HARNESSES.md#getting-the-binaries-on-path)).

After editing the unit: `systemctl --user daemon-reload && systemctl --user
restart harness2-web.service`.

#### Editing the code needs a restart too

The panel is an editable install: it imports `harness2` straight from this repo,
so there is **no build step** — but the running process read the adapter
registry, the harness manifests and the page HTML at import time and never
re-reads them. A change lands on disk and the panel keeps serving the old code
until

```bash
systemctl --user restart harness2-web.service
```

This is not cosmetic. Adding the `t3code` manifest and adapter and then running a
task through the panel gave

```
ConfigError: no adapter for harness 't3code' (adapter='t3code');
known: claude-code, claude-desktop, deepseek, opencode, pi
```

— the code was correct and committed; the process had started before the commit
and its `ADAPTERS` dict had no such key. The same applies after editing a
harness manifest, a substrate renderer, or `cli/web.py`'s `PAGE`.

To tell whether a running panel predates your change, compare the unit's start
time with the commit:

```bash
systemctl --user show -p ExecMainStartTimestamp --value harness2-web.service
git log -1 --format=%ci
```

### Local domain

`https://harness2.local` serves the panel through the host's Caddy, the same way
the other projects are reached (see `PER_PROJECT_DOMAINS.md` in the
`project_manager` repo for the full wildcard-DNS/PKI setup).

```bash
curl -so /dev/null -w '%{http_code}\n' https://harness2.local/   # 200
```

Two details that are easy to get wrong when adding another host this way:

- **The TLS leaf is its own.** `harness2.local` has a standalone Caddy block and
  its own mkcert leaf (`/etc/caddy/harness2-local.pem`), rather than a SAN added
  to the shared `project-local.pem`. That shared leaf covers every `*.local`
  project, so extending it means reissuing one certificate for all of them —
  and `.local` is on the Public Suffix List, so a mistake there is a browser
  cert error on twenty hosts at once. A separate leaf has no blast radius, and
  the same trusted CA signs it, so nothing changes for the browser.
- **The block still needs an `@host` matcher.** A bare `harness2.local { … }`
  works for Caddy but not for the dashboard: `modules/domains.py` reads routes
  by scanning for `@name host <domain>.local`, so the panel would list the
  domain as having *no route* and probe it as unreachable. The matcher is what
  makes it visible to the panel, not just routable.

## What it is for

The CLI runs one task and you wait for it. The panel is for when you have
several projects and want to hand work to them without serialising yourself
behind one rollout:

- Queue a task against a project; it starts immediately in the background.
- Queue several at once, against different projects.
- Switch between them — each keeps running.
- Watch a task's output as it streams, not only when it finishes.

## Screens

**New task** — a task box, a project picker, and a harness picker. Leaving the
harness on `auto` routes by the task text, exactly as `harness2-harness do`
does; picking one overrides that.

**Task list** — every task, newest first, with its project, state
(`queued` / `running` / `done` / `no output` / `failed`), harness and model. The
page polls every 1.5s while anything is running and every 6s when everything has
settled, so a quiet panel does not keep waking the machine.

**Detail** — the selected task: the **prompt** it was given (first thing in the
pane, so you do not have to remember what you asked), workspace, harness, model,
why it was routed there, exit code, the agent's **answer**, the delivered file
names, and the harness's own output log.

#### The output log is rendered, not raw

The log pane shows the captured stream through the same renderers the benchmark
trajectory view uses (`harness2/substrate/codex.py`, `.../opencode.py`), so a
task's log here reads like the same task's trajectory there: one
`[ACTION]`/`[OBSERVATION]`/`[ASSISTANT]` line per event instead of one JSON
object per line.

Two shapes are not event streams and are handled separately:

- **t3code** writes one object per turn (`{"threadId":…,"reply":…}`), not a
  stream. It gets a small renderer of its own.
- **pi** (`--print`) writes bare prose. There is nothing to parse, so the raw
  text is the log.

Anything no renderer recognises falls back to raw, and a harness that dies at
startup writes only `<harness>_stderr.log`, which the pane shows while the
session file is still empty. Rendering is capped at 40 000 characters
(`jobs.LOG_MAX_CHARS`).

### What a task's state means

Exit status alone does not tell you whether the work happened, so the panel
separates the three cases:

| state | meaning |
|---|---|
| `done` | `exit 0`, and the run either delivered a file or produced an answer. |
| `no output` | `exit 0`, but the agent neither wrote a file nor said anything. Nothing went wrong; there was simply no result. Not a failure. |
| `failed` | Non-zero exit, no metrics at all, or cancelled. |

A task does not have to write a file. `hey` is answered in prose, and that reply
*is* the deliverable — the runner records it as `answer` in `metrics.json` and
the panel shows it under the task, so a conversational prompt reads as finished
work rather than as an empty run.

### How "running" shows progress

An agent's inner work is not measurable, so there is no fake percentage. What
the panel shows instead is the honest signals:

- **elapsed** — the time since the task started, in the detail pane, ticking
  every second.
- **heartbeat** — the stream is the heartbeat. Every job re-reads its
  `<harness>_session.jsonl` as long as it is `running`; a fresh write refreshes
  the stall clock.
- **stalled** — a `running` job whose stream has had no new write for
  `jobs.STALL_AFTER` (default 180 s) is flagged `stalled`, as a pill on the
  row and a banner in the detail pane with a **Stop it** link. The web API
  exposes it per task as `stalled`.

The point is to catch a silent hang early. A harness that is waiting on the
model/gateway — the model id never answering — writes nothing, reads as
`stalled`, and can be stopped in seconds instead of watching an hour of
"running" with no output, which is what exit-124 timeouts look like from the
outside.

### Continuing a task

Each task normally starts cold: a fresh workspace and no memory of any earlier
task. **Continue this task** on a finished task — done, no output, or failed —
changes that for one follow-up.
The new task runs in *the same workspace the earlier one worked in* — its files
are still there — and the harness resumes the conversation it left, so "now do
step 2" works on the plan you just wrote.

Continuing inherits the project and the harness: a different project has a
different tree, and a different harness has no conversation to resume. Clicking
**Continue this task** therefore moves the project and harness selectors onto
that task's values — the inherited ones are what will actually run, and leaving
the dropdowns on whatever was picked last made the follow-up look as if it would
land somewhere else. It is offered only for harnesses whose manifest declares a
`continue_flag` (claude, pi, and opencode do; dsh's headless profile does not).
The detail view marks a continued task with the task it continues.

A continuation is a chain, not a thread the panel remembers: each task has its
own entry, and the harness's memory of the conversation lives in the workspace,
not in the panel. The list itself is persisted to `runs/web/index.json`, so a
restart keeps the tasks (and with them the ability to continue one); a task that
was mid-flight when the panel went away is shown as `interrupted`, since a
process that is gone cannot still be running it.

## Where a task actually runs

This is the part worth understanding before pointing it at real work.

Every task gets its own workspace, and which one depends on the project:

| Project | Workspace | Why |
|---|---|---|
| a git repository | a fresh `git clone --shared` under `runs/web/_ws/<job-id>/` | the harness can edit freely and the **real working tree is untouched**; you review the clone and copy back what you want |
| not a git repository | the project directory itself | there is no cheap safe copy, so it runs in place |

A cloned run is marked `· clone` in the list and its detail view says the real
tree was untouched. The project picker shows `·git` next to repositories, so you
can see which is which before you start.

This matters because a harness given a coding task will happily rewrite files.
With a clone, a bad run costs you nothing.

## API

The page is a thin client over a small JSON API, if you would rather script it:

| Method | Path | Does |
|---|---|---|
| `GET` | `/api/projects` | project directories, each with a `git` flag |
| `GET` | `/api/harnesses` | runnable harnesses |
| `GET` | `/api/tasks` | every task's current state |
| `GET` | `/api/tasks/<id>` | one task |
| `GET` | `/api/tasks/<id>/log` | its captured output |
| `POST` | `/api/tasks` | `{"task": "...", "project": "...", "harness": "", "model": "", "continue_from": ""}` |
| `POST` | `/api/tasks/<id>/cancel` | mark it failed so the UI stops waiting |
| `POST` | `/api/tasks/<id>/forget` | drop a finished task from the list |

```bash
curl -s -X POST http://127.0.0.1:8090/api/tasks \
  -H 'Content-Type: application/json' \
  -d '{"task":"find the failing test","project":"myrepo"}'
```

`harness` and `model` are optional; leaving `harness` empty routes by the task
text exactly as `harness2-harness do` does. The response is the new task's own
view, including the `state` field described above, the `stalled` flag while it
is `running`, and the `answer` once it settles.

`continue_from` names an earlier task to continue; with it, `project` is optional
and ignored (both it and the harness come from that task):

```bash
curl -s -X POST http://127.0.0.1:8090/api/tasks \
  -H 'Content-Type: application/json' \
  -d '{"task":"now do step 2","continue_from":"04fa126a7b05"}'
```

## Cancel

Cancel stops the task. The runner starts each harness in a session of its own
and records the pid in the run directory, so the panel signals that process
group — SIGTERM first, since these CLIs flush their session transcript on it,
then SIGKILL if it has not gone. A cancelled task stays `failed`; the run thread
cannot flip it back to `done` even if files landed before the kill, because
reporting success for work you stopped is worse than reporting nothing.

## Limits worth knowing

- **No authentication.** It binds to loopback by default. `--host 0.0.0.0`
  exposes it to the network and prints that warning; only do it on a network you
  trust, because anyone who can reach it can start a task against any listed
  project.
- **A non-git project is written to directly.** That is the trade: no safe cheap
  copy. If you are about to point the panel at something you care about, check
  whether it is a repository first.
- **The task list survives a restart.** It is persisted to `runs/web/index.json`,
  which matters beyond convenience: continuing a task needs the earlier one still
  in the list. A task that was mid-run when the panel stopped comes back as
  `interrupted`, not `running` — the run thread that would have finished it is
  gone, and a task cannot be left running by a process that no longer exists.
