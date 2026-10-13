"""Generates the five n8n workflow JSON files. Run once; the JSON is committed and importable.
n8n only orchestrates (triggers, scheduling, fan-out, error routing). All business rules live in FastAPI."""
import json
import os

API = "http://localhost:8000"
CRED = {"httpHeaderAuth": {"id": "flowops-secret", "name": "FlowOps API Secret"}}
OUT = os.path.join(os.path.dirname(__file__), "..", "n8n", "workflows")


class WF:
    def __init__(self, name):
        self.name, self.nodes, self.conns, self.n = name, [], {}, 0

    def add(self, name, type_, ver, params, x, y, creds=None, **extra):
        n = {"parameters": params, "id": f"{abs(hash((self.name, name))) % 10**12:012d}", "name": name, "type": type_,
             "typeVersion": ver, "position": [x, y], **extra}
        if creds:
            n["credentials"] = creds
        self.nodes.append(n)
        return name

    def link(self, a, b, out=0):
        outs = self.conns.setdefault(a, {"main": []})["main"]
        while len(outs) <= out:
            outs.append([])
        outs[out].append({"node": b, "type": "main", "index": 0})

    def http(self, name, method, path, x, y, body=None, nxt=None, multipart=None):
        p = {"method": method, "url": f"={API}{path}", "authentication": "genericCredentialType",
             "genericAuthType": "httpHeaderAuth", "options": {"timeout": 30000}}
        if multipart:
            p.update({"sendBody": True, "contentType": "multipart-form-data", "bodyParameters": {"parameters": multipart}})
        elif body is not None:
            p.update({"sendBody": True, "specifyBody": "json", "jsonBody": body})
        self.add(name, "n8n-nodes-base.httpRequest", 4.2, p, x, y, CRED, onError="continueErrorOutput",
                 retryOnFail=True, maxTries=3, waitBetweenTries=2000)
        self.link(name, "Record Failure", 1)  # error branch
        if nxt:
            self.link(name, nxt, 0)
        return name

    def failure_node(self, x, y):
        body = ("={{ JSON.stringify({event_id: $execution.id + '-' + $workflow.id + '-failure-' + $now.toMillis(), "
                "type: 'workflow_node_failure', payload: {workflow: $workflow.name, error: String($json.error ? "
                "($json.error.message || $json.error) : 'unknown').slice(0, 300)}}) }}")
        self.add("Record Failure", "n8n-nodes-base.httpRequest", 4.2,
                 {"method": "POST", "url": f"={API}/workflow/events", "authentication": "genericCredentialType",
                  "genericAuthType": "httpHeaderAuth", "sendBody": True, "specifyBody": "json", "jsonBody": body},
                 x, y, CRED, onError="continueRegularOutput")

    def save(self, fname):
        doc = {"name": self.name, "nodes": self.nodes, "connections": self.conns, "active": False,
               "settings": {"executionOrder": "v1", "saveManualExecutions": True},
               "versionId": "1", "meta": {"templateCredsSetupCompleted": False}, "tags": [{"name": "flowops"}]}
        json.dump(doc, open(os.path.join(OUT, fname), "w"), indent=2)


def schedule(wf, name, minutes, x=0, y=0):
    return wf.add(name, "n8n-nodes-base.scheduleTrigger", 1.2,
                  {"rule": {"interval": [{"field": "minutes", "minutesInterval": minutes}]}}, x, y)


def webhook(wf, name, path, x=0, y=0):
    return wf.add(name, "n8n-nodes-base.webhook", 2, {"httpMethod": "POST", "path": path, "authentication": "headerAuth",
                                                    "responseMode": "onReceived", "options": {}}, x, y,
                  {"httpHeaderAuth": {"id": "flowops-webhook-in", "name": "FlowOps Inbound Webhook"}},
                  webhookId=f"flowops-{path}")


def cond(wf, name, left, op_type, op, right, x, y):
    return wf.add(name, "n8n-nodes-base.if", 2, {"conditions": {
        "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose"},
        "conditions": [{"id": f"c-{name}", "leftValue": left, "rightValue": right,
                        "operator": {"type": op_type, "operation": op}}], "combinator": "and"}}, x, y)


def code(wf, name, js, x, y):
    return wf.add(name, "n8n-nodes-base.code", 2, {"jsCode": js}, x, y)


