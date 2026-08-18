"""Simai web API + admin console (sections 14, 15, 17, 18).

Binds to 127.0.0.1 by default.  All state-changing routes require a web
session created by unlocking (or logging in while already unlocked).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..config import Config
from ..core import (
    autorelations,
    backup,
    candidates,
    capture,
    daily,
    dictation,
    export,
    qa,
    relations,
    reorganize,
    search,
    tree,
)
from ..core.state import AppState
from ..crypto import keyring
from ..db.engine import DatabaseError, DatabaseLocked, now_iso
from ..llm.client import ModelError, build_client
from .auth import SESSION_COOKIE, SessionStore, ensure_plugin_token, require_session

log = logging.getLogger("simai.api")
STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"


# -- request bodies -----------------------------------------------------------


class UnlockBody(BaseModel):
    passphrase: str = Field(min_length=1)


class InitBody(BaseModel):
    passphrase: str = Field(min_length=8)


class CaptureBody(BaseModel):
    text: str = Field(min_length=1)
    use_model: bool = True


class ConfirmBody(BaseModel):
    action: str | None = None
    parent_id: str | None = None
    target_node_id: str | None = None
    edited_title: str | None = None
    edited_content: str | None = None
    node_type: str | None = None


class NodeCreateBody(BaseModel):
    title: str
    body: str = ""
    node_type: str = "idea"
    parent_id: str | None = None


class NodeUpdateBody(BaseModel):
    change_type: str = "revise"
    title: str | None = None
    body: str | None = None
    node_type: str | None = None


class MoveBody(BaseModel):
    new_parent_id: str | None


class RelationBody(BaseModel):
    from_node_id: str
    to_node_id: str
    relation_type: str
    rationale: str | None = None


class RelationStateBody(BaseModel):
    state: str


class QueryBody(BaseModel):
    question: str = Field(min_length=1)


class ReorganizeBody(BaseModel):
    node_id: str | None = None  # None reorganises the top level


class SearchBody(BaseModel):
    q: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=10, ge=1, le=100)


class ExportBody(BaseModel):
    format: str
    root_id: str | None = None
    include_history: bool = False
    include_ai_relations: bool = True
    node_types: list[str] | None = None
    since: str | None = None
    until: str | None = None
    encryption_passphrase: str | None = Field(default=None, min_length=8)


class DictTermBody(BaseModel):
    term: str = Field(min_length=1, max_length=80)
    note: str | None = None


# plugin-facing tool API bodies (section 17). NOTE: request models must be
# module-level - with `from __future__ import annotations` FastAPI cannot
# resolve function-local classes from their string annotations.


class ToolCaptureBody(BaseModel):
    binding_id: str
    text: str = Field(min_length=1)
    message_id: str | None = None


class ToolDecideBody(BaseModel):
    binding_id: str
    decision: str  # confirm | reject | snooze
    parent_id: str | None = None
    edited_content: str | None = None


class ToolQueryBody(BaseModel):
    binding_id: str
    question: str = Field(min_length=1)


class ToolSearchBody(BaseModel):
    binding_id: str
    q: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=5, ge=1, le=50)


class ToolDailyBody(BaseModel):
    binding_id: str


class ToolDictationCloseBody(BaseModel):
    binding_id: str
    dictation_id: str = Field(min_length=1, max_length=128)


def create_app(config: Config, state: AppState | None = None) -> FastAPI:
    from contextlib import asynccontextmanager

    from .ingress import IngressServer

    ingress = IngressServer(config)

    async def cleanup_exports():
        ttl = int(config.section("exports").get("plaintext_ttl_minutes", 30))
        while True:
            export.cleanup_expired(config.export_dir, ttl)
            await asyncio.sleep(60)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await ingress.start()
        cleanup_task = asyncio.create_task(cleanup_exports())
        try:
            yield
        finally:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
            await ingress.stop()

    app = FastAPI(title="Simai", docs_url=None, redoc_url=None, lifespan=lifespan)
    if config.web_bind not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("web.bind must be a loopback address (design section 15.4 / 24.4)")

    app.state.config = config
    app.state.vault = state or AppState(config)
    app.state.sessions = SessionStore(int(config.section("web").get("session_idle_minutes", 30)))
    app.state.llm = build_client(config)
    app.state.ingress = ingress
    plugin_token = ensure_plugin_token(config.plugin_token_path)
    unlock_failures: list[float] = []
    unlock_failure_lock = threading.Lock()

    vault: AppState = app.state.vault
    sessions: SessionStore = app.state.sessions

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith(("/api/", "/plugin-api/")):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(DatabaseLocked)
    async def _locked(_req, exc):
        return JSONResponse(status_code=423, content={"detail": str(exc)})

    @app.exception_handler(ModelError)
    async def _model(_req, exc):
        return JSONResponse(status_code=502, content={"detail": f"model task failed: {exc}"})

    # -- static console -------------------------------------------------------
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    # -- lock / unlock ---------------------------------------------------------
    @app.get("/api/status")
    async def status(request: Request):
        # Before authentication reveal only what the unlock screen needs.
        if not sessions.validate(request.cookies.get(SESSION_COOKIE)):
            return {
                "locked": not vault.is_unlocked,
                "initialized": config.key_header_path.is_file(),
            }
        st = vault.status()
        st["initialized"] = config.key_header_path.is_file()
        return st

    @app.post("/api/init")
    async def init_vault(body: InitBody, response: Response):
        if config.key_header_path.is_file():
            raise HTTPException(409, "Vault already initialized")
        recovery_pack = vault.initialize_vault(body.passphrase)
        response.set_cookie(SESSION_COOKIE, sessions.create(), httponly=True, samesite="strict")
        # vault header now exists: start the unix-socket inbox if startup skipped it
        try:
            await ingress.start()
        except OSError:
            log.warning("ingress failed to start after init; restart simai serve")
        # returned once, never persisted server-side
        return {"initialized": True, "recovery_pack": recovery_pack}

    @app.post("/api/unlock")
    async def unlock(body: UnlockBody, response: Response):
        _check_unlock_rate_limit(unlock_failures, unlock_failure_lock)
        try:
            await asyncio.to_thread(vault.unlock, body.passphrase)
        except keyring.WrongPassphrase:
            _record_unlock_failure(unlock_failures, unlock_failure_lock)
            raise HTTPException(403, "口令错误")
        except (keyring.VaultError, DatabaseError) as exc:
            raise HTTPException(500, str(exc))
        response.set_cookie(SESSION_COOKIE, sessions.create(), httponly=True, samesite="strict")
        with unlock_failure_lock:
            unlock_failures.clear()
        backlog = await asyncio.to_thread(_process_backlog_safely, app)
        return {"locked": False, "backlog_processed": backlog}

    @app.post("/api/login")
    async def login(body: UnlockBody, response: Response):
        """Re-login while the vault is already unlocked (session expired)."""
        if not vault.is_unlocked:
            return await unlock(body, response)
        _check_unlock_rate_limit(unlock_failures, unlock_failure_lock)
        try:
            probe = await asyncio.to_thread(keyring.unlock_vault, config.key_header_path, body.passphrase)
            probe.wipe()
        except keyring.WrongPassphrase:
            _record_unlock_failure(unlock_failures, unlock_failure_lock)
            raise HTTPException(403, "口令错误")
        response.set_cookie(SESSION_COOKIE, sessions.create(), httponly=True, samesite="strict")
        with unlock_failure_lock:
            unlock_failures.clear()
        return {"locked": False}

    @app.post("/api/lock", dependencies=[Depends(require_session)])
    async def lock(response: Response):
        vault.lock()
        sessions.clear()
        response.delete_cookie(SESSION_COOKIE)
        return {"locked": True}

    # -- capture & candidates ---------------------------------------------------
    @app.post("/api/capture", dependencies=[Depends(require_session)])
    def capture_text(body: CaptureBody):
        with vault.transaction() as tx:
            keys = vault.keys
            if body.use_model:
                cards = capture.run_capture(
                    tx, app.state.llm, keys.excerpt_key, body.text, source_binding_id="local_web"
                )
            else:
                cards = [
                    capture.create_raw_candidate(
                        tx, keys.excerpt_key, body.text, source_binding_id="local_web"
                    )
                ]
        return {"cards": cards}

    @app.get("/api/candidates", dependencies=[Depends(require_session)])
    async def list_pending(status: str = "pending"):
        with vault.reading() as conn:
            keys = vault.keys
            items = candidates.list_candidates(conn, status, keys.excerpt_key)
            return {
                "candidates": [
                    capture.confirmation_card(conn, c["id"], keys.excerpt_key) if status == "pending" else c
                    for c in items
                ]
            }

    @app.post("/api/candidates/{candidate_id}/confirm", dependencies=[Depends(require_session)])
    def confirm(candidate_id: str, body: ConfirmBody):
        try:
            with vault.transaction() as tx:
                keys = vault.keys
                result = candidates.confirm_candidate(
                    tx,
                    keys.audit_hmac_key,
                    candidate_id,
                    action=body.action,
                    parent_id=body.parent_id,
                    target_node_id=body.target_node_id,
                    edited_title=body.edited_title,
                    edited_content=body.edited_content,
                    node_type=body.node_type,
                )
        except (candidates.CandidateError, tree.TreeError) as exc:
            raise HTTPException(400, str(exc))
        _post_confirm_enrichment(app, result["node_id"])
        return result

    @app.post("/api/candidates/{candidate_id}/reject", dependencies=[Depends(require_session)])
    async def reject(candidate_id: str):
        try:
            with vault.transaction() as tx:
                candidates.reject_candidate(tx, vault.keys.audit_hmac_key, candidate_id)
        except candidates.CandidateError as exc:
            raise HTTPException(409, str(exc))
        return {"status": "rejected"}

    @app.post("/api/candidates/{candidate_id}/snooze", dependencies=[Depends(require_session)])
    async def snooze(candidate_id: str):
        try:
            with vault.transaction() as tx:
                candidates.snooze_candidate(tx, vault.keys.audit_hmac_key, candidate_id)
        except candidates.CandidateError as exc:
            raise HTTPException(409, str(exc))
        return {"status": "snoozed"}

    # -- tree --------------------------------------------------------------------
    @app.get("/api/tree", dependencies=[Depends(require_session)])
    async def get_tree(root_id: str | None = None):
        with vault.reading() as conn:
            return {"nodes": tree.subtree(conn, root_id)}

    @app.post("/api/nodes", dependencies=[Depends(require_session)])
    def create_node(body: NodeCreateBody):
        try:
            with vault.transaction() as tx:
                result = tree.create_node(
                    tx, vault.keys.audit_hmac_key, body.title, body.body, body.node_type, body.parent_id
                )
        except tree.TreeError as exc:
            raise HTTPException(400, str(exc))
        _post_confirm_enrichment(app, result["node_id"])
        return result

    @app.get("/api/nodes/{node_id}", dependencies=[Depends(require_session)])
    async def node_detail(node_id: str):
        with vault.reading() as conn:
            try:
                node = dict(tree.get_node(conn, node_id))
            except tree.TreeError:
                raise HTTPException(404, f"Node not found: {node_id}")
            rev = tree.get_current_revision(conn, node_id)
            return {
                "node": node,
                "body": rev["body"] if rev else "",
                "revision_no": rev["revision_no"] if rev else 0,
                "path": tree.node_path(conn, node_id),
                "history": tree.revision_timeline(conn, node_id),
                "relations": relations.relations_of(conn, node_id, include_stale=True),
                "children": [dict(c) for c in tree.list_children(conn, node_id)],
            }

    @app.post("/api/nodes/{node_id}", dependencies=[Depends(require_session)])
    async def update_node(node_id: str, body: NodeUpdateBody):
        try:
            with vault.transaction() as tx:
                result = tree.update_node(
                    tx,
                    vault.keys.audit_hmac_key,
                    node_id,
                    body.change_type,
                    title=body.title,
                    body=body.body,
                    node_type=body.node_type,
                )
        except tree.TreeError as exc:
            raise HTTPException(400, str(exc))
        _post_confirm_enrichment(app, node_id)
        return result

    @app.post("/api/nodes/{node_id}/move", dependencies=[Depends(require_session)])
    async def move_node(node_id: str, body: MoveBody):
        try:
            with vault.transaction() as tx:
                return tree.move_node(tx, vault.keys.audit_hmac_key, node_id, body.new_parent_id)
        except tree.TreeError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/nodes/{node_id}/archive", dependencies=[Depends(require_session)])
    async def archive_node(node_id: str):
        try:
            with vault.transaction() as tx:
                tree.archive_node(tx, vault.keys.audit_hmac_key, node_id)
        except tree.TreeError as exc:
            raise HTTPException(404, str(exc))
        return {"state": "archived"}

    @app.post("/api/nodes/{node_id}/restore/{revision_no}", dependencies=[Depends(require_session)])
    async def restore_rev(node_id: str, revision_no: int):
        try:
            with vault.transaction() as tx:
                return tree.restore_revision(tx, vault.keys.audit_hmac_key, node_id, revision_no)
        except tree.TreeError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/tree/reorganize", dependencies=[Depends(require_session)])
    def reorganize_children(body: ReorganizeBody):
        """AI analysis of one node's children: merge suggestions become pending
        candidates, sibling relations are recorded as ai_generated. Nothing is
        applied without the user's confirmation."""
        try:
            with vault.transaction() as tx:
                keys = vault.keys
                return reorganize.reorganize_children(
                    tx, app.state.llm, keys.audit_hmac_key, keys.excerpt_key, body.node_id
                )
        except tree.TreeError as exc:
            raise HTTPException(404, str(exc))

    @app.post("/api/tree/reorganize/deep", dependencies=[Depends(require_session)])
    def reorganize_deep():
        """Deep scan of the whole tree: every parent with >=2 children whose
        subtree changed since its last pass gets one reorganize analysis,
        bounded per invocation. Proposals only; nothing is auto-applied."""
        with vault.transaction() as tx:
            keys = vault.keys
            return reorganize.reorganize_tree(
                tx, app.state.llm, keys.audit_hmac_key, keys.excerpt_key
            )

    # -- relations ------------------------------------------------------------------
    @app.get("/api/relations/pending", dependencies=[Depends(require_session)])
    async def pending_relations():
        """ai_generated relations awaiting review, for the confirmation inbox."""
        with vault.reading() as conn:
            return {"relations": relations.pending_ai(conn)}

    @app.get("/api/relations/graph", dependencies=[Depends(require_session)])
    async def relation_graph(node_id: str, depth: int = 1):
        try:
            with vault.reading() as conn:
                return relations.local_graph(conn, node_id, depth)
        except tree.TreeError as exc:
            raise HTTPException(404, str(exc))

    @app.post("/api/relations", dependencies=[Depends(require_session)])
    async def add_relation(body: RelationBody):
        try:
            with vault.transaction() as tx:
                rel_id = relations.add_relation(
                    tx,
                    vault.keys.audit_hmac_key,
                    body.from_node_id,
                    body.to_node_id,
                    body.relation_type,
                    origin="user",
                    rationale=body.rationale,
                )
        except (relations.RelationError, tree.TreeError) as exc:
            raise HTTPException(400, str(exc))
        return {"relation_id": rel_id}

    @app.post("/api/relations/{relation_id}/state", dependencies=[Depends(require_session)])
    async def relation_state(relation_id: str, body: RelationStateBody):
        try:
            with vault.transaction() as tx:
                effective_id = relations.set_relation_state(
                    tx, vault.keys.audit_hmac_key, relation_id, body.state
                )
        except relations.RelationError as exc:
            raise HTTPException(400, str(exc))
        return {"state": body.state, "relation_id": effective_id}

    # -- search & query -----------------------------------------------------------
    @app.post("/api/search", dependencies=[Depends(require_session)])
    def do_search(body: SearchBody):
        with vault.reading() as conn:
            return {"results": search.combined_search(conn, app.state.llm, body.q, body.limit)}

    @app.post("/api/query", dependencies=[Depends(require_session)])
    def do_query(body: QueryBody):
        with vault.reading() as conn:
            return qa.answer_question(conn, app.state.llm, body.question)

    # -- daily / export / backup ----------------------------------------------------
    @app.post("/api/daily/run", dependencies=[Depends(require_session)])
    def daily_run():
        return daily.run_daily(vault, app.state.llm)

    @app.post("/api/export", dependencies=[Depends(require_session)])
    async def do_export(body: ExportBody):
        export.cleanup_expired(
            config.export_dir, int(config.section("exports").get("plaintext_ttl_minutes", 30))
        )
        try:
            with vault.transaction() as tx:
                result = export.run_export(
                    tx,
                    config.export_dir,
                    body.format,
                    root_id=body.root_id,
                    include_history=body.include_history,
                    include_ai_relations=body.include_ai_relations,
                    node_types=body.node_types,
                    since=body.since,
                    until=body.until,
                    encryption_passphrase=body.encryption_passphrase,
                )
        except export.ExportError as exc:
            raise HTTPException(400, str(exc))
        return result

    @app.get("/api/export/{export_file}", dependencies=[Depends(require_session)])
    async def download_export(export_file: str):
        path = (config.export_dir / export_file).resolve()
        if not path.is_file() or path.parent != config.export_dir.resolve():
            raise HTTPException(404, "export file not found (may have expired)")
        return FileResponse(path, filename=path.name)

    @app.post("/api/backup", dependencies=[Depends(require_session)])
    async def do_backup():
        try:
            with vault.reading() as conn:
                return backup.create_backup(
                    conn,
                    vault.keys.sqlcipher_hex(),
                    config.backup_dir,
                    config.key_header_path,
                    config.inbox_dir,
                )
        except backup.BackupError as exc:
            raise HTTPException(500, str(exc))

    # -- config & model health --------------------------------------------------------
    @app.get("/api/config", dependencies=[Depends(require_session)])
    async def get_config():
        """Read-only, secret-free view for the console (section 14.6)."""
        return {
            "profile": config.profile,
            "timezone": config.timezone,
            "daily_capture": config.section("daily_capture"),
            "models": config.section("models"),
            "relations": config.section("relations"),
            "placement": config.section("placement"),
            "source_bindings": [
                {
                    "id": b.id,
                    "channel": b.channel,
                    "account_id": b.account_id,
                    "enabled": b.enabled,
                    "passive_capture": b.passive_capture,
                }
                for b in config.source_bindings()
            ],
        }

    @app.get("/api/models/health", dependencies=[Depends(require_session)])
    def model_health():
        return {
            task: app.state.llm.health_check(task)
            for task in (
                "capture",
                "daily_extract",
                "graph_routing",
                "query",
                "query_relevance",
                "reorganize",
            )
        }

    # -- personal dictionary (section 8.3) ---------------------------------------
    @app.get("/api/dictionary", dependencies=[Depends(require_session)])
    async def dictionary_list():
        with vault.reading() as conn:
            rows = conn.execute(
                "SELECT term, note, created_at FROM personal_dictionary ORDER BY term"
            ).fetchall()
            return {"terms": [dict(r) for r in rows]}

    @app.post("/api/dictionary", dependencies=[Depends(require_session)])
    async def dictionary_add(body: DictTermBody):
        with vault.transaction() as tx:
            tx.execute(
                """INSERT INTO personal_dictionary (term, note, created_at) VALUES (?,?,?)
                   ON CONFLICT(term) DO UPDATE SET note = excluded.note""",
                (body.term.strip(), body.note, now_iso()),
            )
        return {"term": body.term.strip()}

    @app.delete("/api/dictionary/{term}", dependencies=[Depends(require_session)])
    async def dictionary_remove(term: str):
        with vault.transaction() as tx:
            tx.execute("DELETE FROM personal_dictionary WHERE term = ?", (term,))
        return {"removed": term}

    @app.get("/api/jobs", dependencies=[Depends(require_session)])
    async def jobs():
        with vault.reading() as conn:
            rows = conn.execute("SELECT * FROM job_runs ORDER BY started_at DESC LIMIT 20").fetchall()
            return {"jobs": [dict(r) for r in rows]}

    # -- plugin-facing tool API (section 17) ------------------------------------
    # Serves the OpenClaw plugin's simai_* tools on this loopback-only port.
    # The plugin has already whitelisted channel/account/sender; requests must
    # carry a known binding id.  Web-only capabilities (unlock, whitelist
    # changes, bulk ops, full export, backup/restore) are NOT exposed here.
    # Responses never include passphrases, keys, or internal file paths.

    def _plugin_binding(binding_id: str):
        binding = next((b for b in config.source_bindings() if b.id == binding_id and b.enabled), None)
        if binding is None:
            raise HTTPException(403, "unknown or disabled source binding")
        return binding

    def _require_plugin_token(x_simai_plugin_token: str | None = Header(None)):
        import secrets

        if not x_simai_plugin_token or not secrets.compare_digest(x_simai_plugin_token, plugin_token):
            raise HTTPException(401, "invalid plugin credential")

    plugin_auth = [Depends(_require_plugin_token)]

    @app.post("/plugin-api/capture", dependencies=plugin_auth)
    def tool_capture(body: ToolCaptureBody):
        _plugin_binding(body.binding_id)
        import hashlib
        import hmac as hmac_mod

        with vault.transaction() as tx:
            keys = vault.keys
            message_hmac = None
            if body.message_id:
                message_hmac = hmac_mod.new(
                    keys.audit_hmac_key,
                    f"{body.binding_id}|mid:{body.message_id}".encode(),
                    hashlib.sha256,
                ).hexdigest()
            cards = capture.run_capture(
                tx,
                app.state.llm,
                keys.excerpt_key,
                body.text,
                source_binding_id=body.binding_id,
                message_hmac=message_hmac,
            )
            if message_hmac:
                candidates.mark_explicit_receipt(tx, body.binding_id, message_hmac)
        return {"cards": cards}

    @app.get("/plugin-api/candidates", dependencies=plugin_auth)
    async def tool_candidates(binding_id: str):
        _plugin_binding(binding_id)
        with vault.reading() as conn:
            keys = vault.keys
            items = candidates.list_candidates(
                conn, "pending", keys.excerpt_key, source_binding_id=binding_id
            )
            return {"candidates": [capture.confirmation_card(conn, c["id"], keys.excerpt_key) for c in items]}

    @app.post("/plugin-api/candidates/{candidate_id}/decide", dependencies=plugin_auth)
    def tool_decide(candidate_id: str, body: ToolDecideBody):
        _plugin_binding(body.binding_id)
        try:
            with vault.transaction() as tx:
                keys = vault.keys
                candidate = candidates.get_candidate(tx, candidate_id)
                if candidate["source_binding_id"] != body.binding_id:
                    raise HTTPException(403, "candidate belongs to another source binding")
                if body.decision == "confirm":
                    result = candidates.confirm_candidate(
                        tx,
                        keys.audit_hmac_key,
                        candidate_id,
                        parent_id=body.parent_id,
                        edited_content=body.edited_content,
                    )
                elif body.decision == "reject":
                    candidates.reject_candidate(tx, keys.audit_hmac_key, candidate_id)
                    result = {"status": "rejected"}
                elif body.decision == "snooze":
                    candidates.snooze_candidate(tx, keys.audit_hmac_key, candidate_id)
                    result = {"status": "snoozed"}
                else:
                    raise HTTPException(400, "decision must be confirm/reject/snooze")
        except (candidates.CandidateError, tree.TreeError) as exc:
            raise HTTPException(400, str(exc))
        if body.decision == "confirm" and "node_id" in result:
            _post_confirm_enrichment(app, result["node_id"])
        return result

    @app.post("/plugin-api/search", dependencies=plugin_auth)
    def tool_search(body: ToolSearchBody):
        _plugin_binding(body.binding_id)
        with vault.reading() as conn:
            return {"results": search.combined_search(conn, app.state.llm, body.q, body.limit)}

    @app.get("/plugin-api/nodes/{node_id}", dependencies=plugin_auth)
    async def tool_get_node(binding_id: str, node_id: str):
        _plugin_binding(binding_id)
        with vault.reading() as conn:
            try:
                node = dict(tree.get_node(conn, node_id))
            except tree.TreeError:
                raise HTTPException(404, f"Node not found: {node_id}")
            rev = tree.get_current_revision(conn, node_id)
            return {
                "node": node,
                "body": rev["body"] if rev else "",
                "revision_no": rev["revision_no"] if rev else 0,
                "path": tree.node_path(conn, node_id),
                "relations": relations.relations_of(conn, node_id),
            }

    @app.post("/plugin-api/query", dependencies=plugin_auth)
    def tool_query(body: ToolQueryBody):
        _plugin_binding(body.binding_id)
        with vault.reading() as conn:
            return qa.answer_question(conn, app.state.llm, body.question)

    @app.get("/plugin-api/status", dependencies=plugin_auth)
    async def tool_status(binding_id: str):
        _plugin_binding(binding_id)
        return vault.status()

    @app.post("/plugin-api/daily/run", dependencies=plugin_auth)
    def tool_daily(body: ToolDailyBody):
        """Invoked by the OpenClaw cron worker at 22:30. Returns a summary
        that never contains thought bodies; when locked, returns
        locked=true without processing (cursor untouched)."""
        _plugin_binding(body.binding_id)
        return daily.run_daily(vault, app.state.llm)

    @app.post("/plugin-api/dictation/close", dependencies=plugin_auth)
    def tool_dictation_close(body: ToolDictationCloseBody):
        """结束记录 arrived: the session is complete by the owner's own words.
        Persist the closure (so it survives restarts and a locked vault) and,
        when unlocked, merge the session right away instead of waiting out
        the cutoff quiet window."""
        _plugin_binding(body.binding_id)
        try:
            dictation.mark_closed(config.inbox_dir, body.binding_id, body.dictation_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        if not vault.is_unlocked:
            # The unlock-time backlog run will pick the closed session up.
            return {"ok": True, "processing": False, "locked": True}

        def _merge_now() -> None:
            try:
                daily.run_daily(vault, app.state.llm)
            except Exception:
                # run_daily already logs; the session stays sealed for retry.
                log.warning("dictation close: immediate merge run failed")

        threading.Thread(target=_merge_now, name="simai-dictation-merge", daemon=True).start()
        return {"ok": True, "processing": True}

    return app


def _process_backlog_safely(app: FastAPI) -> dict | None:
    """Section 15.3: after unlock, automatically process any sealed-inbox
    backlog. Failures are reported, never raised into the unlock flow."""
    from ..crypto.sealed_inbox import list_items

    vault: AppState = app.state.vault
    if not list_items(app.state.config.inbox_dir):
        return None
    try:
        return daily.run_daily(vault, app.state.llm)
    except Exception:
        log.warning("backlog processing after unlock failed; items retained")
        return {"failed": True}


def _post_confirm_enrichment(app: FastAPI, node_id: str) -> None:
    """After a formal write committed: refresh embedding and auto-relations.
    Best-effort - a model outage must not undo the user's confirmation."""
    vault: AppState = app.state.vault
    rel_cfg = app.state.config.section("relations")
    try:
        with vault.transaction() as tx:
            search.upsert_embedding(tx, app.state.llm, node_id)
        if rel_cfg.get("auto_generate", True):
            with vault.transaction() as tx:
                autorelations.generate_for_node(
                    tx,
                    app.state.llm,
                    vault.keys.audit_hmac_key,
                    node_id,
                    max_per_revision=int(rel_cfg.get("max_per_revision", 3)),
                    minimum_confidence=float(rel_cfg.get("minimum_confidence", 0.75)),
                )
    except Exception:
        log.warning("post-confirm enrichment failed node=%s", node_id)


def _check_unlock_rate_limit(failures: list[float], lock: threading.Lock) -> None:
    now = time.monotonic()
    with lock:
        failures[:] = [value for value in failures if now - value < 60]
        if len(failures) >= 5:
            raise HTTPException(429, "口令尝试过多，请一分钟后重试")


def _record_unlock_failure(failures: list[float], lock: threading.Lock) -> None:
    with lock:
        failures.append(time.monotonic())
