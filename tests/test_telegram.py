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

"""The Telegram front end: routing, the access gate, and the proxy failover.

No network here. The two things worth testing are the decisions the bot makes
before anything is sent -- which handler a message reaches, and whether a chat
is allowed to run a task at all -- and the transport's choice of proxy.
"""

import unittest

from harness2.telegram.bot import Bot, HELP
from harness2.telegram.panel import Panel, PanelError
from harness2.telegram.transport import Telegram, parse_proxy

CHAT = 4242


class FakeTelegram:
    """Stands in for the transport; records calls instead of making them."""

    def __init__(self, *, updates=None):
        self.calls = []
        self.proxy = "fake:1080"
        self._updates = list(updates or [])

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "getMe":
            return {"username": "testbot", "id": 1}
        if method == "getUpdates":
            batch, self._updates = self._updates, []
            return batch
        return {}

    def sent(self):
        return [p for m, p in self.calls if m == "sendMessage"]

    def text(self) -> str:
        return "\n".join(p["text"] for p in self.sent())


class FakePanel:
    base = "http://127.0.0.1:8090"
    state = "done"
    projects_list = [{"name": "alpha", "git": True}, {"name": "beta", "git": False}]

    def __init__(self):
        self.submitted = []
        self.cancelled = []
        self.forgotten = []
        self.tasks_list = []

    def projects(self):
        return list(self.projects_list)

    def harnesses(self):
        return [{"id": "claude-code", "label": "claude", "default": True}]

    def tasks(self):
        return list(self.tasks_list)

    def task(self, job_id):
        return next((t for t in self.tasks_list if t["id"] == job_id), None)

    def log(self, job_id):
        return "line one\nline two"

    def submit(self, task, *, project="", harness="", continue_from=""):
        self.submitted.append(
            {"task": task, "project": project, "harness": harness, "continue_from": continue_from}
        )
        return {"id": "abc123", "project": project or "alpha", "harness": "claude-code",
                "model": "9router/All", "routed_by": "default"}

    def cancel(self, job_id):
        self.cancelled.append(job_id)
        return True

    def forget(self, job_id):
        self.forgotten.append(job_id)
        return True


def make_bot(*, allowed=(str(CHAT),), panel=None, updates=None):
    tg = FakeTelegram(updates=updates)
    bot = Bot(tg, panel or FakePanel(), allowed_chats=list(allowed))
    return bot, tg


def message(text, chat=CHAT):
    return {"update_id": 1, "message": {"chat": {"id": chat}, "text": text}}


def callback(data, chat=CHAT):
    return {"update_id": 2,
            "callback_query": {"id": "cb", "data": data,
                               "message": {"chat": {"id": chat}}}}


class AccessTests(unittest.TestCase):
    def test_unknown_chat_is_refused_with_its_id(self):
        """The refusal has to carry the id: it is how the allowlist gets set."""
        bot, tg = make_bot(allowed=[])
        bot._dispatch(message("hello"))
        self.assertIn(str(CHAT), tg.text())
        self.assertIn("HARNESS2_TELEGRAM_CHAT_ID", tg.text())

    def test_known_chat_is_answered(self):
        bot, tg = make_bot()
        bot._dispatch(message("/status"))
        self.assertIn("panel:", tg.text())

    def test_task_is_not_submitted_from_an_unknown_chat(self):
        panel = FakePanel()
        bot, _ = make_bot(allowed=[], panel=panel)
        bot._dispatch(message("delete everything"))
        self.assertEqual(panel.submitted, [])

    def test_allowlist_accepts_several_ids(self):
        bot, _ = make_bot(allowed=["1", "2"])
        self.assertTrue(bot._permitted(1))
        self.assertTrue(bot._permitted(2))
        self.assertFalse(bot._permitted(3))


