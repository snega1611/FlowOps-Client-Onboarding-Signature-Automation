from __future__ import annotations

import json
import os
import sqlite3

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from app import services as svc
from app.agent.graph import investigate
from app.config import Settings
from app.db import connect, init_db, rows
from app.documents import UnsupportedDocument
from app.integrations.gmail import build_gmail
from app.integrations.signing import build_signer, verify_callback
from app.llm import OllamaClient
from app.security import safe_eq
from app.states import InvalidTransition


class IntakeIn(BaseModel):
    email: str = Field(max_length=254)
    full_name: str | None = Field(None, max_length=120)
    company: str | None = Field(None, max_length=120)
    source: str = "form"


class WorkflowEventIn(BaseModel):
    event_id: str = Field(min_length=1, max_length=120)
    type: str = Field(max_length=60)
    case_id: int | None = None
    payload: dict = {}


class ActionIn(BaseModel):
    action: str
    params: dict = {}
    origin: str = "system"
    investigation_id: int | None = None


class DecisionIn(BaseModel):
    approve: bool


class SignatureCallbackIn(BaseModel):
    external_id: str
    status: str


def create_app(settings: Settings | None = None, gmail=None, signer=None, llm=None) -> FastAPI:
    settings = settings or Settings.from_env()
    init_db(settings.db_path)
    gmail = gmail or build_gmail(settings)
    signer = signer or build_signer(settings)
    if llm is None:
        c = OllamaClient(settings.ollama_url, settings.llm_model, settings.llm_timeout)
        llm = c if c.available() else None
    app = FastAPI(title="FlowOps AI", version="1.0")
    app.state.settings, app.state.gmail, app.state.signer, app.state.llm = settings, gmail, signer, llm

    def db():
        conn = connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def auth(x_flowops_secret: str = Header("")):
        if not safe_eq(x_flowops_secret, settings.webhook_secret):
            raise HTTPException(401, "invalid or missing X-FlowOps-Secret")

    def approver(authorization: str = Header("")) -> str:
        tok = authorization.removeprefix("Bearer ").strip()
        name = next((n for t, n in settings.approver_tokens.items() if safe_eq(t, tok)), None) if tok else None
        if not name:
            raise HTTPException(401, "approver authentication required")
        return name

    def guard(fn):
        try:
            return fn()
        except svc.NotFound as e:
            raise HTTPException(404, f"not found: {e}") from e
        except (ValueError, UnsupportedDocument) as e:
            raise HTTPException(422, str(e)) from e
        except InvalidTransition as e:
            raise HTTPException(409, str(e)) from e
        except svc.ApprovalError as e:
            raise HTTPException({"replay": 409, "expired": 410, "self_approval": 403}[e.code], str(e)) from e

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/ready")
    def ready(conn=Depends(db)):
        conn.execute("SELECT 1")
        return {"status": "ready", "gmail_adapter": gmail.label, "signing_provider": signer.provider,
                "llm": "ollama" if llm else "unavailable (deterministic fallback)"}

    @app.post("/intake", dependencies=[Depends(auth)], status_code=201)
    def post_intake(body: IntakeIn, conn=Depends(db)):
        return guard(lambda: svc.intake(conn, settings, gmail, body.email, body.full_name, body.company, body.source))

    @app.post("/gmail/messages", dependencies=[Depends(auth)])
    def gmail_message(raw: dict, conn=Depends(db)):
        return svc.handle_gmail_message(conn, settings, gmail, raw)

    @app.get("/cases", dependencies=[Depends(auth)])
    def list_cases(state: str | None = None, conn=Depends(db)):
        q = "SELECT c.id,c.state,c.updated_at,cl.email,cl.full_name FROM cases c JOIN clients cl ON cl.id=c.client_id"
        return rows(conn.execute(q + (" WHERE c.state=?" if state else "") + " ORDER BY c.id DESC",
                                 (state,) if state else ()))

    @app.get("/cases/{case_id}", dependencies=[Depends(auth)])
    def get_case(case_id: int, conn=Depends(db)):
        def f():
            c = svc.get_case(conn, case_id)
            fields = rows(conn.execute("SELECT f.* FROM extracted_fields f JOIN documents d ON d.id=f.document_id "
                                       "WHERE d.case_id=?", (case_id,)))
            docs = rows(conn.execute("SELECT id,doc_type,filename,status,ocr_confidence,issues,created_at FROM documents "
                                     "WHERE case_id=?", (case_id,)))
            for d in docs:
                d["issues"] = json.loads(d["issues"] or "[]")
            return {**c, "requirements": svc.get_reqs(conn, case_id), "documents": docs, "extracted_fields": fields,
                    "signatures": rows(conn.execute("SELECT * FROM signature_requests WHERE case_id=?", (case_id,))),
                    "communications": rows(conn.execute("SELECT * FROM communications WHERE case_id=? ORDER BY id",
                                                        (case_id,))),
                    "messages": rows(conn.execute("SELECT * FROM gmail_messages WHERE case_id=? ORDER BY id", (case_id,))),
                    "investigations": rows(conn.execute("SELECT * FROM investigations WHERE case_id=? ORDER BY id DESC",
                                                        (case_id,)))}
        return guard(f)

    @app.post("/cases/{case_id}/documents", dependencies=[Depends(auth)], status_code=201)
    async def upload(case_id: int, doc_type: str = Form(...), file: UploadFile = File(...), conn=Depends(db)):
        data = await file.read(settings.max_upload_bytes + 1)
        return guard(lambda: svc.add_document(conn, settings, gmail, case_id, doc_type, file.filename or "upload", data))

    @app.post("/cases/{case_id}/documents/{doc_id}/review")
    def review(case_id: int, doc_id: int, body: DecisionIn, who: str = Depends(approver), conn=Depends(db)):
        return guard(lambda: svc.resolve_review(conn, case_id, doc_id, body.approve, who))

    @app.post("/cases/{case_id}/signature", dependencies=[Depends(auth)], status_code=201)
    def start_signature(case_id: int, conn=Depends(db)):
        return guard(lambda: svc.initiate_signature(conn, settings, gmail, signer, case_id))

    @app.post("/signing/callback")
    async def signing_callback(request: Request, x_signature: str = Header(""), conn=Depends(db)):
        raw = await request.body()
        if not verify_callback(settings.signing_secret, raw, x_signature):
            raise HTTPException(401, "invalid callback signature")
        body = SignatureCallbackIn(**json.loads(raw))
        return guard(lambda: svc.apply_signature_status(conn, settings, gmail, body.external_id, body.status))

    @app.post("/signing/reconcile", dependencies=[Depends(auth)])
    def signing_reconcile(conn=Depends(db)):
        return {"results": svc.reconcile_signatures(conn, settings, signer)}

    @app.post("/cases/{case_id}/complete", dependencies=[Depends(auth)])
    def complete(case_id: int, conn=Depends(db)):
        return guard(lambda: svc.complete_case(conn, settings, gmail, case_id))

    @app.post("/workflow/events", dependencies=[Depends(auth)])
    def workflow_event(body: WorkflowEventIn, conn=Depends(db)):
        return svc.record_event(conn, body.event_id, body.type, body.case_id, body.payload)

    @app.post("/followups/run", dependencies=[Depends(auth)])
    def followups(conn=Depends(db)):
        return svc.run_followups(conn, settings, gmail)

    @app.post("/stalled/detect", dependencies=[Depends(auth)])
    def stalled(conn=Depends(db)):
        return {"stalled_case_ids": svc.detect_stalled(conn, settings)}

    @app.post("/reconcile/run", dependencies=[Depends(auth)])
    def reconcile(conn=Depends(db)):
        return {"communications": svc.reconcile_communications(conn, settings, gmail),
                "signatures": svc.reconcile_signatures(conn, settings, gmail, signer)}

    @app.post("/cases/{case_id}/investigate", dependencies=[Depends(auth)])
    def investigate_case(case_id: int, conn=Depends(db)):
        return guard(lambda: (svc.get_case(conn, case_id), investigate(conn, settings, case_id, llm))[1])

    @app.post("/cases/{case_id}/actions", dependencies=[Depends(auth)])
    def run_action(case_id: int, body: ActionIn, conn=Depends(db)):
        return guard(lambda: svc.execute_action(conn, settings, gmail, signer, case_id, body.action, body.params,
                                                actor="agent", origin=body.origin,
                                                investigation_id=body.investigation_id))

    @app.post("/cases/{case_id}/recover", dependencies=[Depends(auth)])
    def recover(case_id: int, conn=Depends(db)):
        """Investigate, then let the deterministic policy engine decide the agent's recommendation."""
        def f():
            inv = investigate(conn, settings, case_id, llm)
            act = svc.execute_action(conn, settings, gmail, signer, case_id, inv["recommended_action"], {},
                                     actor="agent", investigation_id=inv["investigation_id"])
            return {"investigation": inv, "action": act}
        return guard(f)

    @app.get("/approvals", dependencies=[Depends(auth)])
    def approvals(status: str | None = None, conn=Depends(db)):
        return rows(conn.execute("SELECT * FROM approvals" + (" WHERE status=?" if status else "") + " ORDER BY id DESC",
                                 (status,) if status else ()))

    @app.post("/approvals/{approval_id}/decision")
    def decision(approval_id: int, body: DecisionIn, who: str = Depends(approver), conn=Depends(db)):
        return guard(lambda: svc.decide_approval(conn, settings, gmail, signer, approval_id, who, body.approve))

    @app.get("/integrations/history", dependencies=[Depends(auth)])
    def integrations(conn=Depends(db)):
        return {"communications": rows(conn.execute("SELECT * FROM communications ORDER BY id DESC LIMIT 200")),
                "signatures": rows(conn.execute("SELECT * FROM signature_requests ORDER BY id DESC LIMIT 100")),
                "events": rows(conn.execute("SELECT * FROM workflow_events ORDER BY id DESC LIMIT 200"))}

    @app.get("/audit", dependencies=[Depends(auth)])
    def audit(case_id: int | None = None, conn=Depends(db)):
        q = "SELECT * FROM audit_events" + (" WHERE case_id=?" if case_id else "") + " ORDER BY id DESC LIMIT 500"
        return rows(conn.execute(q, (case_id,) if case_id else ()))

    @app.get("/evaluation/latest", dependencies=[Depends(auth)])
    def evaluation():
        p = "reports/evaluation.json"
        if not os.path.exists(p):
            raise HTTPException(404, "no evaluation report yet: run `python -m evaluation.run`")
        return json.load(open(p))

    @app.exception_handler(sqlite3.OperationalError)
    async def db_error(_, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "database temporarily unavailable"}, status_code=503)

    return app


def get_app() -> FastAPI:  # uvicorn --factory app.main:get_app
    return create_app()
