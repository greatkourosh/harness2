# Telegram bot — the bot is a front end for the running task panel.

`harness2-telegram` puts the task panel in Telegram: text a task, it runs on
the machine, and the answer, its output, and completion notifications come back
here. It is deliberately a thin client of the running panel's JSON API — the
same one `harness2-web` serves — so the isolation, the cloning, the threads,
and the persisted task list are all inherited, and the bot needed no second
task engine to be written.

This host cannot reach `api.telegram.org` directly, so the bot talks out
through a SOCKS5 proxy. Every Bot API call is tunnelled; the working proxy is
remembered, so a dead proxy costs one failover, not a permanent slowdown.

## On this machine

The bot runs as a user unit, like the panel:

```bash
systemctl --user status harness2-telegram.service
systemctl --user restart harness2-telegram.service
journalctl --user -u harness2-telegram.service -f
```

It `Requires=` the panel (`harness2-web.service`), because every command ends in
a call to it, so restarting a task panel that is mid-request can make the bot
report it. Restart both together:

```bash
systemctl --user restart harness2-web.service harness2-telegram.service
```

As with the panel, the unit file lives only in `~/.config/systemd/user/` and is
not version-controlled. It sets the same `PATH` as the panel's unit — the bot
does not spawn harness binaries, but `~/.local/bin` is where the wrapper it
runs from lives, and a cheap consistent `PATH` avoids a second class of
silently-dead service.

## What it reads from the environment

| variable | used for |
|---|---|
| `TELEGRAM_BOT_TOKEN` | the bot identity (required). |
| `TELEGRAM_BOT_ID` | not read by the bot; recorded for humans. |
| `HARNESS2_TELEGRAM_CHAT_ID` | the one chat the bot answers. Without it the bot answers only `/whoami` — and that message carries the chat's id and the env line to set. |
| `SOCKS5_*` | proxies, used in numbered order as a failover list (`SOCKS5_01`, then `SOCKS5_02`, …). Format `host:port`, SOCKS5, no auth. The list is what makes a dead proxy survivable. |
| `HARNESS2_PANEL_URL` | the panel the bot talks to; defaults to `http://127.0.0.1:8090`. |

All of it comes from `.env`, which the bot loads itself (`config.load_dotenv`,
`setdefault`, so the shell still wins). Editing `.env` and restarting the unit
is the whole operator story; there are no secrets in the unit file.

The proxies in `.env` are numbered with zero-padded names for a reason: the bot
orders the keys by sort, so `SOCKS5_10` follows `SOCKS5_09` rather than
`SOCKS5_01`. The first entry that carries a request to Telegram wins, and is
remembered for subsequent calls.

## Verification

```bash
# the bot answers the command menu, or the error tells you why
.venv/bin/harness2-telegram --once
journalctl --user -u harness2-telegram.service --since '-30min' | tail
```

In Telegram: `/status` shows the panel URL, the proxy in use, and what is
running. `/help` shows every command. Send any plain message — the bot asks
which project, then runs it.

## Commands

| command | does |
|---|---|
| `/run <task>` | pick the project, then run it |
| `/tasks` | recent tasks (10), each with a button to its detail |
| `/task <id>` | one task: state, workspace, harness, answer, delivered files |
| `/log <id>` | the task's captured output, rendered |
| `/answer <id>` | just what the agent said |
| `/cancel <id>` | stop a running task |
| `/forget <id>` | drop a finished task from the list |
| `/continue <id>` | same workspace, conversation resumed — the faithful message takes over |
| `/projects` | the directories a task can target, `·git` marking repositories |
| `/harnesses` | the agents available, and which is default |
| `/status` | panel URL, proxy in use, running tasks |
| `/whoami` | this chat's id |
| `/help` | this list |

The command menu is registered with Telegram at startup (`setMyCommands`), so
the client's `/` list is this build's list.

Any plain message is a task. If a `/continue` is pending, the message is the
follow-up and runs in that task's workspace with its conversation resumed; the
conversation lives in the workspace, not in the bot, so a follow-up survives
the bot's own restart.

## The command set, in short

Required by a task that can run, be watched, and be stopped:

```
/run  /tasks  /task  /log  /cancel
```

Recommended for the round trip and housekeeping:

```
/answer  /continue  /forget  /status  /projects  /harnesses  /whoami
```

## Access

The bot answers only the chat it is told to answer:
`HARNESS2_TELEGRAM_CHAT_ID`, comma-separated ids accepted. A bot is reachable by
whoever learns its username, and every task it starts runs an agent with write
access to a project directory — a clone for a git project, the directory itself
otherwise — so the closed default is deliberate: with no value set, the bot
answers nothing and logs `no HARNESS2_TELEGRAM_CHAT_ID set`. An unaudited chat
is refused on every message, and the refusal carries that chat's id plus the
exact env line to set, so onboarding is a copy-paste.

The panel behind it has **no authentication** and binds to loopback; the bot
does not relax that. The bot is a front end for the local panel, not a
tunnel into the network.

As with the panel, there is no build step — the bot imports `harness2`
straight from the repo — so an edit to `harness2/telegram/*.py` lands on disk
but the running process keeps the old code until

```bash
systemctl --user restart harness2-telegram.service
```

## The proxy code

`harness2/telegram/transport.py` implements SOCKS5 (RFC 1928) on the standard
library — `socket`, `ssl`, `http.client` — because `pyproject.toml` declares no
runtime dependencies. One request per socket (`Connection: close`), which is
what makes rotating proxies safe between calls.
