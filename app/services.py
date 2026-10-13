"""Business rules and persistence. Python is the single source of truth; n8n only orchestrates."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import timedelta

from app import documents as docs
from app.db import dumps, hours_ago, iso, now, one, parse_iso, rows
from app.integrations.gmail import MalformedMessage, parse_message
from app.integrations.signing import RANK
from app.integrations.letter import generate_engagement_letter
from app.policy import SENSITIVE_ACTIONS, decide, is_overdue
from app.retry import AuthExpiredError, IntegrationError, TransientError, UncertainOutcomeError, with_backoff
from app.security import detect_injection, mask_pii
from app.states import TERMINAL, CaseState, InvalidTransition, check_transition

WORKING = ("AWAITING_INFO", "AWAITING_DOCUMENTS", "DOCUMENT_REVIEW", "AWAITING_SIGNATURE")
REQ_DEFAULTS = [("info", "info:full_name"), ("info", "info:company"), ("document", "doc:government_id"),
                ("document", "doc:proof_of_address"), ("signature", "signature:engagement_letter")]


class ApprovalError(Exception):
    def __init__(self, code: str, msg: str):
        super().__init__(msg)
        self.code = code


class NotFound(Exception):
    pass


# ---------- helpers ----------
def audit(conn, case_id, actor, event, detail=None, corr=None):
    conn.execute("INSERT INTO audit_events(case_id,actor,event,detail,correlation_id,created_at) VALUES(?,?,?,?,?,?)",
                 (case_id, actor, event, mask_pii(dumps(detail or {})), corr, iso()))


def get_case(conn, case_id) -> dict:
    c = one(conn, "SELECT c.*, cl.email, cl.full_name, cl.company FROM cases c JOIN clients cl ON cl.id=c.client_id "
                  "WHERE c.id=?", (case_id,))
    if not c:
        raise NotFound(f"case {case_id}")
    return c


def get_reqs(conn, case_id) -> list[dict]:
    return rows(conn.execute("SELECT * FROM requirements WHERE case_id=? ORDER BY id", (case_id,)))


def touch(conn, case_id):
    conn.execute("UPDATE cases SET updated_at=? WHERE id=?", (iso(), case_id))


def transition(conn, case_id, new: CaseState, actor="system", reason=""):
    c = get_case(conn, case_id)
    if c["state"] == new.value:
        return c["state"]
    check_transition(c["state"], new.value)
    closed = iso() if new in TERMINAL else None
    conn.execute("UPDATE cases SET state=?, updated_at=?, closed_at=COALESCE(?,closed_at) WHERE id=?",
                 (new.value, iso(), closed, case_id))
    audit(conn, case_id, actor, "state_transition", {"from": c["state"], "to": new.value, "reason": reason},
          c["correlation_id"])
    conn.commit()
    return new.value



def recompute_state(conn, case_id, actor="system") -> str:
    c = get_case(conn, case_id)
    if CaseState(c["state"]) in TERMINAL or c["state"] == "NEEDS_HUMAN":
        return c["state"]

    reqs = [
        r for r in get_reqs(conn, case_id)
        if r["status"] not in ("satisfied", "waived")
    ]
    kinds = {(r["kind"], r["status"]) for r in reqs}

    if any(k == "info" for k, _ in kinds):
        target = CaseState.AWAITING_INFO
    elif ("document", "under_review") in kinds:
        target = CaseState.DOCUMENT_REVIEW
    elif any(k == "document" for k, _ in kinds):
        target = CaseState.AWAITING_DOCUMENTS
    elif any(k == "signature" for k, _ in kinds):
        target = CaseState.AWAITING_SIGNATURE
    else:
        target = CaseState.SIGNED

    try:
        return transition(
            conn, case_id, target, actor, "recomputed from requirements"
        )
    except InvalidTransition:
        return c["state"]



def idem_get(conn, key):
    r = one(conn, "SELECT result FROM idempotency WHERE key=?", (key,))
    return json.loads(r["result"]) if r else None


def idem_put(conn, key, result):
    conn.execute("INSERT OR IGNORE INTO idempotency(key,result,created_at) VALUES(?,?,?)", (key, dumps(result), iso()))
    conn.commit()


# ---------- intake ----------
def intake(conn, settings, gmail, email, full_name=None, company=None, source="form", message_id=None,
           thread_id=None) -> dict:
    email = email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("invalid email")
    key = f"intake:{message_id}" if message_id else None
    if key and (prev := idem_get(conn, key)):
        return {**prev, "duplicate": True}
    cl = one(conn, "SELECT * FROM clients WHERE email=?", (email,))
    if not cl:
        conn.execute("INSERT INTO clients(email,full_name,company,created_at) VALUES(?,?,?,?)",
                     (email, full_name, company, iso()))
        cl = one(conn, "SELECT * FROM clients WHERE email=?", (email,))
    else:
        conn.execute("UPDATE clients SET full_name=COALESCE(?,full_name), company=COALESCE(?,company) WHERE id=?",
                     (full_name, company, cl["id"]))
    case = one(conn, "SELECT * FROM cases WHERE client_id=? AND state NOT IN ('COMPLETED','CLOSED') "
                     "ORDER BY id DESC", (cl["id"],))
    created = case is None
    if created:
        corr = str(uuid.uuid4())
        cur = conn.execute("INSERT INTO cases(client_id,state,correlation_id,source,thread_id,created_at,updated_at)"
                           " VALUES(?,?,?,?,?,?,?)", (cl["id"], "NEW", corr, source, thread_id, iso(), iso()))
        cid = cur.lastrowid
        due = iso(now() + timedelta(hours=settings.reminder_interval_hours * settings.max_reminders))
        for kind, name in REQ_DEFAULTS:
            conn.execute("INSERT INTO requirements(case_id,kind,name,status,due_at,created_at,updated_at) "
                         "VALUES(?,?,?,?,?,?,?)", (cid, kind, name, "pending", due, iso(), iso()))
        audit(conn, cid, "intake", "case_created", {"source": source}, corr)
    
    else:
        cid = case["id"]
        audit(
            conn,
            cid,
            "intake",
            "existing_case_reused",
            {"source": source},
            case["correlation_id"],
        )

    client = one(conn, "SELECT * FROM clients WHERE id=?", (cl["id"],))
    for name, val in (("info:full_name", client["full_name"]), ("info:company", client["company"])):
        if val:
            conn.execute("UPDATE requirements SET status='satisfied', updated_at=? WHERE case_id=? AND name=? "
                         "AND status='pending'", (iso(), cid, name))
    conn.commit()
    state = recompute_state(conn, cid, "intake")
    ack = None
    if created:
        missing = [r["name"] for r in get_reqs(conn, cid) if r["status"] == "pending"]
        
        ack = send_email(
            conn,
            settings,
            gmail,
            cid,
            "acknowledgement",
            f"ack:{cid}",
            f"We've received your documents, {client['full_name'] or 'there'}",
            (
                f"Hi {client['full_name'] or 'there'},\n\n"
                "Thank you for submitting your onboarding details and documents. "
                "We've received your submission and will review the information "
                "shortly.\n\n"
                "We'll get back to you once the review is complete, or contact you "
                "if we need any additional information.\n\n"
                "Thank you for your patience.\n\n"
                "Best regards,\n"
                "Onboarding Team"
            ),
        )

    
    out = {
        "case_id": cid,
        "created": created,
        "state": state,
        "acknowledgement": ack["status"] if ack else None,
        "duplicate": not created,
        "reused_existing_case": not created,
    }

    if key:
        idem_put(conn, key, out)
    return out


def handle_gmail_message(conn, settings, gmail, raw: dict) -> dict:
    try:
        m = parse_message(raw)
    except MalformedMessage as e:
        audit(conn, None, "gmail", "malformed_message", {"error": str(e)})
        conn.commit()
        return {"status": "malformed", "error": str(e)}
    if one(conn, "SELECT 1 FROM gmail_messages WHERE message_id=?", (m.message_id,)):
        return {"status": "duplicate", "message_id": m.message_id}
    if m.sender == settings.sender_address.lower():
        return {"status": "ignored_own_message"}
    inj = detect_injection(m.subject + "\n" + m.body)
    case_id, status = None, "ignored"
    by_thread = one(conn, "SELECT c.id, cl.email FROM cases c JOIN clients cl ON cl.id=c.client_id "
                          "WHERE c.thread_id=? AND c.thread_id!=''", (m.thread_id,)) if m.thread_id else None
    open_cases = rows(conn.execute(
        "SELECT c.id FROM cases c JOIN clients cl ON cl.id=c.client_id WHERE cl.email=? "
        "AND c.state NOT IN ('COMPLETED','CLOSED')", (m.sender,)))
    if by_thread and by_thread["email"] != m.sender:
        status = "ambiguous_identity"
        case_id = by_thread["id"]
    elif by_thread:
        case_id, status = by_thread["id"], "associated"
    elif len(open_cases) == 1:
        case_id, status = open_cases[0]["id"], "associated"
    elif len(open_cases) > 1:
        status = "ambiguous_case"
    elif re.search(r"onboard|new client|engagement|sign ?up", m.subject, re.I):
        r = intake(conn, settings, gmail, m.sender, source="gmail", message_id=m.message_id, thread_id=m.thread_id)
        case_id, status = r["case_id"], "case_created"
    conn.execute("INSERT INTO gmail_messages(message_id,thread_id,case_id,sender,subject,direction,received_at,"
                 "injection_flag,status,attachment_count,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (m.message_id, m.thread_id, case_id, m.sender, m.subject, "inbound", m.received_at,
                  1 if inj else 0, status, len(m.attachments), iso()))
    if case_id:
        if m.thread_id:
            conn.execute("UPDATE cases SET thread_id=COALESCE(NULLIF(thread_id,''),?) WHERE id=?", (m.thread_id, case_id))
        if status == "associated":
            touch(conn, case_id)
            recompute_state(conn, case_id, "gmail")
    if inj:
        audit(conn, case_id, "security", "prompt_injection_detected", {"message_id": m.message_id, "patterns": inj})
    if status.startswith("ambiguous") and case_id:
        _escalate(conn, case_id, f"gmail {status}")
    conn.commit()
    return {"status": status, "case_id": case_id, "injection_flag": bool(inj),
            "attachments": [a.filename for a in m.attachments]}


def _escalate(conn, case_id, reason):
    try:
        transition(conn, case_id, CaseState.NEEDS_HUMAN, "system", reason)
    except InvalidTransition:
        pass


# ---------- email ----------

def send_email(
    conn,
    settings,
    gmail,
    case_id,
    purpose,
    key,
    subject,
    body,
    sleep=None,
    force_resend=False,
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> dict:
    case = get_case(conn, case_id)

    c = one(
        conn,
        "SELECT * FROM communications WHERE idem_key=?",
        (key,),
    )

    attempt0 = 0

    if c:
        if not force_resend:
            if c["status"] == "sent":
                return {
                    "status": "duplicate_prevented",
                    "comm_id": c["id"],
                }

            if c["status"] in ("uncertain", "pending"):
                r = reconcile_comm(conn, settings, gmail, c)

                if r != "not_sent":
                    return {
                        "status": r,
                        "comm_id": c["id"],
                    }

        attempt0 = c["attempt"]

        conn.execute(
            """
            UPDATE communications
            SET status='pending', updated_at=?
            WHERE id=?
            """,
            (iso(), c["id"]),
        )

    else:
        cur = conn.execute(
            """
            INSERT INTO communications(
                case_id, purpose, recipient, idem_key, status,
                attempt, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                case_id,
                purpose,
                case["email"],
                key,
                "pending",
                0,
                iso(),
                iso(),
            ),
        )

        c = {"id": cur.lastrowid, "attempt": 0}
        attempt0 = 0

    conn.commit()

    kw = {"sleep": sleep} if sleep else {}

    status, err, res, tries = "failed", None, None, 1

    try:
        res, tries = with_backoff(
            lambda: gmail.send(
                case["email"],
                subject,
                body,
                key,
                case["thread_id"],
                attachments=attachments,
            ),
            settings.send_max_attempts,
            settings.backoff_base_seconds,
            **kw,
        )
        status = "sent"

    except UncertainOutcomeError as e:
        status, err = "uncertain", str(e)

    except AuthExpiredError as e:
        status, err = "failed", "auth_expired: re-authorise Gmail"

        record_event(
            conn,
            f"auth-{uuid.uuid4()}",
            "integration_failure",
            case_id,
            {
                "integration": "gmail",
                "error": "auth_expired",
                "detail": str(e),
            },
        )

    except (TransientError, IntegrationError) as e:
        status = "failed"
        err = f"{type(e).__name__}: {e}"
        tries = settings.send_max_attempts

    conn.execute(
        """
        UPDATE communications
        SET status=?, attempt=?, message_id=?, thread_id=?,
            error=?, updated_at=?
        WHERE id=?
        """,
        (
            status,
            attempt0 + tries,
            res and res.message_id,
            res and res.thread_id,
            err,
            iso(),
            c["id"],
        ),
    )

    if res and res.thread_id:
        conn.execute(
            """
            UPDATE cases
            SET thread_id=COALESCE(NULLIF(thread_id,''),?)
            WHERE id=?
            """,
            (res.thread_id, case_id),
        )

    audit(
        conn,
        case_id,
        "gmail",
        f"email_{status}",
        {
            "purpose": purpose,
            "key": key,
            "force_resend": force_resend,
        },
    )

    conn.commit()

    return {
        "status": status,
        "comm_id": c["id"],
        "error": err,
    }


