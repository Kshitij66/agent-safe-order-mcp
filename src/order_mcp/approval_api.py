"""Approval dashboard + REST API for humans. Agents never talk to this service.

    GET  /                          minimal HTML dashboard
    GET  /api/actions?status=...    list queued actions
    POST /api/actions/{id}/approve  execute an action
    POST /api/actions/{id}/reject   close an action
    GET  /api/audit                 recent audit log

All /api routes require the X-Admin-Token header.
"""
from __future__ import annotations

import hmac
import os

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine

from . import approvals, audit
from .config import Settings
from .db import init_db, make_engine
from .services import InvalidState, NotFound


class Decision(BaseModel):
    approver: str = Field(min_length=1, max_length=80)
    note: str | None = Field(default=None, max_length=500)


def create_app(engine: Engine, settings: Settings) -> FastAPI:
    if not settings.admin_token:
        raise RuntimeError("ADMIN_TOKEN must be set to run the approval API")
    app = FastAPI(title="Agent-Safe Orders: Approvals")

    def require_admin(x_admin_token: str = Header(default="")) -> None:
        # Constant-time comparison avoids leaking the token through response timing.
        if not hmac.compare_digest(x_admin_token.encode(), settings.admin_token.encode()):
            raise HTTPException(status_code=401, detail="invalid admin token")

    def _decide(fn, action_id: int, d: Decision):
        try:
            return fn(engine, action_id, approver=f"human:{d.approver}", note=d.note)
        except NotFound as e:
            raise HTTPException(404, str(e))
        except InvalidState as e:
            raise HTTPException(409, str(e))

    @app.get("/api/actions", dependencies=[Depends(require_admin)])
    def list_actions(status: str | None = None):
        approvals.expire_stale(engine)
        return approvals.list_actions(engine, status)

    @app.post("/api/actions/{action_id}/approve", dependencies=[Depends(require_admin)])
    def approve(action_id: int, d: Decision):
        return _decide(approvals.approve, action_id, d)

    @app.post("/api/actions/{action_id}/reject", dependencies=[Depends(require_admin)])
    def reject(action_id: int, d: Decision):
        return _decide(approvals.reject, action_id, d)

    @app.get("/api/audit", dependencies=[Depends(require_admin)])
    def audit_log(limit: int = 100):
        return audit.recent(engine, max(1, min(limit, 500)))

    @app.get("/healthz")
    def health():
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    def dashboard():
        return DASHBOARD_HTML

    return app


DASHBOARD_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Pending agent actions</title>
<style>
 body{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem;color:#222}
 table{width:100%;border-collapse:collapse} td,th{padding:.5rem;border-bottom:1px solid #ddd;text-align:left;vertical-align:top}
 code{background:#f3f3f3;padding:2px 4px;border-radius:3px} button{margin-right:.4rem;cursor:pointer}
 .ok{color:#0a7d32}.bad{color:#b00020}
</style></head><body>
<h1>Pending agent actions</h1>
<p>Admin token <input id="tok" type="password"> Your name <input id="who" value="admin">
<button onclick="load()">Load</button></p>
<p id="msg"></p>
<table><thead><tr><th>ID</th><th>Action</th><th>Params</th><th>Agent's reason</th><th>Expires (UTC)</th><th></th></tr></thead>
<tbody id="rows"></tbody></table>
<script>
const h = () => ({'X-Admin-Token': document.getElementById('tok').value, 'Content-Type': 'application/json'});
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function load(){
  const r = await fetch('/api/actions?status=pending', {headers: h()});
  const msg = document.getElementById('msg');
  if(!r.ok){ msg.className='bad'; msg.textContent='Error ' + r.status; return; }
  const rows = await r.json();
  msg.className=''; msg.textContent = rows.length + ' pending';
  document.getElementById('rows').innerHTML = rows.map(a => `<tr><td>${a.id}</td><td>${esc(a.tool)}</td>
    <td><code>${esc(JSON.stringify(a.params))}</code></td><td>${esc(a.reason)}</td><td>${esc(a.expires_at)}</td>
    <td><button onclick="decide(${a.id},'approve')">Approve</button><button onclick="decide(${a.id},'reject')">Reject</button></td></tr>`).join('');
}
async function decide(id, kind){
  const r = await fetch(`/api/actions/${id}/${kind}`, {method:'POST', headers:h(),
    body: JSON.stringify({approver: document.getElementById('who').value})});
  const body = await r.json(); const msg = document.getElementById('msg');
  msg.className = r.ok && body.status !== 'failed' ? 'ok' : 'bad';
  msg.textContent = `#${id}: ` + (r.ok ? body.status + (body.result ? ' ' + JSON.stringify(body.result) : '') : body.detail);
  load();
}
</script></body></html>"""


def main() -> None:
    import uvicorn

    settings = Settings.from_env()
    engine = make_engine(settings.database_url)
    init_db(engine)
    uvicorn.run(create_app(engine, settings), host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8000")))


if __name__ == "__main__":
    main()