def event_body(etype, payload="{}"):
    return ("={{ JSON.stringify({event_id: $execution.id + '-' + $workflow.id + '-" + etype + "-' + ($json.case_id || $json.id || 0), "
            "type: '" + etype + "', case_id: $json.case_id || $json.id || null, payload: " + payload + "}) }}")


# ---------------- 1. Intake & validation
w = WF("FlowOps 01 - Client Intake & Validation")
w.failure_node(1300, 600)
w.add("Gmail Trigger", "n8n-nodes-base.gmailTrigger", 1.2,
      {"pollTimes": {"item": [{"mode": "everyMinute"}]}, "simple": False,
       "filters": {"labelIds": ["INBOX"], "readStatus": "unread"},
       "options": {"downloadAttachments": True, "attachmentPrefix": "attachment_"}}, 0, 0,
      {"gmailOAuth2": {"id": "gmail-oauth", "name": "FlowOps Gmail"}})
w.http("Send Message To Backend", "POST", "/gmail/messages", 300, 0,
       "={{ JSON.stringify({id: $json.id, threadId: $json.threadId, from: $json.from && $json.from.text ? $json.from.text : $json.from, "
       "subject: $json.subject, text: $json.text, date: $json.date, attachments: Object.keys($binary || {}).map(k => ({filename: $binary[k].fileName, mimeType: $binary[k].mimeType}))}) }}")
cond(w, "Case Resolved And Has Attachments?", "={{ $json.case_id && $json.attachments.length > 0 }}", "boolean", "true", "", 600, 0)
code(w, "Classify Attachments", """const caseId = $input.first().json.case_id;
const out = [];
const bin = $('Gmail Trigger').first().binary || {};
for (const [key, b] of Object.entries(bin)) {
  const n = (b.fileName || '').toLowerCase();
  let type = null;
  if (/(passport|id|licen[cs]e|aadhaar|identity)/.test(n)) type = 'government_id';
  else if (/(address|bill|statement|utility)/.test(n)) type = 'proof_of_address';
  if (type) out.push({json: {case_id: caseId, doc_type: type, filename: b.fileName}, binary: {data: b}});
}
return out; // unclassifiable attachments are left for human review via the audit trail""", 900, -100)
w.http("Upload Attachment", "POST", "/cases/{{ $json.case_id }}/documents", 1200, -100,
       multipart=[{"parameterType": "formBinaryData", "name": "file", "inputDataFieldName": "data"},
                  {"name": "doc_type", "value": "={{ $json.doc_type }}"}])
webhook(w, "Intake Form Webhook", "flowops/intake", 0, 300)
cond(w, "Email Looks Valid?", "={{ $json.body.email }}", "string", "regex", "^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", 300, 300)
w.http("Create Or Update Case", "POST", "/intake", 600, 300,
       "={{ JSON.stringify({email: $json.body.email, full_name: $json.body.full_name, company: $json.body.company, source: 'form'}) }}")
cond(w, "Case Newly Created?", "={{ $json.created }}", "boolean", "true", "", 900, 300)
w.http("Log Case Created Event", "POST", "/workflow/events", 1200, 300, event_body("case_created"))
w.http("Log Invalid Submission", "POST", "/workflow/events", 600, 450,
       "={{ JSON.stringify({event_id: $execution.id + '-invalid', type: 'invalid_intake_rejected', payload: {reason: 'invalid email'}}) }}")
w.link("Gmail Trigger", "Send Message To Backend")
w.link("Send Message To Backend", "Case Resolved And Has Attachments?")
w.link("Case Resolved And Has Attachments?", "Classify Attachments", 0)
w.link("Classify Attachments", "Upload Attachment")
w.link("Intake Form Webhook", "Email Looks Valid?")
w.link("Email Looks Valid?", "Create Or Update Case", 0)
w.link("Email Looks Valid?", "Log Invalid Submission", 1)
w.link("Create Or Update Case", "Case Newly Created?")
w.link("Case Newly Created?", "Log Case Created Event", 0)
w.save("01_client_intake_validation.json")

