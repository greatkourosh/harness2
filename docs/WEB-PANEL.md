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

The unit sets `WorkingDirectory` to the repo and nothing else: `harness2/config.py`
loads `.env` itself (`load_dotenv` uses `setdefault`, so the shell still wins),
which is what puts the 9router base URL, key and models in the panel's
environment. That is worth knowing because a unit that starts with a bare
environment would otherwise route every task to a harness with no gateway — the
task would fail at the first model call, not at startup.

`Linger` is on for this user, so the unit keeps running with no login session.

The menu launcher (`~/.local/bin/harness2-web-ui`) opens the panel and starts it
only if it is not already answering; it used to start a second copy
unconditionally, which then lost the race for port 8090 and printed "Address
already in use" instead of opening anything.

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

**Detail** — the selected task: workspace, harness, model, why it was routed
there, exit code, the agent's **answer**, the delivered file names, and the
harness's own output log.

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

### Continuing a task

Each task normally starts cold: a fresh workspace and no memory of any earlier
task. **Continue this task** on a finished task changes that for one follow-up.
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
view, including the `state` field described above and the `answer` once it
settles.

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
