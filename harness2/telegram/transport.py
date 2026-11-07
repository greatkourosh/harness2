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

"""Telegram Bot API over a SOCKS5 proxy, on the standard library alone.

`pyproject.toml` declares no runtime dependencies, so this is built from
`socket`, `ssl` and `http.client` rather than on an HTTP library. Telegram is
not reachable from this host directly — a plain request to `api.telegram.org`
simply times out — so every call is tunnelled: a SOCKS5 CONNECT (RFC 1928) to
one of the proxies in `.env`, then TLS over that socket.

Only SOCKS5 is implemented. Every `SOCKS5_*` entry speaks it. A proxy that
speaks only HTTP CONNECT would need a `CONNECT host:port` exchange here instead
of the handshake below, which is the one change to make if such an entry is
ever added.
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl

HOST = "api.telegram.org"
PORT = 443

_TLS = ssl.create_default_context()


class TelegramError(RuntimeError):
    """Telegram answered and refused. Not a transport failure, so no failover."""


def parse_proxy(raw: str) -> tuple[str, int] | None:
    """`host:port` as `.env` writes it, or None when the entry is malformed."""
    host, sep, port = raw.rpartition(":")
    if not sep or not host:
        return None
    try:
        return host, int(port)
    except ValueError:
        return None


def _read_exact(sock: socket.socket, n: int) -> bytes:
    """`recv` can return a short read, which would put TLS off its byte offset."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("proxy closed the connection mid-handshake")
        buf += chunk
    return buf


def _socks5(address: tuple[str, int], host: str, port: int, timeout: float) -> socket.socket:
    """A TCP socket to `host:port`, opened by way of a SOCKS5 proxy."""
    sock = socket.create_connection(address, timeout=timeout)
    sock.settimeout(timeout)
    try:
        sock.sendall(b"\x05\x01\x00")  # version 5, one method offered: no auth
        if _read_exact(sock, 2) != b"\x05\x00":
            raise OSError("proxy did not accept the no-auth SOCKS5 method")
        # ATYP 3: let the proxy resolve the name, which is what --socks5-hostname
        # does on the command line and keeps DNS off this host, where Telegram's
        # names are not resolvable anyway.
        name = host.encode()
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + port.to_bytes(2, "big"))
        head = _read_exact(sock, 4)
        if head[1] != 0:
            raise OSError(f"proxy refused CONNECT (SOCKS5 reply {head[1]})")
        # The reply carries the bound address; it has to be consumed or TLS
        # starts reading into the middle of it.
        if head[3] == 1:
            _read_exact(sock, 4)
        elif head[3] == 4:
            _read_exact(sock, 16)
        else:
            _read_exact(sock, _read_exact(sock, 1)[0])
        _read_exact(sock, 2)
        return sock
    except BaseException:
        sock.close()
        raise


class Telegram:
    """Bot API calls, failing over across the proxies until one carries them.

    The working proxy is remembered, so the failover is paid once rather than on
    every call. Only transport failures rotate: a refusal from Telegram itself
    (`ok: false`) is an answer, and asking five more proxies would not change it.
    """

    def __init__(self, token: str, proxies: list[str], *, timeout: float = 40.0, log=None):
        self.token = token
        self.timeout = timeout
        self.log = log or (lambda *_: None)
        self.proxies: list[tuple[str, int]] = []
        for raw in proxies:
            address = parse_proxy(raw)
            if address is None:
                self.log(f"ignoring malformed proxy entry {raw!r}")
            else:
                self.proxies.append(address)
        self._preferred = 0

    @property
    def proxy(self) -> str:
        """The entry in use, for `/status`."""
        if not self.proxies:
            return "(direct)"
        host, port = self.proxies[self._preferred]
        return f"{host}:{port}"

    def call(self, method: str, **params):
        """`method` with `params` as its JSON body; returns the `result` field."""
        # Long polling holds the socket open for `timeout` seconds by design, so
        # the transport timeout has to sit above the poll's own.
        attempts = len(self.proxies) or 1
        failure: Exception | None = None
        for step in range(attempts):
            index = (self._preferred + step) % attempts
            try:
                result = self._call_once(self.proxies[index] if self.proxies else None, method, params)
            except TelegramError:
                raise
            except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
                failure = exc
                self.log(f"{method}: attempt {step + 1}/{attempts} failed: {exc}")
                continue
            if index != self._preferred:
                self.log(f"{method}: moved to {self.proxy}")
            self._preferred = index
            return result
        raise OSError(f"no proxy carried {method}: {failure}") from failure

    def _call_once(self, address: tuple[str, int] | None, method: str, params: dict):
        raw = (
            _socks5(address, HOST, PORT, self.timeout)
            if address
            else socket.create_connection((HOST, PORT), timeout=self.timeout)
        )
        with raw, _TLS.wrap_socket(raw, server_hostname=HOST) as sock:
            body = json.dumps(params).encode()
            # `Connection: close` keeps this to one request per socket, which is
            # what makes rotating proxies between calls safe. The response may
            # still be chunked, so HTTPResponse parses it rather than the raw
            # read being split by hand.
            sock.sendall(
                f"POST /bot{self.token}/{method} HTTP/1.1\r\n"
                f"Host: {HOST}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Connection: close\r\n\r\n".encode()
                + body
            )
            response = http.client.HTTPResponse(sock)
            response.begin()
            payload = json.loads(response.read() or b"{}")
        if not payload.get("ok"):
            raise TelegramError(payload.get("description") or f"{method} failed")
        return payload.get("result")
