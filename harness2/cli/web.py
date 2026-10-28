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

"""A local panel for running tasks against several projects at once.

    harness2-web            # http://127.0.0.1:8090
    harness2-web --port 9000 --host 0.0.0.0

Serves one page and a small JSON API; both come from the standard library, so
the panel adds no dependency to the package. Tasks run in background threads
(see `harness2.web.jobs`), which is what lets several be in flight while the
operator switches between them.

Binds to loopback by default. `--host 0.0.0.0` exposes it to the network, and
nothing here authenticates, so only do that on a network you trust.
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from harness2 import config
from harness2.web import jobs

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Harness2 tasks</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 :root { --bg:#0f1115; --panel:#171a21; --line:#262b36; --txt:#e6e8ee;
         --dim:#8b93a7; --ok:#3fb950; --bad:#f85149; --run:#d29922; --accent:#58a6ff; }
 * { box-sizing:border-box }
 body { margin:0; font:14px/1.5 ui-sans-serif,system-ui,sans-serif;
        background:var(--bg); color:var(--txt) }
 header { padding:12px 18px; border-bottom:1px solid var(--line);
          display:flex; align-items:center; gap:14px }
 h1 { font-size:15px; margin:0; font-weight:600 }
 #count { color:var(--dim); font-size:12px }
 main { display:grid; grid-template-columns:340px 1fr; height:calc(100vh - 49px) }
 #side { border-right:1px solid var(--line); overflow:auto }
 #detail { overflow:auto; padding:16px 20px }
 form { padding:12px 14px; border-bottom:1px solid var(--line) }
 textarea,select,input { width:100%; background:var(--panel); color:var(--txt);
   border:1px solid var(--line); border-radius:6px; padding:7px 9px; font:inherit;
   margin-bottom:7px }
 textarea { resize:vertical; min-height:64px }
 button { background:var(--accent); color:#04121f; border:0; border-radius:6px;
   padding:7px 13px; font:inherit; font-weight:600; cursor:pointer }
 button.ghost { background:transparent; color:var(--dim); border:1px solid var(--line);
   font-weight:400 }
 .row { padding:9px 14px; border-bottom:1px solid var(--line); cursor:pointer }
 .row:hover { background:var(--panel) }
 .row.sel { background:var(--panel); box-shadow:inset 3px 0 0 var(--accent) }
 .top { display:flex; gap:8px; align-items:baseline }
 .proj { font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap }
 .task { color:var(--dim); font-size:12px; overflow:hidden; text-overflow:ellipsis;
   white-space:nowrap }
 .meta { color:var(--dim); font-size:11px; margin-top:3px }
 .pill { font-size:10px; padding:1px 7px; border-radius:99px; border:1px solid var(--line);
   color:var(--dim); flex:0 0 auto }
 .s-running { color:var(--run); border-color:var(--run) }
 .s-done { color:var(--ok); border-color:var(--ok) }
 .s-failed { color:var(--bad); border-color:var(--bad) }
 pre { background:var(--panel); border:1px solid var(--line); border-radius:6px;
   padding:11px; overflow:auto; max-height:calc(100vh - 300px); font-size:12px;
   white-space:pre-wrap; word-break:break-word }
 .kv { display:grid; grid-template-columns:120px 1fr; gap:3px 12px; margin:10px 0 14px }
 .kv dt { color:var(--dim) } .kv dd { margin:0; word-break:break-all }
 th { text-align:left; color:var(--dim); font-weight:500; padding:4px 10px 4px 0;
      font-size:12px } td { padding:3px 10px 3px 0; font-size:13px }
</style></head><body>
<header><h1>Harness2 tasks</h1><span id="count"></span></header>
<main>
 <div id="side">
  <form id="new">
   <textarea id="task" placeholder="task… (routing picks the harness)"></textarea>
   <select id="project"></select>
   <select id="harness"><option value="">auto (route by task)</option></select>
   <button type="submit">Run</button>
  </form>
  <div id="list"></div>
 </div>
 <div id="detail"><p style="color:var(--dim)">Select a task.</p></div>
</main>
<script>
let selected = null, timer = null;
const esc = s => (s??'').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const sjson = async (u, o) => (await fetch(u, o)).json();

async function boot() {
  const [ps, hs] = await Promise.all([sjson('/api/projects'), sjson('/api/harnesses')]);
  document.getElementById('project').innerHTML =
    ps.map(p => `<option value="${esc(p.name)}">${esc(p.name)}${p.git?' ·git':''}</option>`).join('');
  document.getElementById('harness').innerHTML =
    '<option value="">auto (route by task)</option>' +
    hs.map(h => `<option value="${esc(h.id)}">${esc(h.id)}${h.default?' (default)':''}</option>`).join('');
  poll();
}

document.getElementById('new').onsubmit = async e => {
  e.preventDefault();
  const task = document.getElementById('task').value.trim();
  if (!task) return;
  const body = JSON.stringify({
    task,
    project: document.getElementById('project').value,
    harness: document.getElementById('harness').value,
  });
  const job = await sjson('/api/tasks', {
    method:'POST', headers:{'Content-Type':'application/json'}, body });
  document.getElementById('task').value = '';
  selected = job.id;
  poll();
};

async function poll() {
  clearTimeout(timer);
  const jobs = await sjson('/api/tasks');
  const live = jobs.filter(j => j.state === 'running' || j.state === 'queued').length;
  document.getElementById('count').textContent =
    `${jobs.length} task(s)` + (live ? ` · ${live} running` : '');
  document.getElementById('list').innerHTML = jobs.map(j => `
    <div class="row ${j.id===selected?'sel':''}" onclick="pick('${j.id}')">
      <div class="top">
        <span class="proj">${esc(j.project)}</span>
        <span class="pill s-${j.state}">${j.state}</span>
      </div>
      <div class="task">${esc(j.task)}</div>
      <div class="meta">${esc(j.harness)} · ${esc(j.model||'—')}${j.cloned?' · clone':''}</div>
    </div>`).join('') || '<div class="row" style="color:var(--dim)">No tasks yet.</div>';
  if (selected) await detail(selected);
  timer = setTimeout(poll, live ? 1500 : 6000);
}

window.pick = id => { selected = id; poll(); };

window.act = async (id, what) => {
  if (what === 'cancel' && !confirm('Stop this task? The harness process is killed.')) return;
  await fetch(`/api/tasks/${id}/${what}`, {method:'POST'});
  if (what === 'forget') selected = null;
  poll();
};

async function detail(id) {
  const [j, log] = await Promise.all([sjson('/api/tasks/'+id), sjson('/api/tasks/'+id+'/log')]);
  if (!j || j.error === 'not found') return;
  const files = (j.delivered||[]).map(f => `<li>${esc(f)}</li>`).join('');
  document.getElementById('detail').innerHTML = `
   <dl class="kv">
     <dt>project</dt><dd>${esc(j.project)}</dd>
     <dt>workspace</dt><dd>${esc(j.workspace)}${j.cloned?' <span style="color:var(--dim)">(clone — the real tree is untouched)</span>':''}</dd>
     <dt>harness</dt><dd>${esc(j.harness)}</dd>
     <dt>model</dt><dd>${esc(j.model||'—')}</dd>
     <dt>routed by</dt><dd>${esc(j.routed_by)}</dd>
     <dt>state</dt><dd><span class="pill s-${j.state}">${j.state}</span>${j.exit_code!=null?' exit '+j.exit_code:''}</dd>
     <dt>started</dt><dd>${esc(j.started||'—')}</dd>
     <dt>finished</dt><dd>${esc(j.finished||'—')}</dd>
     ${j.error?`<dt>error</dt><dd style="color:var(--bad)">${esc(j.error)}</dd>`:''}
     ${j.run_dir?`<dt>run dir</dt><dd>${esc(j.run_dir)}</dd>`:''}
   </dl>
   ${(j.state==='running'||j.state==='queued')?
     `<button class="ghost" onclick="act('${j.id}','cancel')">Cancel (stops the harness)</button>`:
     `<button class="ghost" onclick="act('${j.id}','forget')">Remove from list</button>`}
   ${files?`<h3 style="font-size:13px">delivered</h3><ul>${files}</ul>`:''}
   <h3 style="font-size:13px">output log</h3>
   <pre>${esc(log || '—')}</pre>`;
}

boot();
</script></body></html>
"""

