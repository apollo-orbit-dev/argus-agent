"""FastAPI backbone. Hosts the engine and exposes its API to the dashboard and
(indirectly) validates the same engine the Telegram bot uses. Transport-agnostic
boundary: the UI talks to the engine ONLY through these HTTP + SSE routes.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import persist_to_env
from engine.engine import Engine
from engine.version import get_version

PROJECT_DIR = Path(__file__).resolve().parents[1]
DASHBOARD_DIR = PROJECT_DIR / "dashboard"
ENV_PATH = PROJECT_DIR / ".env"


class RunRequest(BaseModel):
    session_id: str = "dashboard"
    text: str
    skill: Optional[str] = None
    images: Optional[list] = None   # data: URLs or http(s) URLs, routed via the vision role


def _sse(ev) -> str:
    return f"data: {json.dumps(ev.to_json())}\n\n"


def create_app(engine: Engine) -> FastAPI:
    app = FastAPI(title="Argus", version=get_version())

    @app.post("/run")
    async def run(req: RunRequest):
        answer = await engine.run_task(req.session_id, req.text, requested_skill=req.skill,
                                       images=req.images, origin="dashboard")
        return {"answer": answer, "session_id": req.session_id}

    @app.get("/version")
    async def version():
        from engine.version import get_version
        return {"version": get_version()}

    @app.get("/updates")
    async def updates():
        # Is there a newer published release on GitHub? Cached ~30 min; never raises.
        from engine.version import check_for_update
        return await check_for_update()

    # ---- server logs (dashboard "Logs" page) — admin-gated; consume /logs/stream via fetch so the
    # admin-token header is sent (EventSource can't set headers). ----
    def _log_path() -> str:
        return engine.get_config().get("log_file") or "argus.log"

    @app.get("/logs/recent")
    async def logs_recent(request: Request, lines: int = 200):
        _require_admin(request)
        from engine.logtail import tail_lines
        return {"lines": tail_lines(_log_path(), min(max(lines, 1), 2000))}

    @app.get("/logs/stream")
    async def logs_stream(request: Request, lines: int = 200):
        _require_admin(request)
        from engine.logtail import stream_lines
        n = min(max(lines, 1), 2000)
        path = _log_path()

        async def gen():
            async for batch in stream_lines(path, n):
                yield f"data: {json.dumps({'lines': batch})}\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---- reliability harness (dashboard-only; admin-gated) ----
    @app.get("/reliability/summary")
    async def reliability_summary(request: Request, days: int = 30):
        _require_admin(request)
        return engine.reliability_summary(days)

    @app.get("/reliability/tools")
    async def reliability_tools(request: Request, days: int = 30):
        _require_admin(request)
        return engine.reliability_tools(days)

    @app.get("/reliability/routines")
    async def reliability_routines(request: Request, days: int = 30):
        _require_admin(request)
        return engine.reliability_routines(days)

    @app.get("/reliability/loop")
    async def reliability_loop(request: Request, days: int = 30):
        _require_admin(request)
        return engine.reliability_loop(days)

    @app.get("/reliability/failures")
    async def reliability_failures(request: Request, entity: str = "", limit: int = 20):
        _require_admin(request)
        return engine.reliability_failures(entity or None, limit)

    @app.post("/session/reset")
    async def session_reset(body: dict):
        # "New session": clear the conversation + the event replay buffer for a session.
        sid = (body.get("session_id") or "dashboard")
        engine.new_session(sid)
        return {"ok": True, "session_id": sid}

    @app.get("/events")
    async def events(session_id: Optional[str] = None):
        async def gen():
            if session_id:
                for ev in engine.recent(session_id):
                    yield _sse(ev)
            async for ev in engine.subscribe(session_id):
                yield _sse(ev)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     "Connection": "keep-alive"},
        )

    @app.get("/config")
    async def get_config():
        return engine.get_config()

    @app.patch("/config")
    async def patch_config(patch: dict):
        return engine.patch_config(patch)

    # ---- model presets / switching (shared by dashboard + Telegram) ----
    @app.get("/model/presets")
    async def model_presets_get():
        return engine.model_presets()

    @app.post("/model/presets")
    async def model_presets_add(body: dict):
        name = (body.get("model_name") or "").strip()
        if not name:
            raise HTTPException(400, "model_name is required")
        return engine.model_preset_add(name, body.get("base_url", ""), body.get("context_window"),
                                       body.get("label", ""), body.get("provider", "auto"),
                                       api_key=body.get("api_key"), capabilities=body.get("capabilities"))

    # ---- capability roles (chat / embedding / vision / …) mapped to connections ----
    @app.get("/model/roles")
    async def model_roles_get():
        return engine.model_roles()

    @app.post("/model/roles")
    async def model_roles_set(body: dict):
        role = (body.get("role") or "").strip()
        if not role:
            raise HTTPException(400, "role is required")
        try:
            return engine.set_role(role, body.get("connection"))   # connection None clears the role
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/model/switch")
    async def model_switch_ep(body: dict):
        arg = (body.get("name") or body.get("model_name") or "").strip()
        if not arg:
            raise HTTPException(400, "name (preset label or model id) is required")
        return engine.model_switch(arg)

    @app.post("/model/presets/remove")
    async def model_presets_remove(body: dict):
        arg = (body.get("name") or body.get("label") or "").strip()
        if not arg:
            raise HTTPException(400, "name is required")
        return {"removed": engine.model_preset_remove(arg)}

    @app.post("/model/presets/test")
    async def model_presets_test(body: dict):
        name = (body.get("name") or body.get("label") or "").strip()
        if not name:
            raise HTTPException(400, "name is required")
        return await engine.test_preset(name)

    @app.post("/model/reembed")
    async def model_reembed():
        async def gen():
            async for ev in engine.reembed_iter():
                yield f"data: {json.dumps(ev)}\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---- custom slash-command aliases (dashboard CRUD; hot-reloaded, not in the Telegram menu) ----
    @app.get("/commands")
    async def commands_get():
        return engine.custom_commands_list()

    @app.post("/commands")
    async def commands_set(body: dict):
        try:
            return {"name": engine.custom_command_set(body.get("name") or "", body.get("expansion") or "")}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/commands/remove")
    async def commands_remove(body: dict):
        return {"removed": engine.custom_command_remove(body.get("name") or "")}

    @app.get("/status")
    async def status():
        return await engine.status()

    @app.get("/run-status")
    async def run_status(session_id: str = "dashboard"):
        return engine.run_status(session_id)

    @app.get("/skills")
    async def skills():
        return engine.skills()

    @app.get("/library")
    async def library():
        return {"tools": engine.tools_overview(), "skills": engine.skills_overview()}

    # ---- web artifacts (build_web_page output) ----
    @app.get("/artifacts")
    async def artifacts_list():
        return {"artifacts": engine.artifacts_list()}

    @app.get("/artifacts/{filename}")
    async def artifacts_view(filename: str):
        # bare *.html basename only — no path traversal
        if "/" in filename or "\\" in filename or not filename.endswith(".html"):
            raise HTTPException(400, "invalid artifact name")
        path = os.path.join(engine.artifacts_dir(), filename)
        if not os.path.isfile(path):
            raise HTTPException(404, "no such artifact")
        return FileResponse(path, media_type="text/html")

    @app.post("/artifacts/delete")
    async def artifacts_delete(body: dict, request: Request):
        _require_admin(request)
        if not body.get("filename"):
            raise HTTPException(400, "body must include 'filename'")
        return engine.artifacts_delete(body["filename"])

    # ---- table store ----
    @app.get("/tables")
    async def tables_list():
        return {"tables": engine.tables_list()}

    @app.get("/tables/{name}/rows")
    async def table_rows(name: str, limit: int = 50, offset: int = 0):
        # A page of a table's rows for the dashboard viewer. Read-only; the name is identifier-
        # validated in the store, and the limit is bounded there.
        from engine.tools.tables import TableError
        try:
            return engine.table_rows(name, limit, offset)
        except TableError as e:
            raise HTTPException(404, str(e))

    @app.post("/tables/drop")
    async def tables_drop(body: dict, request: Request):
        _require_admin(request)
        if not body.get("name"):
            raise HTTPException(400, "body must include 'name'")
        return engine.tables_drop(body["name"])

    # ---- routines ----
    @app.get("/routines")
    async def routines_list():
        return {"routines": engine.routines_list()}

    @app.get("/routine-meta")
    async def routine_meta():
        return engine.routine_meta()

    @app.get("/routines/{name}")
    async def routine_get(name: str):
        r = engine.routine_get(name)
        if r is None:
            raise HTTPException(404, f"no routine '{name}'")
        return r

    @app.post("/routines")
    async def routine_save(body: dict, request: Request):
        _require_admin(request)
        if not body.get("name"):
            raise HTTPException(400, "body must include 'name'")
        res = engine.routine_save(body)
        if not res.get("ok"):
            raise HTTPException(400, res.get("error", "invalid routine"))
        return res

    @app.delete("/routines/{name}")
    async def routine_delete(name: str, request: Request):
        _require_admin(request)
        return engine.routine_delete(name)

    @app.post("/routines/{name}/run")
    async def routine_run(name: str, request: Request):
        _require_admin(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        deliver = body.get("deliver", True) if isinstance(body, dict) else True
        return await engine.run_routine_now(name, deliver=bool(deliver))

    # ---- outbound notifications ----
    @app.get("/notify")
    async def notify_status():
        return engine.notify_status()

    @app.post("/notify/test")
    async def notify_test(body: dict, request: Request):
        _require_admin(request)
        ch = body.get("channel", "")
        if not ch:
            raise HTTPException(400, "body must include 'channel'")
        return await engine.notify_test(ch)

    # ---- file workspace ----
    @app.get("/files")
    async def files_list():
        return {"files": engine.files_list()}

    @app.post("/files/upload")
    async def files_upload(request: Request):
        _require_admin(request)
        form = await request.form()
        up = form.get("file")
        if up is None:
            raise HTTPException(400, "no file uploaded")
        data = await up.read()
        return engine.files_save(up.filename, data)

    @app.get("/files/{name}")
    async def files_download(name: str, inline: int = 0):
        if "/" in name or "\\" in name:
            raise HTTPException(400, "invalid file name")
        path = engine.files_path(name)
        if not path:
            raise HTTPException(404, "no such file")
        if inline:
            # Preview in the dashboard: render in place instead of forcing a download. Workspace
            # files are agent/upload-writable, so serving them inline same-origin is an XSS vector
            # (an .html/.svg opened as a document would run in the dashboard's origin). Defenses:
            #   - CSP `sandbox`: if the file is ever loaded as a document it's script-disabled;
            #   - `nosniff`: the browser must honor the declared type, no content sniffing;
            #   - allowlist real types for images/PDF only; force text/plain for everything else
            #     (html, xml, unknown), so it can never execute as a document.
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            headers = {"Content-Disposition": f'inline; filename="{name}"',
                       "X-Content-Type-Options": "nosniff",
                       "Content-Security-Policy": "sandbox"}
            inline_images = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                             "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
                             "ico": "image/x-icon", "avif": "image/avif", "svg": "image/svg+xml"}
            if ext == "pdf":
                media = "application/pdf"
            elif ext in inline_images:
                media = inline_images[ext]
            else:
                media = "text/plain; charset=utf-8"
            return FileResponse(path, media_type=media, headers=headers)
        return FileResponse(path, filename=name)

    @app.post("/files/delete")
    async def files_delete(body: dict, request: Request):
        _require_admin(request)
        if not body.get("name"):
            raise HTTPException(400, "body must include 'name'")
        return engine.files_delete(body["name"])

    # ---- knowledge base ----
    @app.get("/knowledge")
    async def knowledge_overview():
        return engine.knowledge_overview()

    @app.post("/knowledge/forget")
    async def knowledge_forget(body: dict, request: Request):
        _require_admin(request)
        if not body.get("source"):
            raise HTTPException(400, "body must include 'source'")
        return engine.knowledge_forget(body["source"])

    # ---- watches ----
    @app.get("/watches")
    async def watches_list():
        return {"watches": engine.watches_list()}

    @app.post("/watches/delete")
    async def watches_delete(body: dict, request: Request):
        _require_admin(request)
        if not body.get("id"):
            raise HTTPException(400, "body must include 'id'")
        return engine.watch_delete(body["id"])

    @app.post("/library/tool/delete")
    async def library_tool_delete(body: dict, request: Request):
        _require_admin(request)
        if not body.get("name"):
            raise HTTPException(400, "body must include 'name'")
        return engine.delete_created_tool(body["name"])

    @app.post("/library/skill/delete")
    async def library_skill_delete(body: dict, request: Request):
        _require_admin(request)
        if not body.get("name"):
            raise HTTPException(400, "body must include 'name'")
        return engine.delete_created_skill(body["name"])

    @app.get("/scheduled")
    async def scheduled():
        return engine.scheduled_jobs()

    # ---- approval-gated dependency installs ----
    @app.get("/deps")
    async def deps():
        return engine.deps_overview()

    @app.post("/deps/approve")
    async def deps_approve(body: dict, request: Request):
        _require_admin(request)
        req_id = body.get("id", "")
        if not req_id:
            raise HTTPException(400, "body must include 'id'")
        return await engine.approve_dep(req_id)     # runs pip install

    @app.post("/deps/deny")
    async def deps_deny(body: dict, request: Request):
        _require_admin(request)
        req_id = body.get("id", "")
        if not req_id:
            raise HTTPException(400, "body must include 'id'")
        return engine.deny_dep(req_id)

    # ---- trusted-tool tier (human reviews the code, then approves unsandboxed execution) ----
    @app.get("/trust")
    async def trust():
        return engine.trust_overview()

    @app.post("/trust/approve")
    async def trust_approve(body: dict, request: Request):
        _require_admin(request)
        if not body.get("id"):
            raise HTTPException(400, "body must include 'id'")
        return engine.approve_trust(body["id"])

    @app.post("/trust/deny")
    async def trust_deny(body: dict, request: Request):
        _require_admin(request)
        if not body.get("id"):
            raise HTTPException(400, "body must include 'id'")
        return engine.deny_trust(body["id"])

    @app.post("/trust/revoke")
    async def trust_revoke(body: dict, request: Request):
        _require_admin(request)
        if not body.get("tool_name"):
            raise HTTPException(400, "body must include 'tool_name'")
        return engine.revoke_trust(body["tool_name"])

    @app.get("/memory/stats")
    async def memory_stats(session_id: str = "dashboard"):
        return engine.memory_stats(session_id)

    @app.get("/memory/list")
    async def memory_list(session_id: str = "dashboard"):
        return {"facts": engine.memory_list(session_id)}

    @app.post("/memory/forget")
    async def memory_forget(body: dict, request: Request):
        _require_admin(request)
        if body.get("id") is None:
            raise HTTPException(400, "body must include 'id'")
        ok = engine.memory_forget(body.get("session_id", "dashboard"), int(body["id"]))
        return {"ok": ok, "id": body["id"]}

    @app.post("/memory/summary")
    async def memory_summary(body: dict):
        return await engine.memory_summary(body.get("session_id", "dashboard"))

    # ---- behavioral rules (dashboard-managed; mutations admin-gated like /memory) ----
    @app.get("/rules/list")
    async def rules_list():
        return {"rules": engine.rules_list()}

    @app.post("/rules/save")
    async def rules_save(body: dict, request: Request):
        _require_admin(request)
        text = (body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "rule text required")
        rec = engine.rules_add(text)
        return {"rule": rec}

    @app.post("/rules/remove")
    async def rules_remove(body: dict, request: Request):
        _require_admin(request)
        return {"ok": engine.rules_remove(body.get("id", ""))}

    @app.post("/rules/toggle")
    async def rules_toggle(body: dict, request: Request):
        _require_admin(request)
        return {"ok": engine.rules_set_enabled(body.get("id", ""), bool(body.get("enabled")))}

    # ---- approvals (dashboard/Telegram-managed; mutations admin-gated like /rules) ----
    @app.get("/approvals")
    async def approvals_list():
        return {"approvals": engine.approvals_list()}

    @app.get("/permissions")
    async def permissions_list():
        return {"permissions": engine.permissions_list()}

    @app.post("/permissions/set")
    async def permissions_set(body: dict, request: Request):
        _require_admin(request)
        try:
            engine.permission_set(body.get("key", ""), body.get("state", ""))
        except (ValueError, KeyError):
            raise HTTPException(400, "invalid key/state")
        return {"ok": True}

    @app.post("/approvals/decide")
    async def approvals_decide(body: dict, request: Request):
        _require_admin(request)
        result = engine.approvals_decide(body.get("req_id", ""), body.get("action", ""))
        return {"result": result}

    @app.get("/usage")
    async def usage(session_id: str = "dashboard"):
        return await engine.usage(session_id)

    @app.post("/compact")
    async def compact(body: dict):
        return await engine.compact(body.get("session_id", "dashboard"))

    def _require_admin(request: Request):
        """If admin_token is configured, sensitive endpoints require a matching
        X-Admin-Token header. Empty token = open (the spec's open local dashboard)."""
        tok = engine.config.admin_token
        if tok and request.headers.get("X-Admin-Token") != tok:
            raise HTTPException(401, "admin token required for this endpoint")

    # ---- system prompt (editable, persisted to system_prompt.txt) ----
    @app.get("/system-prompt")
    async def get_system_prompt():
        return {"prompt": engine.get_system_prompt()}

    @app.put("/system-prompt")
    async def put_system_prompt(body: dict, request: Request):
        _require_admin(request)
        prompt = body.get("prompt", "")
        engine.set_system_prompt(prompt)
        return {"saved": True, "prompt": engine.get_system_prompt()}

    # ---- SOUL (persona; persisted to SOUL.md) ----
    @app.get("/soul")
    async def get_soul():
        return {"soul": engine.get_soul()}

    @app.post("/soul/revert")
    async def revert_soul(request: Request):
        _require_admin(request)
        return engine.revert_soul()

    @app.put("/soul")
    async def put_soul(body: dict, request: Request):
        _require_admin(request)
        engine.set_soul(body.get("soul", ""))
        return {"saved": True, "soul": engine.get_soul()}

    # ---- persist live config to .env, and raw .env view/edit ----
    @app.post("/config/save")
    async def config_save(request: Request):
        _require_admin(request)
        try:
            persist_to_env(engine.config, ENV_PATH)
        except Exception as e:
            raise HTTPException(500, f"could not write .env: {e}")
        return {"saved": True, "path": str(ENV_PATH)}

    @app.get("/config/env")
    async def config_env_get(request: Request):
        _require_admin(request)
        text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
        return {"text": text, "path": str(ENV_PATH)}

    @app.put("/config/env")
    async def config_env_put(body: dict, request: Request):
        _require_admin(request)
        text = body.get("text")
        if not isinstance(text, str):
            raise HTTPException(400, "body must include 'text'")
        try:
            ENV_PATH.write_text(text, encoding="utf-8")
        except Exception as e:
            raise HTTPException(500, f"could not write .env: {e}")
        return {"saved": True, "note": "restart to apply changes that need a restart"}

    # ---- restart (re-exec the process; reloads .env) ----
    @app.post("/admin/restart")
    async def restart(request: Request):
        _require_admin(request)
        async def _do():
            await asyncio.sleep(0.4)  # let the HTTP response flush first
            os.execv(sys.executable, [sys.executable] + sys.argv)
        asyncio.create_task(_do())
        return {"restarting": True}

    # Serve index.html with content-hash cache-busting on its asset URLs. Without this, a browser
    # holding a stale index.html keeps requesting bare /app.js (which it also has cached) and never
    # picks up a redeploy. Stamping ?v=<hash-of-assets> gives changed assets a NEW url the browser
    # can't satisfy from cache, so a normal reload lands the new bundle — no hard-refresh needed.
    @app.get("/", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    async def _index():
        html = (DASHBOARD_DIR / "index.html").read_text(encoding="utf-8")
        v = _asset_version()
        html = html.replace('href="/styles.css"', f'href="/styles.css?v={v}"')
        html = html.replace('src="/app.js"', f'src="/app.js?v={v}"')
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    # Serve the remaining dashboard files at "/" (mounted last so API + index routes win).
    if DASHBOARD_DIR.exists():
        app.mount("/", _NoCacheStatic(directory=str(DASHBOARD_DIR), html=True), name="dashboard")

    return app


def _asset_version() -> str:
    """Short content hash of the cache-busted assets, so the ?v= stamp changes iff a file changes."""
    h = hashlib.md5()
    for name in ("app.js", "styles.css"):
        try:
            h.update((DASHBOARD_DIR / name).read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:10]


class _NoCacheStatic(StaticFiles):
    """Dashboard assets with `Cache-Control: no-cache` so browsers ALWAYS revalidate before using a
    cached copy. The etag makes that a cheap 304 when nothing changed, but a redeploy of app.js /
    styles.css takes effect immediately instead of a stale bundle lingering in the browser."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp
