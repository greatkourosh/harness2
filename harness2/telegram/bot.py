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

"""The bot: a Telegram front end for the task panel.

Long polling, not a webhook. A webhook would need a public HTTPS endpoint and a
certificate; this process already has an outbound proxy, so `getUpdates` keeps
everything in that direction.

Access is refused unless the chat is the configured one. A bot is reachable by
whoever learns its username, and every task it starts runs an agent with write
access to a project directory, so the open case is the wrong default: with no
allowlist set the bot answers only `/whoami`, which is how the operator learns
the id to set.
"""

from __future__ import annotations

import html
import re
import time

from .panel import Panel, PanelError
from .transport import Telegram, TelegramError

# Telegram rejects anything longer, so every assembled message goes through this.
LIMIT = 4000
# Terminal states: a watched task reaches one and the notification fires.
SETTLED = ("done", "no_files", "failed", "interrupted")

HELP = """<b>Harness2</b> — run agent tasks on this machine.

<b>Tasks</b>
/run &lt;task&gt; — pick a project, then run it
/tasks — recent tasks, with buttons
/task &lt;id&gt; — one task: state, answer, files
/log &lt;id&gt; — its output, rendered
/answer &lt;id&gt; — just what the agent said
/cancel &lt;id&gt; — stop it
/forget &lt;id&gt; — drop it from the list
/continue &lt;id&gt; &lt;follow-up&gt; — same workspace, resumed

<b>This machine</b>
/projects — the directories a task can target
/harnesses — the agents available, and which is default
/status — panel, proxy, and what is running
/whoami — this chat's id

Any plain message is taken as a task.

/help — this list"""

# Registered with Telegram at startup, so the command menu in the client is
# this list rather than whatever was set by hand in BotFather. Order is the
# order Telegram shows.
COMMANDS = [
    ("run", "run a task against a project"),
    ("tasks", "recent tasks, with buttons"),
    ("task", "one task: state, answer, files"),
    ("log", "a task's rendered output"),
    ("answer", "just what the agent said"),
    ("cancel", "stop a running task"),
    ("forget", "drop a finished task from the list"),
    ("continue", "follow up in the same workspace"),
    ("projects", "directories a task can target"),
    ("harnesses", "agents available"),
    ("status", "panel, proxy, and what is running"),
    ("whoami", "this chat's id"),
    ("help", "this list"),
]

# The always-visible keyboard. Its labels are the commands, so no mapping table.
MENU = {
    "keyboard": [[{"text": "/tasks"}, {"text": "/projects"}], [{"text": "/status"}, {"text": "/help"}]],
    "resize_keyboard": True,
    "is_persistent": True,
}