def reconcile_comm(conn, settings, gmail, c: dict, force: bool = False) -> str:
    """Returns 'sent' (confirmed in Sent), 'in_progress' (too early to tell) or 'not_sent' (safe to resend)."""
    try:
        found = gmail.find_sent(c["idem_key"])
    except IntegrationError as e:
        return "in_progress" if not force else f"lookup_failed:{e}"
    if found:
        conn.execute("UPDATE communications SET status='sent', message_id=?, thread_id=?, error=NULL, updated_at=? "
                     "WHERE id=?", (found.message_id, found.thread_id, iso(), c["id"]))
        audit(conn, c["case_id"], "reconciler", "email_reconciled_as_sent", {"key": c["idem_key"]})
        conn.commit()
        return "sent"
    age = (now() - parse_iso(c["updated_at"])).total_seconds()
    if age < settings.reconcile_min_age_seconds and not force:
        return "in_progress"
    conn.execute("UPDATE communications SET status='failed', error='confirmed not in Sent', updated_at=? WHERE id=?",
                 (iso(), c["id"]))
    audit(conn, c["case_id"], "reconciler", "email_confirmed_not_sent", {"key": c["idem_key"]})
    conn.commit()
    return "not_sent"


def reconcile_communications(conn, settings, gmail, case_id=None, force=False) -> list[dict]:
    q = "SELECT * FROM communications WHERE status IN ('uncertain','pending')"
    args: tuple = ()
    if case_id:
        q, args = q + " AND case_id=?", (case_id,)
    return [{"comm_id": c["id"], "result": reconcile_comm(conn, settings, gmail, c, force)}
            for c in rows(conn.execute(q, args))]


