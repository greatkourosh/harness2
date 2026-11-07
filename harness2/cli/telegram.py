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

"""Run the Telegram bot in front of the task panel.

    harness2-telegram                       # panel on 127.0.0.1:8090
    harness2-telegram --panel http://127.0.0.1:9000
    harness2-telegram --once                # one poll, then exit (smoke test)

Reads `TELEGRAM_BOT_TOKEN` and the `SOCKS5_*` entries from `.env`. The chat the
bot answers is `HARNESS2_TELEGRAM_CHAT_ID`; with none set it only replies to
`/whoami`, which is how that id is obtained. This host cannot reach
`api.telegram.org` directly, so a proxy is required, not optional.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys

from .. import config
from ..telegram.bot import Bot
from ..telegram.panel import Panel
from ..telegram.transport import Telegram

# Panel's own default; see cli/web.py.
DEFAULT_PANEL = "http://127.0.0.1:8090"


def proxies_from_env() -> list[str]:
    """Every `SOCKS5_*` value, in the order their names sort.

    Numbered names are the point: the list is an ordered failover, and `sorted`
    on the key keeps `SOCKS5_10` after `SOCKS5_09` rather than after `SOCKS5_01`.
    """
    keys = sorted(k for k in os.environ if k.startswith("SOCKS5_"))
    return [os.environ[k].strip() for k in keys if os.environ.get(k, "").strip()]


def _log(text: str) -> None:
    print(f"harness2-telegram: {text}", file=sys.stderr, flush=True)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="harness2-telegram", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--panel", default=config.env("HARNESS2_PANEL_URL", DEFAULT_PANEL),
                   help="base URL of the running harness2-web")
    p.add_argument("--poll-timeout", type=int, default=10,
                   help="seconds a getUpdates long poll may wait")
    p.add_argument("--once", action="store_true",
                   help="handle the updates already queued, then exit")
    p.add_argument("--allow", action="append", default=None, metavar="CHAT_ID",
                   help="chat id to allow; repeatable (default: HARNESS2_TELEGRAM_CHAT_ID)")
    args = p.parse_args(argv)

    try:
        token = config.require("TELEGRAM_BOT_TOKEN", needed_by="the Telegram bot")
    except config.ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1

    proxies = proxies_from_env()
    if not proxies:
        _log("no SOCKS5_* entries in .env; trying a direct connection, which this host blocks")

    allowed = args.allow or [c.strip() for c in config.env("HARNESS2_TELEGRAM_CHAT_ID", "").split(",")]
    allowed = [c for c in allowed if c]

    telegram = Telegram(token, proxies, timeout=max(40.0, args.poll_timeout + 30), log=_log)
    bot = Bot(
        telegram,
        Panel(args.panel, timeout=60.0),
        allowed_chats=allowed,
        poll_timeout=args.poll_timeout,
        log=_log,
    )

    # systemd stops a unit with SIGTERM; the long poll is holding a socket, and
    # ending the loop between polls is what lets the process exit on time.
    signal.signal(signal.SIGTERM, lambda *_: bot.stop())
    signal.signal(signal.SIGINT, lambda *_: bot.stop())

    try:
        bot.run(once=args.once)
    except KeyboardInterrupt:
        _log("stopped")
        return 0
    except OSError as exc:
        print(f"could not reach Telegram: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