class RoutingTests(unittest.TestCase):
    def test_command_with_bot_suffix_reaches_its_handler(self):
        """In a group Telegram sends /status@this_bot, which must still route."""
        bot, tg = make_bot()
        bot._dispatch(message("/projects@testbot"))
        self.assertIn("alpha", tg.text())

    def test_unknown_command_lists_the_commands(self):
        bot, tg = make_bot()
        bot._dispatch(message("/nope"))
        self.assertIn("no such command", tg.text())

    def test_plain_text_becomes_a_task_and_asks_for_a_project(self):
        panel = FakePanel()
        bot, tg = make_bot(panel=panel)
        bot._dispatch(message("fix the build"))
        self.assertEqual(panel.submitted, [])
        self.assertIn("where?", tg.text())
        self.assertEqual(bot._pending[CHAT]["task"], "fix the build")

    def test_project_button_submits_the_pending_task(self):
        panel = FakePanel()
        bot, _ = make_bot(panel=panel)
        bot._dispatch(message("fix the build"))
        bot._dispatch(callback("run:1"))
        self.assertEqual(panel.submitted, [
            {"task": "fix the build", "project": "beta", "harness": "", "continue_from": ""}
        ])

    def test_cancelled_picker_submits_nothing(self):
        panel = FakePanel()
        bot, tg = make_bot(panel=panel)
        bot._dispatch(message("fix the build"))
        bot._dispatch(callback("run:-1"))
        self.assertEqual(panel.submitted, [])
        self.assertIn("Dropped", tg.text())

    def test_project_index_out_of_range_does_not_submit(self):
        panel = FakePanel()
        bot, tg = make_bot(panel=panel)
        bot._dispatch(message("fix the build"))
        panel.projects_list = [{"name": "alpha", "git": True}]
        bot._dispatch(callback("run:7"))
        self.assertEqual(panel.submitted, [])
        self.assertIn("no longer in the list", tg.text())

    def test_run_with_an_argument_still_asks_which_project(self):
        bot, tg = make_bot()
        bot._dispatch(message("/run tidy the README"))
        self.assertEqual(bot._pending[CHAT]["task"], "tidy the README")
        self.assertIn("where?", tg.text())

    def test_continue_sets_the_parent_for_the_next_message(self):
        panel = FakePanel()
        panel.tasks_list = [{"id": "abc123", "state": "done", "project": "alpha", "task": "one"}]
        bot, tg = make_bot(panel=panel)
        bot._dispatch(message("/continue abc123"))
        self.assertIn("Follow-up to abc123", tg.text())
        bot._dispatch(message("now do step 2"))
        self.assertEqual(len(panel.submitted), 1)
        self.assertEqual(panel.submitted[0]["continue_from"], "abc123")
        # The parent is consumed: the next plain message is a fresh task again.
        bot._dispatch(message("something else"))
        self.assertEqual(bot._pending[CHAT]["task"], "something else")

    def test_continue_with_an_inline_follow_up_submits_at_once(self):
        panel = FakePanel()
        bot, _ = make_bot(panel=panel)
        bot._dispatch(message("/continue abc123 also update the docs"))
        self.assertEqual(panel.submitted, [
            {"task": "also update the docs", "project": "", "harness": "", "continue_from": "abc123"}
        ])

    def test_task_reports_a_missing_id(self):
        bot, tg = make_bot()
        bot._dispatch(message("/task nosuch"))
        self.assertIn("No task nosuch", tg.text())

    def test_cancel_button_reaches_the_panel(self):
        panel = FakePanel()
        bot, _ = make_bot(panel=panel)
        bot._dispatch(callback("c:abc123"))
        self.assertEqual(panel.cancelled, ["abc123"])

    def test_forget_button_reaches_the_panel(self):
        panel = FakePanel()
        bot, _ = make_bot(panel=panel)
        bot._dispatch(callback("f:abc123"))
        self.assertEqual(panel.forgotten, ["abc123"])

    def test_log_is_rendered_into_a_pre_block(self):
        bot, tg = make_bot()
        bot._dispatch(message("/log abc123"))
        self.assertIn("<pre>", tg.text())
        self.assertIn("line two", tg.text())

    def test_whoami_from_an_unknown_chat_is_refused_with_the_remediation(self):
        """An unaudited chat gets its id AND the env line that admits it."""
        bot, tg = make_bot(allowed=[])
        bot._dispatch(message("/whoami"))
        self.assertIn(str(CHAT), tg.text())
        self.assertIn("HARNESS2_TELEGRAM_CHAT_ID", tg.text())

    def test_whoami_from_a_known_chat_reports_allowed(self):
        bot, tg = make_bot()
        bot._dispatch(message("/whoami"))
        self.assertIn("allowed", tg.text())

    def test_panel_down_is_reported_not_raised(self):
        class Down(FakePanel):
            def projects(self):
                raise PanelError("cannot reach the panel")

        bot, tg = make_bot(panel=Down())
        bot._dispatch(message("/projects"))
        self.assertIn("cannot reach the panel", tg.text())