# ---------------- 2. Documents & signatures
w = WF("FlowOps 02 - Documents & E-Signatures")
w.failure_node(1500, 500)
schedule(w, "Every 5 Min: Find Ready Cases", 5, 0, 0)
w.http("List Awaiting Signature Cases", "GET", "/cases?state=AWAITING_SIGNATURE", 300, 0)
w.http("Initiate Signature Request", "POST", "/cases/{{ $json.id }}/signature", 900, 0)
cond(w, "Newly Issued?", "={{ $json.duplicate === false }}", "boolean", "true", "", 1200, 0)
w.http("Log Signature Issued", "POST", "/workflow/events", 1500, 0, event_body("signature_issued", "{provider: 'opensign_sandbox'}"))
schedule(w, "Every 10 Min: Check Signature Status", 10, 0, 250)
w.http("Reconcile Signature Status", "POST", "/signing/reconcile", 300, 250)
w.http("List Signed Cases", "GET", "/cases?state=SIGNED", 600, 250)
w.http("Verify And Complete Case", "POST", "/cases/{{ $json.id }}/complete", 1200, 250)
webhook(w, "Document Upload Webhook", "flowops/document-upload", 0, 450)
w.http("Upload And Process Document", "POST", "/cases/{{ $json.query.case_id }}/documents", 400, 450,
       multipart=[{"parameterType": "formBinaryData", "name": "file", "inputDataFieldName": "file"},
                  {"name": "doc_type", "value": "={{ $json.query.doc_type }}"}])
cond(w, "Needs Human Review?", "={{ $json.status }}", "string", "equals", "needs_review", 800, 450)
w.http("Log Review Needed", "POST", "/workflow/events", 1200, 450, event_body("document_needs_review", "{document_id: $json.document_id}"))
w.link("Every 5 Min: Find Ready Cases", "List Awaiting Signature Cases")
w.link("List Awaiting Signature Cases", "Initiate Signature Request")
w.link("Initiate Signature Request", "Newly Issued?")
w.link("Newly Issued?", "Log Signature Issued", 0)
w.link("Every 10 Min: Check Signature Status", "Reconcile Signature Status")
w.link("Reconcile Signature Status", "List Signed Cases")
w.link("List Signed Cases", "Verify And Complete Case")
w.link("Document Upload Webhook", "Upload And Process Document")
w.link("Upload And Process Document", "Needs Human Review?")
w.link("Needs Human Review?", "Log Review Needed", 0)
w.save("02_documents_signatures.json")

# ---------------- 3. Follow-ups & deadlines
w = WF("FlowOps 03 - Follow-ups & Deadlines")
w.failure_node(1200, 400)
schedule(w, "Hourly Deadline Check", 60, 0, 0)
webhook(w, "Requirement Changed Webhook", "flowops/requirement-changed", 0, 200)
w.http("Run Follow-up Engine", "POST", "/followups/run", 400, 100)
code(w, "Summarise Outcome", """const r = $input.first().json;
return [{json: {sent: r.sent.length, suppressed: r.suppressed.length, escalated: r.escalated, case_id: 0}}];""", 700, 100)
cond(w, "Anything To Report?", "={{ $json.sent + $json.escalated.length > 0 }}", "boolean", "true", "", 950, 100)
w.http("Log Follow-up Run", "POST", "/workflow/events", 1200, 100,
       "={{ JSON.stringify({event_id: $execution.id + '-followups', type: 'followups_run', payload: $json}) }}")
w.link("Hourly Deadline Check", "Run Follow-up Engine")
w.link("Requirement Changed Webhook", "Run Follow-up Engine")
w.link("Run Follow-up Engine", "Summarise Outcome")
w.link("Summarise Outcome", "Anything To Report?")
w.link("Anything To Report?", "Log Follow-up Run", 0)
w.save("03_followups_deadlines.json")

# ---------------- 4. AI investigation & recovery
w = WF("FlowOps 04 - AI Investigation & Recovery")
w.failure_node(1500, 500)
schedule(w, "Every 30 Min: Detect Stalled", 30, 0, 0)
w.http("Detect Stalled Cases", "POST", "/stalled/detect", 300, 0)
w.http("List Stalled Cases", "GET", "/cases?state=STALLED", 600, 0)
w.http("Investigate And Recover", "POST", "/cases/{{ $json.id }}/recover", 1200, 0)
cond(w, "Awaiting Human Approval?", "={{ $json.action.status }}", "string", "equals", "awaiting_approval", 1500, 0)
w.http("Log Approval Pending", "POST", "/workflow/events", 1800, -100,
       "={{ JSON.stringify({event_id: 'approval-' + $json.action.approval_id, type: 'approval_pending', payload: {approval_id: $json.action.approval_id}}) }}")
