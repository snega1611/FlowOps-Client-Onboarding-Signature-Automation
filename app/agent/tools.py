"""Read-only investigation tools. Explicit allow-list, validated args, bounded call count.
There is deliberately no SQL, shell, HTTP or write tool here."""
from __future__ import annotations

import json

from pydantic import BaseModel, Field

from app import services as svc
from app.db import one, rows

BLOCKERS = ["missing_client_info", "missing_documents", "document_validation_failed", "document_conflict",
            "awaiting_signature", "signature_provider_issue", "email_delivery_failure",
            "uncertain_external_outcome", "prompt_injection_detected", "unknown"]
ACTION_FOR = {"missing_client_info": "request_missing_info", "missing_documents": "send_reminder",
              "document_validation_failed": "request_correction", "document_conflict": "escalate_to_human",
              "awaiting_signature": "send_reminder", "signature_provider_issue": "reconcile_signature",
              "email_delivery_failure": "reconcile_email", "uncertain_external_outcome": "reconcile_email",
              "prompt_injection_detected": "escalate_to_human", "unknown": "escalate_to_human"}


class CaseArgs(BaseModel):
    case_id: int = Field(gt=0)


class ToolBudgetExceeded(RuntimeError):
    pass


class Toolbox:
    def __init__(self, conn, max_calls: int = 12):
        self.conn, self.max_calls, self.calls = conn, max_calls, 0
        self.registry = {
            "get_case_details": self.get_case_details,
            "get_outstanding_requirements": self.get_outstanding_requirements,
            "get_document_validation_results": self.get_document_validation_results,
            "get_recent_workflow_events": self.get_recent_workflow_events,
            "get_integration_status": self.get_integration_status,
            "get_communication_history": self.get_communication_history,
        }

    def call(self, name: str, args: dict) -> dict:
        if name not in self.registry:
            raise ValueError(f"tool '{name}' is not permitted")
        if self.calls >= self.max_calls:
            raise ToolBudgetExceeded("tool call limit reached")
        self.calls += 1
        a = CaseArgs(**args)
        return self.registry[name](a.case_id)

    def get_case_details(self, case_id):
        c = svc.get_case(self.conn, case_id)
        msgs = rows(self.conn.execute("SELECT id,sender,subject,status,injection_flag FROM gmail_messages "
                                      "WHERE case_id=? ORDER BY id DESC LIMIT 5", (case_id,)))
        return {"case": {k: c[k] for k in ("id", "state", "email", "source", "updated_at", "created_at")},
                "inbound_messages": msgs}

    def get_outstanding_requirements(self, case_id):
        return {"requirements": [r for r in svc.get_reqs(self.conn, case_id)
                                 if r["status"] not in ("satisfied", "waived")]}

    def get_document_validation_results(self, case_id):
        docs = rows(self.conn.execute("SELECT id,doc_type,status,issues,ocr_confidence FROM documents WHERE case_id=?",
                                      (case_id,)))
        for d in docs:
            d["issues"] = json.loads(d["issues"] or "[]")
        return {"documents": docs}

    def get_recent_workflow_events(self, case_id):
        return {"events": rows(self.conn.execute(
            "SELECT id,type,payload,received_at FROM workflow_events WHERE case_id=? ORDER BY id DESC LIMIT 10",
            (case_id,)))}

    def get_integration_status(self, case_id):
        return {"signatures": rows(self.conn.execute(
            "SELECT id,provider,status,sandbox,verified,last_checked_at FROM signature_requests WHERE case_id=?",
            (case_id,)))}

    def get_communication_history(self, case_id):
        return {"communications": rows(self.conn.execute(
            "SELECT id,purpose,status,attempt,error,updated_at FROM communications WHERE case_id=? ORDER BY id DESC "
            "LIMIT 10", (case_id,)))}


def classify_blocker(ev: dict) -> tuple[str, list[str], str, float]:
    """Deterministic classifier. Returns (blocker, evidence_ids, explanation, confidence)."""
    reqs = ev["requirements"]["requirements"]
    docs = ev["documents"]["documents"]
    comms = ev["communications"]["communications"]
    sigs = ev["integration"]["signatures"]
    msgs = ev["case"]["inbound_messages"]
    inj = [f"msg:{m['id']}" for m in msgs if m["injection_flag"]]
    if inj:
        return "prompt_injection_detected", inj, "inbound message contained instruction-like content", 0.9
    unc = [f"comm:{c['id']}" for c in comms if c["status"] in ("uncertain", "pending")]
    if unc:
        return "uncertain_external_outcome", unc, "an email send outcome is unconfirmed", 0.9
    failed = [f"comm:{c['id']}" for c in comms if c["status"] == "failed"]
    if failed:
        return "email_delivery_failure", failed, "outbound email failed", 0.85
    conflict = [d for d in docs if d["status"] == "needs_review"]
    if conflict:
        return "document_conflict", [f"doc:{d['id']}" for d in conflict], "document needs human review", 0.9
    bad = [d for d in docs if d["status"] in ("needs_correction", "failed")]
    if bad:
        return "document_validation_failed", [f"doc:{d['id']}" for d in bad], "document failed validation", 0.9
    pend = {r["kind"]: r for r in reqs if r["status"] == "pending"}
    stuck_sig = [s for s in sigs if s["status"] in ("sent", "created", "viewed")]
    if "info" in pend:
        return "missing_client_info", [f"req:{r['id']}" for r in reqs if r["kind"] == "info"], \
            "client information is missing", 0.9
    if "document" in pend:
        return "missing_documents", [f"req:{r['id']}" for r in reqs if r["kind"] == "document"], \
            "required documents not received", 0.9
    if "signature" in pend:
        if stuck_sig:
            return "awaiting_signature", [f"sig:{s['id']}" for s in stuck_sig], \
                "signature request sent but not completed", 0.8
        return "signature_provider_issue", [f"req:{pend['signature']['id']}"], \
            "signature is pending but no active signature request exists", 0.7
    return "unknown", [], "no known blocker pattern", 0.3


def evidence_ids(ev: dict) -> set[str]:
    ids = {f"req:{r['id']}" for r in ev["requirements"]["requirements"]}
    ids |= {f"doc:{d['id']}" for d in ev["documents"]["documents"]}
    ids |= {f"comm:{c['id']}" for c in ev["communications"]["communications"]}
    ids |= {f"sig:{s['id']}" for s in ev["integration"]["signatures"]}
    ids |= {f"msg:{m['id']}" for m in ev["case"]["inbound_messages"]}
    ids |= {f"event:{e['id']}" for e in ev["events"]["events"]}
    return ids


def last_investigation(conn, case_id):
    return one(conn, "SELECT * FROM investigations WHERE case_id=? ORDER BY id DESC", (case_id,))