def _state() -> dict:
    return {"projects": jobs.projects(), "harnesses": jobs.harnesses()}

def _make_handler(store: jobs.JobStore):
    class Handler(BaseHTTPRequestHandler):
        server_version = "harness2-web"

        def log_message(self, fmt, *a):  # keep the console for the panel's own errors
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, payload, code: int = 200) -> None:
            self._send(code, json.dumps(payload).encode(), "application/json")

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return {}

        def do_GET(self) -> None:
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path == "/":
                return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            if path == "/api/tasks":
                return self._json(store.list())
            if path == "/api/projects":
                return self._json(jobs.projects())
            if path == "/api/harnesses":
                return self._json(jobs.harnesses())
            if path.startswith("/api/tasks/"):
                rest = path[len("/api/tasks/") :]
                job_id, _, tail = rest.partition("/")
                if tail == "log":
                    return self._json(store.log(job_id))
                view = store.get(job_id)
                return self._json(view if view else {"error": "not found"}, 200 if view else 404)
            self._json({"error": "not found"}, 404)

        def do_POST(self) -> None:
            path = urlparse(self.path).path.rstrip("/")
            if path == "/api/tasks":
                body = self._read_json()
                task = str(body.get("task") or "").strip()
                project = str(body.get("project") or "").strip()
                if not task or not project:
                    return self._json({"error": "task and project are required"}, 400)
                try:
                    job = store.submit(
                        task,
                        project,
                        harness=str(body.get("harness") or ""),
                        model=str(body.get("model") or ""),
                        timeout=int(body.get("timeout") or 3600),
                    )
                except (ValueError, KeyError, RuntimeError) as exc:
                    return self._json({"error": str(exc)}, 400)
                return self._json(job, 201)
            if path.startswith("/api/tasks/"):
                rest = path[len("/api/tasks/") :]
                job_id, _, action = rest.partition("/")
                if action == "cancel":
                    return self._json({"cancelled": store.cancel(job_id)})
                if action == "forget":
                    return self._json({"forgotten": store.forget(job_id)})
            self._json({"error": "not found"}, 404)

    return Handler

def serve(host: str, port: int, *, root=None) -> None:
    store = jobs.JobStore(root=root)
    httpd = ThreadingHTTPServer((host, port), _make_handler(store))
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print(f"harness2-web on http://{shown}:{port}  (Ctrl-C to stop)", flush=True)
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"  note: bound to {host}; the panel has no authentication", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    finally:
        httpd.server_close()

def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="harness2-web", description=__doc__)
    p.add_argument("--host", default="127.0.0.1", help="bind address (default loopback)")
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--runs-root", default="", help="where task workspaces are written")
    args = p.parse_args(argv)
    root = args.runs_root or config.RUNS / "web"
    try:
        serve(args.host, args.port, root=root)
    except OSError as exc:
        print(f"could not start on {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
