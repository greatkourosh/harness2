# Task panel

A local web panel for running tasks against several projects at once, and
switching between them while they run.

```bash
harness2-web                     # http://127.0.0.1:8090
harness2-web --port 9000
```

There is also a launcher: **Harness2 Tasks** in the application menu starts the
panel and opens it in the browser.

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
(`queued` / `running` / `done` / `failed`), harness and model. The page polls
every 1.5s while anything is running and every 6s when everything has settled,
so a quiet panel does not keep waking the machine.

**Detail** — the selected task: workspace, harness, model, why it was routed
there, exit code, the delivered file names, and the harness's own output log.

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
| `POST` | `/api/tasks` | `{"task": "...", "project": "...", "harness": "", "model": ""}` |
| `POST` | `/api/tasks/<id>/cancel` | mark it failed so the UI stops waiting |
| `POST` | `/api/tasks/<id>/forget` | drop a finished task from the list |

```bash
curl -s -X POST http://127.0.0.1:8090/api/tasks \
  -H 'Content-Type: application/json' \
  -d '{"task":"find the failing test","project":"myrepo"}'
```

## Limits worth knowing

- **`cancel` does not kill the harness process.** The runner starts it in its
  own session and the store holds no handle, so cancel marks the task failed and
  says so. The process runs to completion or its own timeout; its output still
  lands in the run directory.
- **No authentication.** It binds to loopback by default. `--host 0.0.0.0`
  exposes it to the network and prints that warning; only do it on a network you
  trust, because anyone who can reach it can start a task against any listed
  project.
- **A non-git project is written to directly.** That is the trade: no safe cheap
  copy. If you are about to point the panel at something you care about, check
  whether it is a repository first.
- **State is in memory.** Restarting the panel loses the task list, though each
  run's own directory under `runs/web/<job-id>/` survives.