def _short(text: str, width: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _chunks(text: str, limit: int = LIMIT) -> list[str]:
    """`text` as messages Telegram will accept, split on line boundaries."""
    out: list[str] = []
    while text:
        if len(text) <= limit:
            out.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return out or [""]


class Bot:
    def __init__(
        self,
        telegram: Telegram,
        panel: Panel,
        *,
        allowed_chats: list[str] | None = None,
        poll_timeout: int = 10,
        log=None,
    ):
        self.tg = telegram
        self.panel = panel
        self.allowed = {str(c) for c in (allowed_chats or []) if str(c).strip()}
        self.poll_timeout = poll_timeout
        self.log = log or (lambda *_: None)
        self.offset = 0
        self.stopping = False
        # Per-chat UI state, all in memory: a restart loses which project a
        # half-finished /run was aimed at, which is the right thing to lose.
        self._pending: dict[int, dict] = {}
        self._continuing: dict[int, str] = {}
        self._choices: dict[int, list[dict]] = {}
        self._watch: dict[int, set[str]] = {}
        self._announced: dict[str, str] = {}

    # -- access --------------------------------------------------------------

    def _permitted(self, chat_id: int) -> bool:
        return str(chat_id) in self.allowed

    def _refuse(self, chat_id: int) -> None:
        self._send(
            chat_id,
            "This bot is not configured for this chat yet.\n\n"
            f"Its id is <code>{chat_id}</code>. Add it to <code>.env</code> and restart:\n\n"
            f"<code>HARNESS2_TELEGRAM_CHAT_ID={chat_id}</code>",
        )

    # -- transport helpers ---------------------------------------------------

    def _send(self, chat_id: int, text: str, keyboard: dict | None = None) -> None:
        for chunk in _chunks(text):
            params = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            }
            if keyboard is not None:
                params["reply_markup"] = keyboard
            try:
                self.tg.call("sendMessage", **params)
            except (TelegramError, OSError) as exc:
                # A refusal is usually bad HTML, which would drop the whole
                # message. Retrying as plain text loses the <pre> blocks but
                # still says the thing.
                self.log(f"sendMessage failed ({exc}); retrying unformatted")
                params["text"] = html.unescape(_strip_tags(chunk))
                params.pop("parse_mode", None)
                try:
                    self.tg.call("sendMessage", **params)
                except (TelegramError, OSError) as again:
                    self.log(f"could not send to {chat_id}: {again}")
                    return

    def _buttons(self, chat_id: int, text: str, rows: list[list[tuple[str, str]]]) -> None:
        keyboard = {
            "inline_keyboard": [
                [{"text": label, "callback_data": data} for label, data in row] for row in rows
            ]
        }
        self._send(chat_id, text, keyboard)

    # -- loop ----------------------------------------------------------------

    def _register_commands(self) -> None:
        """Put the command menu in the client, so it is this build's list."""
        try:
            self.tg.call(
                "setMyCommands",
                commands=[{"command": c, "description": d} for c, d in COMMANDS],
                scope={"type": "default"},
            )
        except (TelegramError, OSError) as exc:
            self.log(f"could not set the command menu: {exc}")

    def run(self, *, once: bool = False) -> None:
        self._register_commands()
        me = self.tg.call("getMe")
        self.log(f"@{me.get('username')} polling via {self.tg.proxy} -> {self.panel.base}")
        if not self.allowed:
            self.log("no HARNESS2_TELEGRAM_CHAT_ID set: only /whoami will answer")
        while not self.stopping:
            try:
                updates = self.tg.call(
                    "getUpdates",
                    offset=self.offset,
                    timeout=self.poll_timeout,
                    allowed_updates=["message", "callback_query"],
                )
            except (TelegramError, OSError) as exc:
                self.log(f"getUpdates failed: {exc}")
                time.sleep(3)
                if once:
                    return
                continue
            for update in updates or []:
                self.offset = update["update_id"] + 1
                try:
                    self._dispatch(update)
                except (TelegramError, OSError, PanelError) as exc:
                    self.log(f"update {update.get('update_id')} failed: {exc}")
            self._notify()
            if once:
                return

    def _notify(self) -> None:
        """Tell a chat when a task it started has settled."""
        for chat_id, job_ids in list(self._watch.items()):
            for job_id in list(job_ids):
                job = self.panel.task(job_id)
                if job is None:
                    job_ids.discard(job_id)
                    continue
                state = job.get("state")
                if state not in SETTLED or self._announced.get(job_id) == state:
                    continue
                job_ids.discard(job_id)
                self._announced[job_id] = state
                self._send(chat_id, f"<b>{_short(job.get('project'), 40)}</b> — {state}\n" + self._summary(job))

    # -- dispatch ------------------------------------------------------------

    def _dispatch(self, update: dict) -> None:
        if "callback_query" in update:
            return self._on_callback(update["callback_query"])
        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        chat = (message.get("chat") or {}).get("id")
        if chat is None or not text:
            return
        chat_id = int(chat)
        if not self._permitted(chat_id):
            return self._refuse(chat_id)
        if text.startswith("/"):
            head, _, rest = text[1:].partition(" ")
            # A command typed in a group arrives as /cmd@this_bot.
            command = head.split("@", 1)[0].lower()
            handler = getattr(self, f"_cmd_{command}", None)
            if handler is None:
                return self._send(chat_id, f"no such command: /{command}\n\n{HELP}")
            return handler(chat_id, rest.strip())
        return self._as_task(chat_id, text)

    # The one-letter kinds the buttons encode (`t:` for a task row, `c:` for
    # cancel, and so on), mapped to the handlers. Separate from the command
    # names because callback data shares the 64-byte budget with the label.
    _CALLBACK_HANDLERS = {
        "run": "_cb_run",
        "t": "_cb_task",
        "l": "_cb_log",
        "a": "_cb_answer",
        "c": "_cb_cancel",
        "f": "_cb_forget",
        "k": "_cb_continue",
    }

    def _on_callback(self, query: dict) -> None:
        chat_id = int((query.get("message") or {}).get("chat", {}).get("id") or 0)
        data = query.get("data") or ""
        try:
            self.tg.call("answerCallbackQuery", callback_query_id=query.get("id"))
        except (TelegramError, OSError) as exc:
            self.log(f"answerCallbackQuery failed: {exc}")
        if not self._permitted(chat_id):
            return self._refuse(chat_id)
        kind, _, arg = data.partition(":")
        name = self._CALLBACK_HANDLERS.get(kind)
        if name is None:
            return
        getattr(self, name)(chat_id, arg)

    # -- plain text is a task ------------------------------------------------

    def _as_task(self, chat_id: int, text: str) -> None:
        parent = self._continuing.pop(chat_id, "")
        if parent:
            return self._submit(chat_id, text, continue_from=parent)
        self._pending[chat_id] = {"task": text, "harness": ""}
        self._ask_project(chat_id)

    def _ask_project(self, chat_id: int) -> None:
        try:
            projects = self.panel.projects()
        except PanelError as exc:
            return self._send(chat_id, f"⚠️ {exc}")
        if not projects:
            self._pending.pop(chat_id, None)
            return self._send(chat_id, "No project directories to target.")
        # Kept because the button carries only an index: callback data is capped
        # at 64 bytes and a project name does not have to fit.
        self._choices[chat_id] = projects
        rows = []
        for index, project in enumerate(projects[:8]):
            mark = " ·git" if project.get("git") else ""
            rows.append([(f"{_short(project['name'], 40)}{mark}", f"run:{index}")])
        task = self._pending.get(chat_id, {}).get("task", "")
        self._buttons(
            chat_id,
            f"Run <b>{html.escape(_short(task, 120))}</b>\n\nwhere?",
            rows + [[("✖️ Cancel", "run:-1")]],
        )

    def _submit(self, chat_id: int, task: str, *, project: str = "", harness: str = "", continue_from: str = "") -> None:
        try:
            job = self.panel.submit(
                task, project=project, harness=harness, continue_from=continue_from
            )
        except PanelError as exc:
            return self._send(chat_id, f"⚠️ {exc}")
        self._watch.setdefault(chat_id, set()).add(job["id"])
        self._send(
            chat_id,
            f"▶️ <code>{job['id']}</code> — <b>{html.escape(_short(job['project'], 60))}</b>\n"
            f"{html.escape(job['harness'])} · {html.escape(job.get('model') or '—')}\n"
            f"{html.escape(job.get('routed_by') or '')}",
            {"inline_keyboard": [[{"text": "Log", "callback_data": f"l:{job['id']}"},
                                  {"text": "Cancel", "callback_data": f"c:{job['id']}"}]]},
        )

    # -- callbacks -----------------------------------------------------------

    def _cb_run(self, chat_id: int, arg: str) -> None:
        pending = self._pending.pop(chat_id, None)
        if pending is None:
            return self._send(chat_id, "That picker has expired; send the task again.")
        try:
            index = int(arg)
        except ValueError:
            return
        if index < 0:
            return self._send(chat_id, "Dropped.")
        choices = self._choices.get(chat_id) or []
        if index >= len(choices):
            return self._send(chat_id, "That project is no longer in the list.")
        self._submit(
            chat_id, pending["task"], project=choices[index]["name"], harness=pending.get("harness", "")
        )

    def _cb_task(self, chat_id: int, job_id: str) -> None:
        job = self.panel.task(job_id)
        if job is None:
            return self._send(chat_id, f"No task {job_id}.")
        self._send(chat_id, self._detail(job), self._task_buttons(job))

    def _cb_log(self, chat_id: int, job_id: str) -> None:
        self._send(chat_id, self._log_text(job_id))

    def _cb_answer(self, chat_id: int, job_id: str) -> None:
        job = self.panel.task(job_id)
        if job is None:
            return self._send(chat_id, f"No task {job_id}.")
        answer = (job.get("answer") or "").strip()
        self._send(chat_id, html.escape(answer) if answer else "The agent said nothing.")

    def _cb_cancel(self, chat_id: int, job_id: str) -> None:
        stopped = False
        try:
            stopped = self.panel.cancel(job_id)
        except PanelError as exc:
            return self._send(chat_id, f"⚠️ {exc}")
        self._send(chat_id, f"Stopped {job_id}." if stopped else f"{job_id} was not running.")

    def _cb_forget(self, chat_id: int, job_id: str) -> None:
        gone = False
        try:
            gone = self.panel.forget(job_id)
        except PanelError as exc:
            return self._send(chat_id, f"⚠️ {exc}")
        self._send(chat_id, f"Dropped {job_id}." if gone else f"{job_id} is still running.")

    def _cb_continue(self, chat_id: int, job_id: str) -> None:
        self._continuing[chat_id] = job_id
        self._send(chat_id, f"Follow-up to {job_id}: send it as a message.")

    # -- commands ------------------------------------------------------------

    def _cmd_start(self, chat_id: int, _arg: str) -> None:
        self._send(chat_id, HELP, MENU)

    def _cmd_help(self, chat_id: int, _arg: str) -> None:
        self._send(chat_id, HELP, MENU)

    def _cmd_whoami(self, chat_id: int, _arg: str) -> None:
        # Not gated here: `_dispatch` has already refused an unaudited chat, and
        # that refusal message is what tells the operator this chat's id.
        self._send(chat_id, f"This chat's id is <code>{chat_id}</code> — allowed.")

    def _cmd_projects(self, chat_id: int, _arg: str) -> None:
        try:
            projects = self.panel.projects()
        except PanelError as exc:
            return self._send(chat_id, f"⚠️ {exc}")
        if not projects:
            return self._send(chat_id, "No project directories.")
        lines = [
            f"• <b>{html.escape(p['name'])}</b>{' ·git' if p.get('git') else ''} — {p.get('mtime','')}"
            for p in projects
        ]
        self._send(chat_id, f"<b>{len(projects)} projects</b>\n" + "\n".join(lines))

    def _cmd_harnesses(self, chat_id: int, _arg: str) -> None:
        try:
            specs = self.panel.harnesses()
        except PanelError as exc:
            return self._send(chat_id, f"⚠️ {exc}")
        lines = [
            f"• <code>{html.escape(s['id'])}</code> — {html.escape(s.get('label') or '')}"
            f"{' (default)' if s.get('default') else ''}"
            for s in specs
        ]
        self._send(chat_id, "<b>Harnesses</b>\n" + "\n".join(lines) + "\n\n<b>auto</b> routes by the task text.")

    def _cmd_status(self, chat_id: int, _arg: str) -> None:
        try:
            tasks = self.panel.tasks()
        except PanelError as exc:
            return self._send(chat_id, f"⚠️ {exc}")
        live = [t for t in tasks if t.get("state") in ("running", "queued")]
        lines = [
            f"panel: <code>{html.escape(self.panel.base)}</code> ok",
            f"proxy: <code>{html.escape(self.tg.proxy)}</code>",
            f"tasks: {len(tasks)} known · {len(live)} running",
        ]
        for t in live:
            lines.append(f"  ▶️ <code>{t['id']}</code> {html.escape(_short(t.get('project'), 24))} — {html.escape(_short(t.get('task'), 60))}")
        self._send(chat_id, "\n".join(lines))

    def _cmd_tasks(self, chat_id: int, arg: str) -> None:
        try:
            tasks = self.panel.tasks()
        except PanelError as exc:
            return self._send(chat_id, f"⚠️ {exc}")
        if arg.isdigit():
            tasks = tasks[: int(arg)]
        if not tasks:
            return self._send(chat_id, "No tasks yet. Send one, or /run <task>.")
        rows, lines = [], []
        for job in tasks[:10]:
            icon = {"done": "✅", "failed": "❌", "running": "▶️", "queued": "⏳"}.get(job.get("state"), "⚪️")
            lines.append(
                f"{icon} <code>{job['id']}</code> <b>{html.escape(_short(job.get('project'), 24))}</b>\n"
                f"    {html.escape(_short(job.get('task'), 70))}"
            )
            rows.append([(f"{icon} {_short(job.get('task'), 30)}", f"t:{job['id']}")])
        self._send(chat_id, "<b>Recent tasks</b>\n" + "\n".join(lines), {"inline_keyboard": rows})

    def _cmd_task(self, chat_id: int, arg: str) -> None:
        job_id = arg.split()[0] if arg else ""
        if not job_id:
            return self._send(chat_id, "Which task? /tasks lists them.")
        job = self.panel.task(job_id)
        if job is None:
            return self._send(chat_id, f"No task {html.escape(job_id)}.")
        self._send(chat_id, self._detail(job), self._task_buttons(job))

    def _cmd_answer(self, chat_id: int, arg: str) -> None:
        self._cb_answer(chat_id, arg.split()[0] if arg else "")

    def _cmd_log(self, chat_id: int, arg: str) -> None:
        parts = arg.split()
        if not parts:
            return self._send(chat_id, "Which task? /tasks lists them.")
        self._send(chat_id, self._log_text(parts[0], int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0))

    def _cmd_cancel(self, chat_id: int, arg: str) -> None:
        if arg:
            self._cb_cancel(chat_id, arg.split()[0])
        else:
            self._pick_running(chat_id, "cancel", settled=False)

    def _cmd_forget(self, chat_id: int, arg: str) -> None:
        if arg:
            self._cb_forget(chat_id, arg.split()[0])
        else:
            self._pick_running(chat_id, "forget", settled=True)

    def _cmd_continue(self, chat_id: int, arg: str) -> None:
        job_id, _, follow_up = arg.partition(" ")
        if not job_id:
            return self._pick_running(chat_id, "continue", settled=True)
        # The label says a picker picks an unfinished task, so that is what it
        # shows -- and what this guard is for.
        job = self.panel.task(job_id)
        if job is not None and job.get("state") in ("running", "queued"):
            return self._send(chat_id, f"⏳ {html.escape(job_id)} is still running.")
        if follow_up.strip():
            return self._submit(chat_id, follow_up.strip(), continue_from=job_id)
        self._cb_continue(chat_id, job_id)

    def _cmd_run(self, chat_id: int, arg: str) -> None:
        if arg:
            return self._as_task(chat_id, arg)
        if chat_id in self._pending:
            self._ask_project(chat_id)  # a task is already waiting for a project
        else:
            self._send(chat_id, "Send me the task, then I will ask which project.")

    # -- shared views --------------------------------------------------------

    def _pick_running(self, chat_id: int, what: str, *, settled: bool = False) -> None:
        try:
            tasks = self.panel.tasks()
        except PanelError as exc:
            return self._send(chat_id, f"⚠️ {exc}")
        live = [t for t in tasks if t.get("state") not in ("running", "queued")]
        if not settled:
            live = [t for t in tasks if t.get("state") in ("running", "queued")]
        if not live:
            return self._send(chat_id, "Nothing to pick.")
        label, verb = {"cancel": ("Cancel", "c"), "forget": ("Forget", "f"), "continue": ("Continue", "k")}[what]
        rows = [[(_short(t.get("task"), 40), f"{verb}:{t['id']}")] for t in live[:10]]
        self._send(chat_id, f"{label} which?", {"inline_keyboard": rows})

    def _task_buttons(self, job: dict) -> dict:
        job_id = job["id"]
        top = [
            {"text": "Log", "callback_data": f"l:{job_id}"},
            {"text": "Answer", "callback_data": f"a:{job_id}"},
        ]
        if job.get("state") in ("running", "queued"):
            bottom = [{"text": "⛔️ Cancel", "callback_data": f"c:{job_id}"}]
        else:
            bottom = [
                {"text": "↩️ Continue", "callback_data": f"k:{job_id}"},
                {"text": "🗑 Forget", "callback_data": f"f:{job_id}"},
            ]
        return {"inline_keyboard": [top, bottom]}

    def _detail(self, job: dict) -> str:
        delivered = job.get("delivered") or []
        rows = [
            ("project", job.get("project")),
            ("workspace", job.get("workspace") + (" (clone)" if job.get("cloned") else "")),
            ("harness", job.get("harness")),
            ("model", job.get("model")),
            ("routed by", job.get("routed_by")),
            ("state", f"{job.get('state')}" + (f" (exit {job['exit_code']})" if job.get("exit_code") is not None else "")),
            ("started", job.get("started")),
            ("finished", job.get("finished")),
        ]
        if job.get("continues"):
            rows.append(("continues", job["continues"]))
        if job.get("error"):
            rows.append(("error", job["error"]))
        body = [f"<b>{html.escape(job.get('project') or '')}</b> <code>{job['id']}</code>", "<pre>" + html.escape(job.get("task") or "—") + "</pre>"]
        body += [f"<b>{k}</b>: {html.escape(_short(v, 200))}" for k, v in rows if v]
        if job.get("answer"):
            body.append("<b>answer</b>\n<pre>" + html.escape(_short(job["answer"], 1200)) + "</pre>")
        if delivered:
            body.append("<b>delivered</b>\n" + "\n".join(f"• {html.escape(f)}" for f in delivered[:20]))
        return "\n".join(body)

    def _summary(self, job: dict) -> str:
        bits = [f"<code>{job['id']}</code>"]
        if job.get("answer"):
            bits.append(html.escape(_short(job["answer"], 300)))
        if job.get("delivered"):
            bits.append("delivered: " + html.escape(", ".join(job["delivered"][:5])))
        if job.get("error"):
            bits.append("⚠️ " + html.escape(_short(job["error"], 300)))
        bits.append(f"tap for detail: /task {job['id']}")
        return "\n".join(bits)

    def _log_text(self, job_id: str, lines: int = 0) -> str:
        try:
            log = self.panel.log(job_id)
        except PanelError as exc:
            return f"⚠️ {exc}"
        if not log.strip():
            return f"No output captured yet for {html.escape(job_id)}."
        if lines > 0:
            log = "\n".join(log.splitlines()[-lines:])
        return f"<b>{html.escape(job_id)}</b>\n<pre>" + html.escape(log[-LIMIT + 200 :]) + "</pre>"

    # -- shutdown ------------------------------------------------------------

    def stop(self) -> None:
        self.stopping = True


def _strip_tags(text: str) -> str:
    return re.sub(r"</?[a-zA-Z][^>]*>", "", text)
