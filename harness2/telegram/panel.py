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

"""A client for the task panel's JSON API.

The bot is deliberately not a second task engine. `harness2.cli.web` already
owns the whole of it -- background threads so several tasks run at once, a
`git clone --shared` per git project so a run cannot touch the real tree, the
list persisted so a restart keeps it — and all of that is reachable over HTTP.
Re-implementing any of it here would be a second copy to keep in step, and the
one place a task's isolation could be lost.

So every command below is a thin call, and `harness2-web` has to be running.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class PanelError(RuntimeError):
    """The panel answered with an error, or is not answering at all."""


class Panel:
    def __init__(self, base: str, *, timeout: float = 30.0):
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _request(self, path: str, *, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method="POST" if data is not None else "GET",
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            # The panel reports a refused task as a 4xx with a JSON `error`; that
            # message is what the operator needs, not "HTTP Error 400".
            try:
                detail = json.loads(exc.read() or b"{}").get("error")
            except (ValueError, OSError):
                detail = None
            raise PanelError(detail or f"{exc.code} from {path}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise PanelError(
                f"cannot reach the panel at {self.base} ({exc}); "
                "is harness2-web running?"
            ) from exc

    # -- reading -------------------------------------------------------------

    def projects(self) -> list[dict]:
        return self._request("/api/projects")

    def harnesses(self) -> list[dict]:
        return self._request("/api/harnesses")

    def tasks(self) -> list[dict]:
        return self._request("/api/tasks")

    def task(self, job_id: str) -> dict | None:
        try:
            return self._request(f"/api/tasks/{job_id}")
        except PanelError:
            return None

    def log(self, job_id: str) -> str:
        return self._request(f"/api/tasks/{job_id}/log") or ""

    # -- writing -------------------------------------------------------------

    def submit(
        self,
        task: str,
        *,
        project: str = "",
        harness: str = "",
        continue_from: str = "",
    ) -> dict:
        body = {"task": task, "project": project, "harness": harness, "continue_from": continue_from}
        return self._request("/api/tasks", body=body)

    def cancel(self, job_id: str) -> bool:
        return bool(self._request(f"/api/tasks/{job_id}/cancel", body={}).get("cancelled"))

    def forget(self, job_id: str) -> bool:
        return bool(self._request(f"/api/tasks/{job_id}/forget", body={}).get("forgotten"))