# ---------- documents ----------
DOC_REQ = {"government_id": "doc:government_id", "proof_of_address": "doc:proof_of_address"}


def add_document(conn, settings, gmail, case_id, doc_type, filename, data: bytes) -> dict:
    case = get_case(conn, case_id)
    if CaseState(case["state"]) in TERMINAL:
        raise ValueError("case is closed")
    ext = docs.check_upload(filename, data, settings.max_upload_bytes)
    sha = hashlib.sha256(data).hexdigest()
    
    prev = one(
        conn,
        """
        SELECT id, status, path
        FROM documents
        WHERE case_id = ? AND sha256 = ? AND doc_type = ?
        """,
        (case_id, sha, doc_type),
    )

    if prev:
        # Already processed successfully or awaiting review: do not process again.
        if prev["status"] != "failed":
            return {
                "document_id": prev["id"],
                "status": prev["status"],
                "duplicate": True,
                "retried": False,
            }

        # Retry extraction using the previously stored file.
        try:
            ex = docs.process_file(
                prev["path"],
                doc_type,
                case["full_name"],
            )
        except docs.OCRUnavailable as e:
            ex = docs.Extraction(
                doc_type,
                "failed",
                [],
                [{
                    "type": "ocr_unavailable",
                    "detail": str(e)[:120],
                }],
                0.0,
                "",
            )

        conn.execute(
            """
            UPDATE documents
            SET status = ?,
                ocr_confidence = ?,
                issues = ?,
                text_excerpt = ?
            WHERE id = ?
            """,
            (
                ex.status,
                ex.ocr_confidence,
                dumps(ex.issues),
                mask_pii(ex.text[:300]),
                prev["id"],
            ),
        )
        conn.commit()

        return {
            "document_id": prev["id"],
            "status": ex.status,
            "duplicate": True,
            "retried": True,
        }

    os.makedirs(settings.upload_dir, exist_ok=True)
    path = os.path.join(settings.upload_dir, f"{case_id}_{sha[:16]}{ext}")  # never trust client filename for path
    with open(path, "wb") as f:
        f.write(data)
    try:
        ex = docs.process_file(path, doc_type, case["full_name"])
    except docs.OCRUnavailable as e:
        ex = docs.Extraction(doc_type, "failed", [], [{"type": "ocr_unavailable", "detail": str(e)[:120]}], 0.0, "")
    req = one(conn, "SELECT id FROM requirements WHERE case_id=? AND name=?", (case_id, DOC_REQ[doc_type]))
    cur = conn.execute("INSERT INTO documents(case_id,requirement_id,doc_type,filename,sha256,path,status,"
                       "ocr_confidence,issues,text_excerpt,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (case_id, req["id"], doc_type, os.path.basename(filename)[:120], sha, path, ex.status,
                        ex.ocr_confidence, dumps(ex.issues), mask_pii(ex.text[:300]), iso()))
    did = cur.lastrowid
    for f in ex.fields:
        conn.execute("INSERT INTO extracted_fields(document_id,name,raw_value,normalized_value,confidence,evidence,"
                     "status) VALUES(?,?,?,?,?,?,?)", (did, f.name, f.raw, f.normalized, f.confidence, f.evidence,
                                                      f.status))
    new_req = {
        "validated": "satisfied",
        "needs_correction": "needs_correction",
        "needs_review": "needs_correction",
        "failed": "needs_correction",
    }[ex.status]
    conn.execute("UPDATE requirements SET status=?, detail=?, updated_at=? WHERE id=?",
                 (new_req, dumps(ex.issues), iso(), req["id"]))
    touch(conn, case_id)
    audit(conn, case_id, "documents", "document_processed", {"doc": did, "status": ex.status, "issues": ex.issues},
          case["correlation_id"])
    conn.commit()
    recompute_state(conn, case_id, "documents")
    
    if ex.status in ("needs_correction", "failed", "needs_review"):
        issue_types = {
            i["type"] + (":" + i["field"] if "field" in i else "")
            for i in ex.issues
        }

        instructions = {
            "missing_field": (
                "Please upload a clear, complete document that displays "
                "the missing information. For proof of address, ensure your "
                "residential address is visible on the document."
            ),
            "name_mismatch": (
                "The name on your document does not match the name provided "
                "during onboarding. Please check the name and upload the correct document."
            ),
            "field_mismatch": (
                "The information on the document does not match the details "
                "provided. Please check the details and upload a corrected document."
            ),
            "low_confidence": (
                "We could not reliably read some information. Please upload "
                "a clearer image or scan showing the full document."
            ),
            "ocr_unavailable": (
                "We could not process your document automatically. Please "
                "resubmit a clear copy. If the problem continues, our onboarding "
                "team will review it."
            ),
        }

        guidance = []
        for issue in sorted(issue_types):
            issue_type, _, field = issue.partition(":")
            instruction = instructions.get(
                issue_type,
                "We could not verify one or more document requirements. "
                "Please review the document and resubmit it, or contact "
                "the onboarding team for assistance."
            )

            if issue_type == "missing_field" and field:
                readable_field = field.replace("_", " ")
                instruction = (
                    f"The document appears to be missing {readable_field}. "
                    + instruction
                )

            guidance.append(f"- {instruction}")

        body = (
            "Hello,\n\n"
            f"We could not accept your {doc_type.replace('_', ' ')} yet.\n\n"
            "What needs attention:\n"
            + "\n".join(guidance)
            + "\n\nPlease correct the issue and resubmit your document "
            "through the onboarding process. If you believe the document "
            "already contains the required information, please contact "
            "the onboarding team for assistance.\n\n"
            "Best regards,\n"
            "Onboarding Team"
        )

        send_email(
            conn,
            settings,
            gmail,
            case_id,
            "correction_request",
            f"correction:{did}",
            "Action required: Please correct your document",
            body,
        )

    return {"document_id": did, "status": ex.status, "issues": ex.issues, "duplicate": False}