cond(w, "Recovery Unverified Or Failed?", "={{ ['executed_unverified','failed'].includes($json.action.status) }}", "boolean", "true", "", 1800, 100)
w.http("Log Recovery Problem", "POST", "/workflow/events", 2100, 100,
       "={{ JSON.stringify({event_id: $execution.id + '-recovery-problem', type: 'recovery_unverified', payload: {investigation: $json.investigation.investigation_id, status: $json.action.status}}) }}")
webhook(w, "Incident Webhook", "flowops/incident", 0, 300)
w.http("Investigate Reported Case", "POST", "/cases/{{ $json.body.case_id }}/recover", 400, 300)
w.link("Every 30 Min: Detect Stalled", "Detect Stalled Cases")
w.link("Detect Stalled Cases", "List Stalled Cases")
w.link("List Stalled Cases", "Investigate And Recover")
w.link("Investigate And Recover", "Awaiting Human Approval?")
w.link("Awaiting Human Approval?", "Log Approval Pending", 0)
w.link("Awaiting Human Approval?", "Recovery Unverified Or Failed?", 1)
w.link("Recovery Unverified Or Failed?", "Log Recovery Problem", 0)
w.link("Incident Webhook", "Investigate Reported Case")
w.save("04_ai_investigation_recovery.json")

# ---------------- 5. Reconciliation & error handling
w = WF("FlowOps 05 - Reconciliation & Error Handling")
w.failure_node(1200, 500)
schedule(w, "Every 10 Min: Reconcile", 10, 0, 0)
w.http("Reconcile Gmail And Signatures", "POST", "/reconcile/run", 300, 0)
code(w, "Find Unresolved", """const r = $input.first().json;
const bad = [...r.communications.filter(c => c.result !== 'sent'), ...r.signatures.filter(s => s.error)];
return [{json: {unresolved: bad.length, detail: bad.slice(0, 10), case_id: 0}}];""", 600, 0)
cond(w, "Unresolved Items?", "={{ $json.unresolved > 0 }}", "boolean", "true", "", 900, 0)
w.http("Log Reconciliation Attention", "POST", "/workflow/events", 1200, 0,
       "={{ JSON.stringify({event_id: $execution.id + '-recon', type: 'reconciliation_attention', payload: $json}) }}")
webhook(w, "Provider Event Webhook", "flowops/provider-event", 0, 300)
w.http("Record Provider Event (dedupe)", "POST", "/workflow/events", 300, 300,
       "={{ JSON.stringify({event_id: $json.body.event_id, type: 'provider_event', payload: $json.body}) }}")
cond(w, "First Delivery?", "={{ $json.duplicate === false }}", "boolean", "true", "", 600, 300)
w.http("Reconcile Signature State", "POST", "/signing/reconcile", 900, 300)
w.add("Error Trigger", "n8n-nodes-base.errorTrigger", 1, {}, 0, 600)
w.http("Record Execution Failure", "POST", "/workflow/events", 300, 600,
       "={{ JSON.stringify({event_id: 'exec-' + $json.execution.id, type: 'workflow_execution_failed', payload: {workflow: $json.workflow.name, node: $json.execution.lastNodeExecuted, error: String($json.execution.error && $json.execution.error.message).slice(0, 300)}}) }}")
w.link("Every 10 Min: Reconcile", "Reconcile Gmail And Signatures")
w.link("Reconcile Gmail And Signatures", "Find Unresolved")
w.link("Find Unresolved", "Unresolved Items?")
w.link("Unresolved Items?", "Log Reconciliation Attention", 0)
w.link("Provider Event Webhook", "Record Provider Event (dedupe)")
w.link("Record Provider Event (dedupe)", "First Delivery?")
w.link("First Delivery?", "Reconcile Signature State", 0)
w.link("Error Trigger", "Record Execution Failure")
w.save("05_reconciliation_error_handling.json")
print("generated 5 workflows")