class NotifyTests(unittest.TestCase):
    def test_a_settled_watched_task_is_announced_once(self):
        panel = FakePanel()
        panel.tasks_list = [{"id": "abc123", "state": "running", "project": "alpha",
                             "task": "t", "answer": "", "delivered": [], "error": ""}]
        bot, tg = make_bot(panel=panel)
        bot._watch[CHAT] = {"abc123"}
        bot._notify()
        self.assertEqual(tg.sent(), [])

        panel.tasks_list[0]["state"] = "done"
        panel.tasks_list[0]["answer"] = "all good"
        bot._notify()
        self.assertIn("all good", tg.text())
        self.assertEqual(bot._watch[CHAT], set())

        # A second pass must not repeat it.
        before = len(tg.sent())
        bot._notify()
        self.assertEqual(len(tg.sent()), before)

    def test_a_task_that_vanished_stops_being_watched(self):
        bot, _ = make_bot()
        bot._watch[CHAT] = {"gone"}
        bot._notify()
        self.assertEqual(bot._watch[CHAT], set())


class MenuTests(unittest.TestCase):
    def test_run_registers_the_command_menu(self):
        bot, tg = make_bot()
        bot.run(once=True)
        registered = [p for m, p in tg.calls if m == "setMyCommands"]
        self.assertEqual(len(registered), 1)
        names = [c["command"] for c in registered[0]["commands"]]
        self.assertIn("run", names)
        self.assertIn("status", names)

    def test_help_and_menu_agree(self):
        """A command in HELP that is not registered (or the reverse) is a bug
        that only shows up as a menu item the bot then refuses."""
        from harness2.telegram.bot import COMMANDS

        for name, _ in COMMANDS:
            self.assertIn(f"/{name}", HELP, f"/{name} is registered but not in HELP")
        self.assertIn("/help", HELP)


class TransportTests(unittest.TestCase):
    def test_parse_proxy_reads_host_and_port(self):
        self.assertEqual(parse_proxy("192.168.1.13:10810"), ("192.168.1.13", 10810))
        self.assertEqual(parse_proxy("127.0.0.1:9150"), ("127.0.0.1", 9150))

    def test_parse_proxy_rejects_malformed_entries(self):
        for bad in ("", "no-port", ":1080", "host:notaport"):
            self.assertIsNone(parse_proxy(bad), bad)

    def test_malformed_entries_are_dropped_not_fatal(self):
        tg = Telegram("token", ["good:1080", "junk", ":9"])
        self.assertEqual(tg.proxies, [("good", 1080)])

    def test_failover_moves_to_the_next_proxy_and_remembers_it(self):
        tg = Telegram("token", ["bad:1", "good:2"])
        tried = []

        def fake(address, method, params):
            tried.append(address)
            if address[0] == "bad":
                raise OSError("refused")
            return {"ok": True}

        tg._call_once = fake
        tg.call("getMe")
        self.assertEqual(tried, [("bad", 1), ("good", 2)])
        self.assertEqual(tg._preferred, 1)
        self.assertEqual(tg.proxy, "good:2")

        # The next call starts from the remembered one, not from the top.
        tried.clear()
        tg.call("getMe")
        self.assertEqual(tried, [("good", 2)])

    def test_a_refusal_from_telegram_does_not_rotate_proxies(self):
        """`ok: false` is an answer. Five more proxies would not change it."""
        from harness2.telegram.transport import TelegramError

        tg = Telegram("token", ["a:1", "b:2"])
        tried = []

        def fake(address, method, params):
            tried.append(address)
            raise TelegramError("Bad Request: message is too long")

        tg._call_once = fake
        with self.assertRaises(TelegramError):
            tg.call("sendMessage")
        self.assertEqual(len(tried), 1)

    def test_every_proxy_failing_is_an_oserror(self):
        tg = Telegram("token", ["a:1", "b:2"])

        def fake(address, method, params):
            raise OSError("down")

        tg._call_once = fake
        with self.assertRaises(OSError):
            tg.call("getMe")


class PanelClientTests(unittest.TestCase):
    def test_an_unreachable_panel_says_so(self):
        """The most likely first failure is the panel not running at all."""
        panel = Panel("http://127.0.0.1:9", timeout=1)
        with self.assertRaises(PanelError) as ctx:
            panel.tasks()
        self.assertIn("harness2-web", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