def resolve_review(conn, case_id, document_id, accept: bool, approver: str) -> dict:
    d = one(conn, "SELECT * FROM documents WHERE id=? AND case_id=?", (document_id, case_id))
    if not d or d["status"] != "needs_review":
        raise ValueError("document not awaiting review")
    conn.execute("UPDATE documents SET status=? WHERE id=?", ("validated" if accept else "needs_correction", document_id))
    conn.execute("UPDATE requirements SET status=?, updated_at=? WHERE id=?",
                 ("satisfied" if accept else "needs_correction", iso(), d["requirement_id"]))
    audit(conn, case_id, approver, "document_review_resolved", {"document": document_id, "accepted": accept})
    conn.commit()
    return {"state": recompute_state(conn, case_id, approver)}


# ---------- signatures ----------
LETTER = ("ENGAGEMENT LETTER\nClient: {name}\nCompany: {company}\nDate: {date}\n\n"
          "This letter confirms the terms of engagement. Signature is required via the e-signature provider.\n")


def initiate_signature(conn, settings, gmail, signer, case_id) -> dict:
    case = get_case(conn, case_id)

    open_prior = [
        r for r in get_reqs(conn, case_id)
        if r["kind"] != "signature"
        and r["status"] not in ("satisfied", "waived")
    ]
    if open_prior:
        raise ValueError("prerequisite requirements are not complete")

 
    if ex := one(
        conn,
        "SELECT * FROM signature_requests WHERE case_id=?",
        (case_id,),
    ):
        # Recover the signing URL for an existing OpenSign document.
        if not ex["signing_url"]:
            try:
                recovered_url = signer.get_signing_url(
                    ex["external_id"],
                    case["email"],
                )
            except IntegrationError as e:
                audit(
                    conn,
                    case_id,
                    "signing",
                    "signing_url_recovery_failed",
                    {
                        "external_id": ex["external_id"],
                        "error": str(e),
                    },
                )
                conn.commit()
                recovered_url = None

            if recovered_url:
                conn.execute(
                    """UPDATE signature_requests
                    SET signing_url=?, updated_at=?
                    WHERE id=?""",
                    (recovered_url, iso(), ex["id"]),
                )
                conn.commit()

                ex = one(
                    conn,
                    "SELECT * FROM signature_requests WHERE id=?",
                    (ex["id"],),
                )

        # Reuse the existing request if its URL is available.
        if ex["signing_url"]:
            return {
                "signature_request_id": ex["id"],
                "status": ex["status"],
                "duplicate": True,
                "signing_url": ex["signing_url"],
            }

        # Never create a duplicate if recovery failed.
        return {
            "signature_request_id": ex["id"],
            "status": ex["status"],
            "duplicate": True,
            "signing_url": None,
            "error": (
                "Could not recover the signing URL for the existing "
                "OpenSign document."
            ),
        }


    os.makedirs(settings.generated_dir, exist_ok=True)

    path = os.path.join(
        settings.generated_dir,
        f"engagement_{case_id}.pdf",
    )

    generate_engagement_letter(
        output_path=path,
        name=case["full_name"],
        company=case["company"],
        signed_date=now().date(),
    )
    
    signature_created_at = now()
    signature_due_at = signature_created_at + timedelta(minutes=5)

    key = f"sig:{case_id}"

    st = signer.create_request(
        case["email"],
        path,
        key,
        signer_name=case["full_name"],
    )

    cur = conn.execute(
        """INSERT INTO signature_requests(
            case_id, provider, external_id, status, sandbox,
            letter_path, signing_url, created_at, updated_at, due_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            case_id,
            signer.provider,
            st.external_id,
            st.status,
            int(st.sandbox),
            path,
            st.signing_url,
            signature_created_at.isoformat(),
            signature_created_at.isoformat(),
            signature_due_at.isoformat(),
        ),
    )


    audit(
        conn,
        case_id,
        "signing",
        "signature_requested",
        {
            "provider": signer.provider,
            "external_id": st.external_id,
            "sandbox": st.sandbox,
            "signing_url_available": bool(st.signing_url),
        },
    )

    conn.commit()
    recompute_state(conn, case_id, "signing")

    if st.signing_url:
        email_body = (
            f"Hi {case['full_name'] or 'there'},\n\n"
            "Your engagement letter is ready for review and signature.\n"
            f"Please complete the signing within 5 minutes, by "
            f"{signature_due_at.strftime('%Y-%m-%d %H:%M UTC')}.\n\n"
            "Please use the secure link below to open your document, "
            "review it, and complete the signing process:\n\n"
            f"{st.signing_url}\n\n"
            "If you have any questions or notice anything that needs "
            "correction, please contact our onboarding team before signing.\n\n"
            "Thank you,\n"
            "Onboarding Team"
        )

        email_result = send_email(
            conn,
            settings,
            gmail,
            case_id,
            "signing_request",
            f"signmail:{case_id}:test2",
            "Action Required: Review and Sign Your Engagement Letter",
            email_body,
            force_resend=True,
        )
    else:
        audit(
            conn,
            case_id,
            "signing",
            "signing_url_missing",
            {"external_id": st.external_id},
        )
        conn.commit()

        email_result = {
            "status": "not_sent",
            "error": "OpenSign did not return a signing URL",
        }

    return {
        "signature_request_id": cur.lastrowid,
        "status": st.status,
        "duplicate": False,
        "sandbox": st.sandbox,
        "signing_url_available": bool(st.signing_url),
        "email_status": email_result["status"],
    }


def apply_signature_status(
    conn,
    settings,
    gmail,
    signer,
    external_id: str,
    status: str,
) -> dict:
    s = one(
        conn,
        "SELECT * FROM signature_requests WHERE external_id=?",
        (external_id,),
    )
    if not s:
        raise NotFound(external_id)

    if RANK.get(status, -1) < 0:
        raise ValueError("unknown signature status")

    if (
        RANK[status] < RANK.get(s["status"], 0)
        or (
            s["status"] in ("signed", "declined", "expired")
            and status != s["status"]
        )
    ):
        audit(
            conn,
            s["case_id"],
            "signing",
            "stale_signature_event_ignored",
            {"got": status, "have": s["status"]},
        )
        conn.commit()
        return {
            "applied": False,
            "reason": "out_of_order_or_terminal",
            "status": s["status"],
        }

    cid = s["case_id"]
    is_sandbox = bool(s["sandbox"])
    sandbox_allowed = bool(
        getattr(settings, "allow_sandbox_signature", False)
    )

    verified = int(status == "signed")

    conn.execute(
        """
        UPDATE signature_requests
        SET status=?, verified=?, updated_at=?, last_checked_at=?
        WHERE id=?
        """,
        (status, verified, iso(), iso(), s["id"]),
    )

    accepted = False

    if status == "signed":
        if is_sandbox and not sandbox_allowed:
            audit(
                conn,
                cid,
                "signing",
                "sandbox_signature_not_accepted",
                {"external_id": external_id},
            )
        else:
            accepted = True

            conn.execute(
                """
                UPDATE requirements
                SET status='satisfied', updated_at=?
                WHERE case_id=? AND kind='signature'
                """,
                (iso(), cid),
            )
            touch(conn, cid)

            if is_sandbox:
                audit(
                    conn,
                    cid,
                    "signing",
                    "sandbox_signature_accepted_for_review",
                    {"external_id": external_id},
                )

    elif status in ("declined", "expired"):
        _escalate(conn, cid, f"signature {status}")

    conn.commit()
    recompute_state(conn, cid, "signing")

    email_result = None

    if accepted:
        case = get_case(conn, cid)

        try:
            # Retrieve the completed PDF from OpenSign.
            pdf_bytes = signer.download_signed_pdf(external_id)

            # Send the signed PDF in a separate email.
            email_result = send_email(
                conn,
                settings,
                gmail,
                cid,
                "signed_document",
                f"signed_document:{cid}",
                "Your Signed Engagement Letter",
                (
                    f"Hi {case['full_name']},\n\n"
                    "Thank you for signing the engagement letter. "
                    "A copy of the signed document is attached "
                    "for your records.\n\n"
                    "Our onboarding team will review your submission "
                    "and contact you if anything else is needed.\n\n"
                    "Best regards,\n"
                    "Onboarding Team"
                ),
                attachments=[
                    (
                        f"engagement_{cid}_signed.pdf",
                        pdf_bytes,
                        "application/pdf",
                    )
                ],
            )

            if email_result.get("status") not in (
                "sent",
                "duplicate_prevented",
            ):
                audit(
                    conn,
                    cid,
                    "signing",
                    "signed_pdf_email_failed",
                    {
                        "external_id": external_id,
                        "email_status": email_result.get("status"),
                        "error": email_result.get("error"),
                    },
                )
                conn.commit()

        except IntegrationError as exc:
            email_result = {
                "status": "failed",
                "error": f"Signed PDF retrieval failed: {exc}",
            }

            audit(
                conn,
                cid,
                "signing",
                "signed_pdf_email_failed",
                {
                    "external_id": external_id,
                    "error": str(exc),
                },
            )
            conn.commit()

    return {
        "applied": True,
        "status": status,
        "case_id": cid,
        "sandbox": is_sandbox,
        "accepted_for_review": accepted,
        "signed_document_email": email_result,
    }



def reconcile_signatures(conn, settings, gmail, signer, case_id=None) -> list[dict]:
    q = """
        SELECT * FROM signature_requests s
        WHERE (
            s.status NOT IN ('signed', 'declined', 'expired')
            OR (
                s.status = 'signed'
                AND s.sandbox = 1
                AND ? = 1
                AND EXISTS (
                    SELECT 1 FROM requirements r
                    WHERE r.case_id = s.case_id
                      AND r.kind = 'signature'
                      AND r.status NOT IN ('satisfied', 'waived')
                )
            )
        )
    """
    args: tuple = (int(settings.allow_sandbox_signature),)
    if case_id:
        q += " AND s.case_id=?"
        args = args + (case_id,)
    out = []
    for s in rows(conn.execute(q, args)):
        try:
            st = signer.get_status(s["external_id"])
            out.append({
                "external_id": s["external_id"],
                **apply_signature_status(
                    conn,
                    settings,
                    gmail,
                    signer,
                    s["external_id"],
                    st.status,
                ),
            })
        except IntegrationError as e:
            out.append({"external_id": s["external_id"], "error": str(e)})
    return out


def complete_case(conn, settings, gmail, case_id) -> dict:
    """Verification gate: COMPLETED only if every requirement is satisfied/waived AND a real signature exists."""
    case = get_case(conn, case_id)
    if case["state"] != "SIGNED":
        return {"completed": False, "reason": f"state is {case['state']}"}
    open_reqs = [r["name"] for r in get_reqs(conn, case_id) if r["status"] not in ("satisfied", "waived")]
    sig = one(conn, "SELECT * FROM signature_requests WHERE case_id=? AND status='signed'", (case_id,))
    sig_waived = one(conn, "SELECT 1 FROM requirements WHERE case_id=? AND kind='signature' AND status='waived'", (case_id,))
    ok_sig = bool(sig and sig["verified"]) or bool(sig_waived)
    if open_reqs or not ok_sig:
        return {"completed": False, "reason": "verification failed", "open": open_reqs, "signature_verified": bool(ok_sig)}
    transition(conn, case_id, CaseState.COMPLETED, "system", "verified")
    send_email(conn, settings, gmail, case_id, "completion", f"complete:{case_id}", "Onboarding complete",
               "Your onboarding is complete.")
    return {"completed": True}


# ---------- events, followups, stall ----------
def record_event(conn, event_id, etype, case_id, payload, corr=None) -> dict:
    try:
        conn.execute("INSERT INTO workflow_events(event_id,type,case_id,payload,correlation_id,status,received_at) "
                     "VALUES(?,?,?,?,?,?,?)", (event_id, etype, case_id, mask_pii(dumps(payload)), corr, "received", iso()))
        conn.commit()
        return {"duplicate": False}
    except Exception:  # noqa: BLE001 - UNIQUE violation = duplicate delivery
        conn.rollback()
        return {"duplicate": True}


def outstanding_text(reqs):
    return "\n".join(f"- {r['name']} ({r['status']})" for r in reqs)


def run_followups(conn, settings, gmail) -> dict:
    sent, suppressed, escalated = [], [], []

    for case in rows(conn.execute(
        "SELECT id FROM cases WHERE state IN "
        "('AWAITING_INFO','AWAITING_DOCUMENTS','AWAITING_SIGNATURE',"
        "'STALLED','DOCUMENT_REVIEW')"
    )):
        cid = case["id"]
        c = get_case(conn, cid)
        reqs = get_reqs(conn, cid)

        sig_req = one(
            conn,
            "SELECT * FROM signature_requests WHERE case_id=?",
            (cid,),
        )

        signature_deadline_passed = (
            c["state"] == "AWAITING_SIGNATURE"
            and sig_req is not None
            and sig_req["due_at"] is not None
            and parse_iso(sig_req["due_at"]) <= now()
        )

        # Handle the signature deadline reminder separately.
        if signature_deadline_passed:
            signature_requirements = [
                r for r in reqs
                if r["kind"] == "signature"
                and r["status"] in ("pending", "needs_correction")
            ]

            if signature_requirements:
                signature_key = f"signature-deadline:{cid}"

                # Skip processing if this exact deadline reminder was already sent.
                existing = one(
                    conn,
                    "SELECT status FROM communications WHERE idem_key=?",
                    (signature_key,),
                )

                if not existing or existing["status"] != "sent":
                    # The deadline itself overrides the ordinary reminder interval.
                    # Other policy checks, including the reminder cap, still apply.
                    policy_requirements = [
                        {**r, "last_reminder_at": None}
                        for r in signature_requirements
                    ]

                    decision = decide(
                        "send_reminder", c, policy_requirements, settings
                    )

                    if not decision.allowed:
                        if "maximum reminders" in decision.reason:
                            _escalate(conn, cid, "max reminders reached")
                            escalated.append(cid)
                        else:
                            suppressed.append({
                                "case_id": cid,
                                "reason": decision.reason,
                            })
                    else:
                        result = send_email(
                            conn,
                            settings,
                            gmail,
                            cid,
                            "reminder",
                            signature_key,
                            "Reminder: signature deadline passed",
                            "Your engagement letter is still awaiting signature. "
                            "Please review and sign it as soon as possible.",
                        )

                        # Increment only after a confirmed send.
                        # A duplicate or uncertain result must not increment again.
                        if result["status"] == "sent":
                            for r in signature_requirements:
                                conn.execute(
                                    "UPDATE requirements "
                                    "SET reminder_count=reminder_count+1, "
                                    "last_reminder_at=? WHERE id=?",
                                    (iso(), r["id"]),
                                )
                            conn.commit()

                        sent.append({
                            "case_id": cid,
                            "type": "signature_deadline",
                            "status": result["status"],
                        })

        # Ordinary reminders continue to follow their normal interval.
        # Don't send the signature requirement again through this path
        # after its dedicated deadline reminder has been processed.
        due = [
            r for r in reqs
            if is_overdue(r, settings)
            and r["status"] != "under_review"
            and (
                r["kind"] != "signature"
                or sig_req is not None
            )
            and not (
                signature_deadline_passed
                and r["kind"] == "signature"
            )
        ]

        if not due:
            continue

        decision = decide("send_reminder", c, due, settings)

        if not decision.allowed:
            if "maximum reminders" in decision.reason:
                _escalate(conn, cid, "max reminders reached")
                escalated.append(cid)
            else:
                suppressed.append({
                    "case_id": cid,
                    "reason": decision.reason,
                })
            continue

        n = max(r["reminder_count"] for r in due) + 1

        result = send_email(
            conn,
            settings,
            gmail,
            cid,
            "reminder",
            f"reminder:{cid}:{n}",
            "Reminder: outstanding onboarding items",
            "Still outstanding:\n" + outstanding_text(due),
        )

        if result["status"] == "sent":
            for r in due:
                conn.execute(
                    "UPDATE requirements "
                    "SET reminder_count=reminder_count+1, "
                    "last_reminder_at=? WHERE id=?",
                    (iso(), r["id"]),
                )
            conn.commit()

        sent.append({
            "case_id": cid,
            "type": "ordinary_reminder",
            "status": result["status"],
        })

    return {
        "sent": sent,
        "suppressed": suppressed,
        "escalated": escalated,
    }

def detect_stalled(conn, settings) -> list[int]:
    cutoff = hours_ago(settings.stall_hours)
    ids = [r["id"] for r in rows(conn.execute(
        "SELECT id FROM cases WHERE state IN ('AWAITING_INFO','AWAITING_DOCUMENTS','AWAITING_SIGNATURE',"
        "'DOCUMENT_REVIEW') AND updated_at<?", (cutoff,)))]
    for cid in ids:
        transition(conn, cid, CaseState.STALLED, "monitor", f"no activity for {settings.stall_hours}h")
    return ids


# ---------- actions & approvals ----------
def _outstanding(conn, cid):
    return [r for r in get_reqs(conn, cid) if r["status"] in ("pending", "needs_correction")]


def _run_action(conn, settings, gmail, signer, case, action, params) -> tuple[dict, bool]:
    cid = case["id"]
    if action in ("send_reminder", "request_missing_info", "request_correction"):
        reqs = _outstanding(conn, cid)
        n = max([r["reminder_count"] for r in reqs] + [0]) + 1
        res = send_email(conn, settings, gmail, cid, action, f"{action}:{cid}:{n}", "Action needed on your onboarding",
                         "Outstanding items:\n" + outstanding_text(reqs))
        if res["status"] == "sent":
            for r in reqs:
                conn.execute("UPDATE requirements SET reminder_count=reminder_count+1, last_reminder_at=? WHERE id=?",
                             (iso(), r["id"]))
            conn.commit()
        return res, res["status"] in ("sent", "duplicate_prevented")
    if action == "reconcile_signature":
        res = reconcile_signatures(conn, settings, signer, cid)
        return {"results": res}, all("error" not in r for r in res)
    if action == "reconcile_email":
        res = reconcile_communications(conn, settings, gmail, cid, force=True)
        left = one(conn, "SELECT COUNT(*) n FROM communications WHERE case_id=? AND status IN ('uncertain','pending')", (cid,))
        return {"results": res}, left["n"] == 0
    if action == "escalate_to_human":
        _escalate(conn, cid, params.get("reason", "agent escalation"))
        return {}, get_case(conn, cid)["state"] == "NEEDS_HUMAN"
    if action == "waive_requirement":
        conn.execute("UPDATE requirements SET status='waived', detail=?, updated_at=? WHERE id=? AND case_id=?",
                     (params.get("reason", ""), iso(), int(params["requirement_id"]), cid))
        conn.commit()
        c2 = get_case(conn, cid)
        if c2["state"] == "NEEDS_HUMAN":
            transition(conn, cid, CaseState.STALLED, "approval", "resumed after waiver")
        recompute_state(conn, cid, "approval")
        r = one(conn, "SELECT status FROM requirements WHERE id=?", (int(params["requirement_id"]),))
        return {}, r["status"] == "waived"
    if action == "close_case":
        c2 = get_case(conn, cid)
        if c2["state"] != "CLOSED":
            transition(conn, cid, CaseState.CLOSED, "approval", params.get("reason", ""))
        return {}, get_case(conn, cid)["state"] == "CLOSED"
    if action == "change_client_email":
        new = str(params.get("new_email", "")).lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", new):
            raise ValueError("invalid new_email")
        conn.execute("UPDATE clients SET email=? WHERE id=?", (new, case["client_id"]))
        conn.commit()
        return {}, get_case(conn, cid)["email"] == new
    raise ValueError("no handler")


def execute_action(conn, settings, gmail, signer, case_id, action, params=None, actor="system", origin="system",
                   investigation_id=None, approval_id=None) -> dict:
    params = params or {}
    case = get_case(conn, case_id)
    approved = False
    if approval_id:
        a = one(conn, "SELECT * FROM approvals WHERE id=?", (approval_id,))
        approved = bool(a and a["status"] == "approved" and a["action"] == action and a["case_id"] == case_id)
    d = decide(action, case, get_reqs(conn, case_id), settings, origin, approved)
    cur = conn.execute("INSERT INTO actions(case_id,investigation_id,action,params,status,policy_reason,actor,created_at)"
                       " VALUES(?,?,?,?,?,?,?,?)", (case_id, investigation_id, action, dumps(params), "proposed",
                                                    d.reason, actor, iso()))
    aid = cur.lastrowid
    if not d.allowed:
        conn.execute("UPDATE actions SET status='denied' WHERE id=?", (aid,))
        audit(conn, case_id, actor, "action_denied", {"action": action, "reason": d.reason})
        conn.commit()
        return {"action_id": aid, "status": "denied", "reason": d.reason}
    if d.requires_approval:
        exp = iso(now() + timedelta(minutes=settings.approval_ttl_minutes))
        ap = conn.execute("INSERT INTO approvals(action_id,case_id,action,params,requested_by,status,expires_at,"
                          "created_at) VALUES(?,?,?,?,?,?,?,?)", (aid, case_id, action, dumps(params), actor,
                                                                  "pending", exp, iso()))
        conn.execute("UPDATE actions SET status='awaiting_approval' WHERE id=?", (aid,))
        audit(conn, case_id, actor, "approval_requested", {"action": action, "approval_id": ap.lastrowid})
        conn.commit()
        return {"action_id": aid, "status": "awaiting_approval", "approval_id": ap.lastrowid}
    try:
        result, verified = _run_action(conn, settings, gmail, signer, case, action, params)
    except Exception as e:  # noqa: BLE001
        conn.execute("UPDATE actions SET status='failed', result=? WHERE id=?", (str(e)[:300], aid))
        audit(conn, case_id, actor, "action_failed", {"action": action, "error": str(e)[:200]})
        conn.commit()
        return {"action_id": aid, "status": "failed", "error": str(e)[:200]}
    status = "verified" if verified else "executed_unverified"
    conn.execute("UPDATE actions SET status=?, result=? WHERE id=?", (status, dumps(result), aid))
    if approval_id:
        conn.execute("UPDATE approvals SET status='consumed' WHERE id=?", (approval_id,))
    audit(conn, case_id, actor, f"action_{status}", {"action": action})
    conn.commit()
    return {"action_id": aid, "status": status, "result": result}


def decide_approval(conn, settings, gmail, signer, approval_id, approver: str, approve: bool) -> dict:
    a = one(conn, "SELECT * FROM approvals WHERE id=?", (approval_id,))
    if not a:
        raise NotFound(f"approval {approval_id}")
    if a["status"] != "pending":
        raise ApprovalError("replay", f"approval already {a['status']}")
    if parse_iso(a["expires_at"]) < now():
        conn.execute("UPDATE approvals SET status='expired' WHERE id=?", (approval_id,))
        conn.commit()
        raise ApprovalError("expired", "approval expired")
    if approver == a["requested_by"] or a["requested_by"] in ("agent", "system") and approver in ("agent", "system"):
        raise ApprovalError("self_approval", "requester cannot approve their own action")
    conn.execute("UPDATE approvals SET status=?, decided_by=?, decided_at=? WHERE id=?",
                 ("approved" if approve else "rejected", approver, iso(), approval_id))
    audit(conn, a["case_id"], approver, "approval_decided", {"approval_id": approval_id, "approved": approve})
    conn.commit()
    if not approve:
        conn.execute("UPDATE actions SET status='denied' WHERE id=?", (a["action_id"],))
        conn.commit()
        return {"status": "rejected"}
    res = execute_action(conn, settings, gmail, signer, a["case_id"], a["action"], json.loads(a["params"]),
                         actor=approver, approval_id=approval_id)
    conn.execute("UPDATE actions SET status=? WHERE id=?", (res["status"], a["action_id"]))
    conn.commit()
    return {"status": "approved", "execution": res}


def is_sensitive(action: str) -> bool:
    return action in SENSITIVE_ACTIONS
