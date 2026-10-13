from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

import requests
import streamlit as st
import pandas as pd


# ============================================================
# CONFIGURATION
# ============================================================

API_URL = os.getenv(
    "FLOWOPS_API_URL",
    "http://127.0.0.1:8000",
).rstrip("/")

FLOWOPS_SECRET = os.getenv("FLOWOPS_SECRET", "")
APPROVER_TOKEN = os.getenv("FLOWOPS_APPROVER_TOKEN", "")
TIMEOUT = 20

st.set_page_config(
    page_title="FlowOps | Onboarding Control",
    page_icon="◈",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ============================================================
# STYLING
# ============================================================

st.markdown(
    """
    <style>
    @import url(
      'https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&'
      'family=DM+Sans:wght@400;500;600;700&display=swap'
    );

    :root {
        --bg: #0b0e11;
        --panel: #11161b;
        --panel2: #151b21;
        --border: #252d35;
        --text: #e8edf2;
        --muted: #8b98a5;
        --lime: #c4f56b;
        --cyan: #69d9ed;
        --amber: #f2c46d;
        --red: #ff8585;
        --green: #8ee6a1;
    }

    html, body, [class*="css"] {
        font-family: 'DM Sans', sans-serif;
    }

    .stApp {
        background: var(--bg);
        color: var(--text);
    }

    [data-testid="stSidebar"] {
        background: #0e1216;
        border-right: 1px solid var(--border);
    }

    [data-testid="stSidebar"] > div {
        padding-top: 1.2rem;
    }

    h1, h2, h3 {
        color: var(--text) !important;
        letter-spacing: -0.035em;
    }

    p, label, li {
        color: var(--text);
    }

    [data-testid="stMetric"] {
        background: var(--panel);
        border: 1px solid var(--border);
        padding: 17px 18px;
        border-radius: 12px;
    }

    [data-testid="stMetricLabel"] {
        color: var(--muted) !important;
        font-size: 0.78rem;
    }

    [data-testid="stMetricValue"] {
        color: var(--text) !important;
        font-weight: 700;
    }

    [data-testid="stMetricDelta"] {
        display: none;
    }

    div[data-testid="stVerticalBlockBorderWrapper"] {
        border-color: var(--border) !important;
        border-radius: 12px;
        background: var(--panel);
    }

    .eyebrow {
        color: var(--lime);
        font-family: 'DM Mono', monospace;
        font-size: 0.69rem;
        letter-spacing: 0.13em;
        text-transform: uppercase;
        margin-bottom: 7px;
    }

    .muted {
        color: var(--muted);
        font-size: 0.87rem;
    }

    .small-mono {
        font-family: 'DM Mono', monospace;
        font-size: 0.75rem;
        color: var(--muted);
    }

    .case-title {
        font-size: 1.12rem;
        font-weight: 700;
        color: var(--text);
    }

    .status-pill {
        display: inline-block;
        border: 1px solid var(--border);
        border-radius: 999px;
        padding: 4px 10px;
        font-size: 0.75rem;
        font-weight: 600;
        background: var(--panel2);
        color: var(--text);
    }

    .timeline-line {
        border-left: 2px solid var(--border);
        margin-left: 7px;
        padding-left: 18px;
    }

    .timeline-item {
        padding: 0 0 18px 0;
    }

    .timeline-title {
        font-weight: 600;
        margin-bottom: 4px;
    }

    .timeline-description {
        color: var(--muted);
        font-size: 0.86rem;
        line-height: 1.5;
    }

    div.stButton > button {
        border-radius: 8px;
        font-weight: 600;
        min-height: 40px;
        border: 1px solid var(--border);
    }

    div.stButton > button[kind="primary"] {
        background: var(--lime);
        color: #11160b;
        border-color: var(--lime);
    }

    div.stButton > button[kind="primary"]:hover {
        background: #d4ff87;
        border-color: #d4ff87;
        color: #11160b;
    }

    div[data-testid="stProgressBar"] > div > div {
        background: var(--lime);
    }

    hr {
        border-color: var(--border);
    }

    #MainMenu, footer {
        visibility: hidden;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ============================================================
# STATUS LABELS
# Keep the actual backend states unchanged for API operations.
# ============================================================

STATUS_LABELS = {
    "NEW": "New",
    "AWAITING_DOCUMENTS": "Documents needed",
    "NEEDS_HUMAN": "Needs review",
    "AWAITING_SIGNATURE": "Awaiting signature",
    "SIGNED": "Signed — review needed",
    "COMPLETED": "Completed",
    "FAILED": "Needs attention",
}

DOCUMENT_STATUS_LABELS = {
    "validated": "Validated",
    "approved": "Approved",
    "rejected": "Rejected",
    "needs_human": "Needs review",
    "needs_review": "Needs review",
    "pending_review": "Pending review",
    "manual_review": "Pending review",
    "pending": "Pending",
    "failed": "Could not validate",
}

SIGNATURE_STATUS_LABELS = {
    "pending": "Waiting for signature",
    "sent": "Signing request sent",
    "requested": "Signing request sent",
    "signed": "Signed",
    "completed": "Signed",
    "declined": "Declined",
    "rejected": "Declined",
    "expired": "Expired",
    "cancelled": "Cancelled",
    "failed": "Could not complete",
}


def norm(value: Any) -> str:
    return str(value or "").strip().lower()


def status_label(value: Any) -> str:
    raw = str(value or "UNKNOWN").strip().upper()
    return STATUS_LABELS.get(raw, raw.replace("_", " ").title())


def document_status_label(value: Any) -> str:
    raw = norm(value)
    return DOCUMENT_STATUS_LABELS.get(
        raw,
        raw.replace("_", " ").title() or "Unknown",
    )


def signature_status_label(value: Any) -> str:
    raw = norm(value)
    return SIGNATURE_STATUS_LABELS.get(
        raw,
        raw.replace("_", " ").title() or "Unknown",
    )


def friendly_time(value: Any) -> str:
    """Convert ISO timestamps into readable local time where possible."""
    if not value:
        return "Time unavailable"

    try:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"

        parsed = datetime.fromisoformat(raw)

        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()

        return parsed.strftime("%d %b %Y, %I:%M %p").lstrip("0")
    except (ValueError, TypeError):
        return str(value)


def safe_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value

    if not isinstance(value, str):
        return {}

    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}


def first_value(record: dict, *keys: str, default: Any = "") -> Any:
    for key in keys:
        value = record.get(key)
        if value is not None and value != "":
            return value
    return default


def display_text(value: Any, default: str = "Not available") -> str:
    if value is None or str(value).strip() == "":
        return default
    return str(value)


def human_event_title(event: dict) -> str:
    raw = str(
        first_value(event, "event_type", "event", "name", "action", default="")
    ).strip().lower()

    mapping = {
        "email sent": "Email sent",
        "email_sent": "Email sent",
        "signature requested": "Signing request sent",
        "signature_requested": "Signing request sent",
        "state transition": "Case status updated",
        "state_transition": "Case status updated",
        "document processed": "Document checked",
        "document_processed": "Document checked",
        "signature status updated": "Signature status updated",
        "signature_status_updated": "Signature status updated",
        "onboarding completed": "Onboarding completed",
        "onboarding_completed": "Onboarding completed",
        "document reviewed": "Document review recorded",
        "document_reviewed": "Document review recorded",
        "communication reconciled": "Email delivery checked",
        "communication_reconciled": "Email delivery checked",
        "followup sent": "Follow-up sent",
        "followup_sent": "Follow-up sent",
        "case created": "Onboarding case created",
        "case_created": "Onboarding case created",
        "approval requested": "Review requested",
        "approval_requested": "Review requested",
    }

    if raw in mapping:
        return mapping[raw]

    normalized = raw.replace("_", " ").strip().title()
    return normalized or "Activity recorded"


def human_event_description(event: dict) -> str:
    """
    Return a safe, coordinator-friendly summary.
    Deliberately avoid rendering raw event payloads, provider IDs,
    idempotency keys, and internal implementation flags.
    """
    raw = str(
        first_value(event, "event_type", "event", "name", "action", default="")
    ).strip().lower()

    details = safe_json(
        first_value(event, "details", "metadata", "payload", "data", default={})
    )

    if not isinstance(details, dict):
        details = {}

    if "email" in raw and ("sent" in raw or "send" in raw):
        recipient = first_value(
            details,
            "recipient",
            "to",
            "email",
            default="",
        )
        subject = first_value(details, "subject", "email_subject", default="")

        parts = []
        if recipient:
            parts.append(f"Recipient: {recipient}")
        if subject:
            parts.append(f"Subject: {subject}")

        return " · ".join(parts) or "An email was recorded in the activity history."

    if "signature requested" in raw or "signature_requested" in raw:
        available = details.get("signing_url_available")
        if available is True:
            return "The applicant was sent a signing invitation."
        return "A signing request was created."

    if "state transition" in raw or "state_transition" in raw:
        old_state = first_value(details, "from", "previous_state", default="")
        new_state = first_value(details, "to", "new_state", default="")

        if old_state and new_state:
            return (
                f"Case moved from {status_label(old_state)} "
                f"to {status_label(new_state)}."
            )
        if new_state:
            return f"Case status is now {status_label(new_state)}."
        return "The onboarding case status changed."

    if "document processed" in raw or "document_processed" in raw:
        document_status = first_value(details, "status", default="")
        issues = details.get("issues", [])

        if document_status:
            description = f"Document check result: {document_status_label(document_status)}."
        else:
            description = "The document was checked."

        if isinstance(issues, list) and issues:
            description += " It was flagged for further review."

        return description

    if "signature status" in raw or "signature_status" in raw:
        state = first_value(details, "status", "signature_status", default="")
        if state:
            return f"Signing status is now {signature_status_label(state)}."
        return "The signing status was updated."

    if "completed" in raw:
        return "The onboarding case was marked complete."

    if "review" in raw:
        decision = first_value(details, "decision", "status", default="")
        if decision:
            return f"Review outcome: {str(decision).replace('_', ' ').title()}."
        return "A review action was recorded."

    reason = first_value(event, "description", "summary", "message", default="")
    if reason:
        return str(reason)

    return "This activity was recorded in the onboarding history."


def get_status_color(value: Any) -> str:
    state = str(value or "").upper()

    if state == "COMPLETED":
        return "#8ee6a1"
    if state in {"SIGNED", "AWAITING_SIGNATURE"}:
        return "#69d9ed"
    if state in {"NEEDS_HUMAN", "FAILED"}:
        return "#ff8585"
    if state == "AWAITING_DOCUMENTS":
        return "#f2c46d"
    return "#aab5bf"


def status_badge(value: Any) -> None:
    color = get_status_color(value)
    label = status_label(value)

    st.markdown(
        f"""
        <span style="
            display:inline-block;
            padding:5px 10px;
            border-radius:999px;
            border:1px solid {color}55;
            color:{color};
            background:{color}12;
            font-size:0.76rem;
            font-weight:600;
        ">{label}</span>
        """,
        unsafe_allow_html=True,
    )


# ============================================================
# API CLIENT
# ============================================================

def api_request(
    method: str,
    path: str,
    *,
    approver: bool = False,
    **kwargs,
) -> Any:
    headers = dict(kwargs.pop("headers", {}) or {})

    if FLOWOPS_SECRET:
        headers["X-FlowOps-Secret"] = FLOWOPS_SECRET

    if approver and APPROVER_TOKEN:
        headers["Authorization"] = f"Bearer {APPROVER_TOKEN}"

    response = requests.request(
        method=method,
        url=f"{API_URL}{path}",
        headers=headers,
        timeout=TIMEOUT,
        **kwargs,
    )

    if response.status_code >= 400:
        try:
            body = response.json()
            detail = body.get("detail", body)
        except ValueError:
            detail = response.text or "Unknown server error"

        raise RuntimeError(
            f"FlowOps returned HTTP {response.status_code}: {detail}"
        )

    if response.status_code == 204 or not response.content:
        return {}

    try:
        return response.json()
    except ValueError:
        return {"message": response.text}


def get_cases() -> list[dict]:
    result = api_request("GET", "/cases")

    if isinstance(result, list):
        return result

    if isinstance(result, dict):
        for key in ("cases", "items", "results"):
            if isinstance(result.get(key), list):
                return result[key]

    return []


def get_case(case_id: Any) -> dict:
    result = api_request("GET", f"/cases/{case_id}")
    return result if isinstance(result, dict) else {}


def get_audit() -> list[dict]:
    result = api_request("GET", "/audit")

    if isinstance(result, list):
        return result

    if isinstance(result, dict):
        for key in ("events", "audit_events", "items", "results"):
            if isinstance(result.get(key), list):
                return result[key]

    return []


def get_list(data: dict, *keys: str) -> list[dict]:
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def case_state(case: dict) -> str:
    return str(first_value(case, "state", "case_state", default="UNKNOWN")).upper()


def case_name(case: dict) -> str:
    return display_text(
        first_value(case, "full_name", "client_name", "name", default="Applicant")
    )


def case_email(case: dict) -> str:
    return display_text(
        first_value(case, "email", "client_email", default=""),
        default="Email unavailable",
    )


def case_id(case: dict) -> Any:
    return first_value(case, "id", "case_id", default=None)


def is_open_requirement(requirement: dict) -> bool:
    state = norm(
        first_value(requirement, "status", "state", "requirement_status", default="")
    )

    return state not in {
        "satisfied",
        "complete",
        "completed",
        "approved",
        "waived",
        "not_required",
    }


def open_requirements(requirements: list[dict]) -> list[dict]:
    return [r for r in requirements if is_open_requirement(r)]


def requirement_name(requirement: dict) -> str:
    return display_text(
        first_value(
            requirement,
            "label",
            "name",
            "requirement_type",
            "type",
            default="Requirement",
        )
    )


def requirement_status(requirement: dict) -> str:
    return display_text(
        first_value(requirement, "status", "state", default="Pending")
    ).replace("_", " ").title()


def document_issues(document: dict) -> list:
    issues = safe_json(document.get("issues", []))
    return issues if isinstance(issues, list) else []


def is_reviewable(document: dict) -> bool:
    status = norm(document.get("status"))

    return status in {
        "needs_human",
        "needs_review",
        "pending_review",
        "manual_review",
    }


def count_by_state(cases: list[dict], *states: str) -> int:
    wanted = {state.upper() for state in states}
    return sum(1 for case in cases if case_state(case) in wanted)


def extract_signature_status(signatures: list[dict]) -> str:
    if not signatures:
        return "No signing request recorded"

    latest = signatures[0]
    return signature_status_label(
        first_value(latest, "status", "state", default="unknown")
    )


def human_next_step(
    state: str,
    requirements: list[dict],
    documents: list[dict],
) -> str:
    if state == "COMPLETED":
        return "Onboarding is complete. No further action is required."

    if state == "FAILED":
        return "This case needs attention before onboarding can continue."

    if state == "NEEDS_HUMAN":
        return "Review the flagged documents and record your decision."

    if state == "AWAITING_DOCUMENTS":
        return "Wait for the applicant to provide the outstanding documents."

    if state == "AWAITING_SIGNATURE":
        return "The signing request is in progress. Check the signing status if needed."

    if state == "SIGNED":
        if open_requirements(requirements):
            return "Review the outstanding requirements before completing onboarding."
        return "Review the case and complete onboarding when you are satisfied."

    reviewable = [d for d in documents if is_reviewable(d)]
    if reviewable:
        return "One or more documents need your review."

    return "Check the case details to decide the next step."


# ============================================================
# SIDEBAR
# ============================================================

with st.sidebar:
    st.markdown(
        """
        <div style="padding:4px 0 24px 0;">
            <div style="
                font-size:1.5rem;
                font-weight:800;
                letter-spacing:-0.06em;
            ">◈ FlowOps</div>
            <div class="small-mono" style="margin-top:5px;">
                ONBOARDING CONTROL
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown('<div class="eyebrow">Workspace</div>', unsafe_allow_html=True)

    page = st.radio(
        "Navigation",
        [
            "Case operations",
            "Document review",
            "Signature tracking",
            "Activity & audit",
        ],
        label_visibility="collapsed",
        key="navigation_page",
    )

    st.divider()

    if st.button("↻  Refresh data", use_container_width=True):
        st.rerun()

    st.markdown(
        """
        <div style="
            position:fixed;
            bottom:25px;
            font-size:0.75rem;
            color:#65717c;
        ">
            FlowOps · Onboarding operations
        </div>
        """,
        unsafe_allow_html=True,
    )


# ============================================================
# PAGE HEADER
# ============================================================

st.markdown(
    '<div class="eyebrow">ONBOARDING OPERATIONS</div>',
    unsafe_allow_html=True,
)

st.title(page)

st.markdown(
    '<div class="muted">'
    'A clear view of applicant progress, outstanding reviews, and next actions.'
    '</div>',
    unsafe_allow_html=True,
)

st.write("")


# ============================================================
# LOAD LIVE DATA
# ============================================================

try:
    cases = get_cases()
except Exception as exc:
    st.error(
        "Could not load onboarding cases. Check that the FlowOps API is running "
        "and the dashboard credentials are configured."
    )
    st.caption(str(exc))
    st.stop()


# ============================================================
# TOP METRICS
# ============================================================

total_cases = len(cases)
needs_review_count = count_by_state(cases, "NEEDS_HUMAN")
awaiting_signature_count = count_by_state(cases, "AWAITING_SIGNATURE")
signed_count = count_by_state(cases, "SIGNED")
completed_count = count_by_state(cases, "COMPLETED")
attention_count = count_by_state(cases, "FAILED")

m1, m2, m3, m4, m5 = st.columns(5)

m1.metric("Total cases", total_cases)
m2.metric("Needs review", needs_review_count)
m3.metric("Awaiting signature", awaiting_signature_count)
m4.metric("Signed · pending completion", signed_count)
m5.metric("Completed", completed_count)

if attention_count:
    st.warning(
        f"{attention_count} case(s) need attention. "
        "Filter the case queue to locate them."
    )

st.write("")


# ============================================================
# CASE FILTERS
# ============================================================

st.markdown(
    '<div class="eyebrow">CASE QUEUE</div>',
    unsafe_allow_html=True,
)

left, middle, right = st.columns([2, 1, 1])

with left:
    search_term = st.text_input(
        "Search applicants",
        placeholder="Search name or email",
        key="case_search",
    )

with middle:
    available_states = sorted({
        case_state(case)
        for case in cases
        if case_state(case) != "UNKNOWN"
    })

    selected_state = st.selectbox(
        "Status",
        options=["ALL STATUSES"] + available_states,
        format_func=lambda state: (
            "All statuses"
            if state == "ALL STATUSES"
            else status_label(state)
        ),
    )

with right:
    sort_order = st.selectbox(
        "Sort by",
        [
            "Needs attention first",
            "Newest updated",
            "Oldest updated",
            "Applicant name",
        ],
    )


def searchable(case: dict, query: str) -> bool:
    if not query:
        return True

    haystack = " ".join([
        str(first_value(case, "full_name", "client_name", "name", default="")),
        str(first_value(case, "email", "client_email", default="")),
        str(case_id(case) or ""),
    ]).lower()

    return query.lower() in haystack


filtered_cases = [
    case for case in cases
    if searchable(case, search_term)
    and (
        selected_state == "ALL STATUSES"
        or case_state(case) == selected_state
    )
]

attention_order = {
    "FAILED": 0,
    "NEEDS_HUMAN": 1,
    "SIGNED": 2,
    "AWAITING_DOCUMENTS": 3,
    "AWAITING_SIGNATURE": 4,
    "NEW": 5,
    "COMPLETED": 6,
}

if sort_order == "Needs attention first":
    filtered_cases.sort(
        key=lambda case: attention_order.get(case_state(case), 99)
    )
elif sort_order == "Newest updated":
    filtered_cases.sort(
        key=lambda case: str(case.get("updated_at", "")),
        reverse=True,
    )
elif sort_order == "Oldest updated":
    filtered_cases.sort(
        key=lambda case: str(case.get("updated_at", ""))
    )
else:
    filtered_cases.sort(key=lambda case: case_name(case).lower())


# ============================================================
# VISIBLE CASE LIST
# ============================================================

count = len(filtered_cases)

st.caption(
    f"Showing {count} {'case' if count == 1 else 'cases'}. "
    "Select an applicant to open their onboarding details."
)

if not filtered_cases:
    st.info("No cases match your filters.")
    st.stop()

# Keep the selected case available after the dashboard reruns.
valid_ids = {str(case_id(case)) for case in filtered_cases}

if str(st.session_state.get("selected_case_id", "")) not in valid_ids:
    st.session_state["selected_case_id"] = str(case_id(filtered_cases[0]))

for case in filtered_cases:
    cid = case_id(case)
    name = case_name(case)
    email = case_email(case)
    state = case_state(case)
    is_selected = str(cid) == str(st.session_state["selected_case_id"])

    with st.container(border=True):
        col_person, col_status, col_action = st.columns([3, 2, 1])

        with col_person:
            st.markdown(f"**{name}**")
            st.caption(email)
            st.caption(f"Case ID: {cid}")

        with col_status:
            status_badge(state)

            updated = case.get("updated_at")
            if updated:
                st.caption(f"Updated: {friendly_time(updated)}")

        with col_action:
            if is_selected:
                st.button(
                    "Selected",
                    key=f"selected_{cid}",
                    disabled=True,
                    use_container_width=True,
                )
            else:
                if st.button(
                    "View case",
                    key=f"view_{cid}",
                    use_container_width=True,
                ):
                    st.session_state["selected_case_id"] = str(cid)
                    st.rerun()

# Resolve the selected case from the visible list.
selected_case = next(
    (
        case for case in filtered_cases
        if str(case_id(case))
        == str(st.session_state["selected_case_id"])
    ),
    filtered_cases[0],
)

selected_id = case_id(selected_case)



# ============================================================
# CASE QUEUE
# ============================================================

st.markdown(
    '<div class="eyebrow">CASE QUEUE</div>',
    unsafe_allow_html=True,
)

st.caption(f"{len(filtered_cases)} case(s) match your filters.")

if not filtered_cases:
    st.info("No cases match these filters.")
    st.stop()


case_options = {
    (
        f"{case_name(case)} · "
        f"{case_email(case)} · "
        f"{status_label(case_state(case))} · "
        f"Case {case_id(case)}"
    ): case
    for case in filtered_cases
}

option_labels = list(case_options.keys())

previous_selection = st.session_state.get("selected_case_label")

if previous_selection not in option_labels:
    previous_selection = option_labels[0]

selected_label = st.selectbox(
    "Select an applicant",
    options=option_labels,
    index=option_labels.index(previous_selection),
    key="selected_case_label",
    label_visibility="collapsed",
)

selected_case = case_options[selected_label]
selected_id = case_id(selected_case)

if selected_id is None:
    st.error("The selected case does not have a case ID.")
    st.stop()


# ============================================================
# LOAD SELECTED CASE DETAILS
# ============================================================

try:
    detail = get_case(selected_id)
except Exception as exc:
    st.error("Could not load the selected case details.")
    st.caption(str(exc))
    st.stop()


# Some API implementations return a nested case object.
if isinstance(detail.get("case"), dict):
    base_case = detail["case"]
    for key, value in detail.items():
        if key != "case" and key not in base_case:
            base_case[key] = value
    detail = base_case

current_state = case_state(detail) if detail else case_state(selected_case)

requirements = get_list(detail, "requirements")
documents = get_list(detail, "documents")
signatures = get_list(detail, "signatures")
communications = get_list(detail, "communications")
messages = get_list(detail, "messages")
investigations = get_list(detail, "investigations")

# Use the list response if some top-level case fields are not in the detail.
applicant_name = case_name(detail) if detail else case_name(selected_case)
applicant_email = case_email(detail) if detail else case_email(selected_case)

outstanding = open_requirements(requirements)


# ============================================================
# SELECTED CASE SUMMARY
# ============================================================

st.divider()

header_left, header_right = st.columns([3, 1])

with header_left:
    st.markdown(
        f'<div class="case-title">{applicant_name}</div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        f'<div class="muted">{applicant_email}</div>',
        unsafe_allow_html=True,
    )

with header_right:
    status_badge(current_state)

st.write("")

s1, s2, s3 = st.columns(3)

with s1:
    st.markdown("**Requirements**")
    st.write(f"{len(requirements) - len(outstanding)} of {len(requirements)} complete")

with s2:
    st.markdown("**Documents received**")
    st.write(len(documents))

with s3:
    st.markdown("**Signing status**")
    st.write(extract_signature_status(signatures))

next_step = human_next_step(current_state, requirements, documents)

st.info(f"**Recommended next step:** {next_step}")


# ============================================================
# PAGE CONTENT
# ============================================================

if page == "Case operations":
    tab_overview, tab_documents, tab_signatures = st.tabs(
        ["Overview", "Documents & fields", "Signing"]
    )

elif page == "Document review":
    tab_documents = st.container()
    tab_overview = None
    tab_signatures = None

elif page == "Signature tracking":
    tab_signatures = st.container()
    tab_overview = None
    tab_documents = None

else:
    tab_overview = None
    tab_documents = None
    tab_signatures = None


# ============================================================
# OVERVIEW
# ============================================================

if tab_overview is not None:
    with tab_overview:
        st.markdown("### Onboarding checklist")

        if not requirements:
            st.caption("No requirements were returned for this case.")

        for requirement in requirements:
            done = not is_open_requirement(requirement)
            name = requirement_name(requirement)
            status = requirement_status(requirement)

            icon = "✓" if done else "○"
            st.markdown(
                f"""
                <div style="
                    padding:13px 15px;
                    margin:7px 0;
                    border:1px solid #252d35;
                    border-radius:9px;
                    background:#11161b;
                ">
                    <div style="font-weight:600;">
                        {icon} &nbsp; {name}
                    </div>
                    <div class="muted" style="margin:4px 0 0 23px;">
                        {status}
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

        st.write("")
        st.markdown("### Completion review")

        if current_state == "COMPLETED":
            st.success("This onboarding case is already complete.")

        elif current_state != "SIGNED":
            st.warning(
                "Onboarding cannot be completed yet. "
                "The case must reach the signed state first."
            )

        elif outstanding:
            st.warning(
                f"{len(outstanding)} requirement(s) remain outstanding. "
                "Resolve them before completing onboarding."
            )

        else:
            st.write(
                "The case is signed and all requirements appear satisfied. "
                "Review the details before confirming completion."
            )

            confirmation = st.checkbox(
                "I have reviewed this case and confirm that onboarding can be completed.",
                key=f"confirm_completion_{selected_id}",
            )

            if st.button(
                "Complete onboarding",
                type="primary",
                disabled=not confirmation,
                key=f"complete_case_{selected_id}",
            ):
                try:
                    result = api_request(
                        "POST",
                        f"/cases/{selected_id}/complete",
                    )

                    st.success(
                        "Completion request processed. "
                        "The backend has returned a successful response."
                    )

                    if isinstance(result, dict):
                        state = first_value(result, "state", "case_state", default="")
                        if state:
                            st.write(f"Current status: **{status_label(state)}**")

                    st.rerun()

                except Exception as exc:
                    st.error(
                        "FlowOps did not complete this case. "
                        "The backend may have detected an outstanding requirement."
                    )
                    st.caption(str(exc))

        st.caption(
            "FlowOps validates completion on the server. "
            "The dashboard does not independently mark cases as complete."
        )


# ============================================================
# DOCUMENTS & EXTRACTED FIELDS
# ============================================================

if tab_documents is not None:
    with tab_documents:
        st.markdown("### Documents")

        if not documents:
            st.info(
                "No documents have been recorded for this case. "
                "Documents should be submitted by the applicant."
            )

        for index, document in enumerate(documents):
            doc_id = first_value(document, "id", "document_id", default=index)
            doc_type = display_text(
                first_value(document, "doc_type", "document_type", "type", default="Document")
            ).replace("_", " ").title()

            doc_status = document_status_label(document.get("status"))
            filename = display_text(
                first_value(document, "filename", "file_name", "name", default="")
            )

            with st.container(border=True):
                st.markdown(f"**{doc_type}**")

                if filename != "Not available":
                    st.caption(filename)

                st.write(f"Status: **{doc_status}**")

                confidence = first_value(
                    document,
                    "ocr_confidence",
                    "confidence",
                    default=None,
                )

                if confidence is not None:
                    try:
                        confidence_value = float(confidence)
                        if confidence_value > 1:
                            confidence_value = confidence_value / 100

                        confidence_value = max(0.0, min(1.0, confidence_value))

                        st.caption(f"Extraction confidence: {confidence_value:.0%}")
                        st.progress(confidence_value)
                    except (TypeError, ValueError):
                        pass

                issues = document_issues(document)

                if issues:
                    st.markdown("**Items to check**")
                    for issue in issues:
                        if isinstance(issue, dict):
                            issue_message = first_value(
                                issue,
                                "message",
                                "description",
                                "reason",
                                "code",
                                default="This document needs attention.",
                            )
                        else:
                            issue_message = str(issue)

                        st.warning(str(issue_message))

                if is_reviewable(document):
                    st.markdown("**Review decision**")

                    decision = st.radio(
                        "Choose an outcome",
                        ["Approve document", "Reject document"],
                        key=f"decision_{selected_id}_{doc_id}",
                        horizontal=True,
                    )

                    if st.button(
                        "Submit review",
                        key=f"review_{selected_id}_{doc_id}",
                        type="primary",
                    ):
                        if not APPROVER_TOKEN:
                            st.error(
                                "Document review is unavailable because the "
                                "approver credential is not configured."
                            )
                        else:
                            try:
                                api_request(
                                    "POST",
                                    f"/cases/{selected_id}/documents/{doc_id}/review",
                                    approver=True,
                                    json={
                                        "approve": decision == "Approve document"
                                    },
                                )

                                st.success("Your document review was recorded.")
                                st.rerun()

                            except Exception as exc:
                                st.error("The document review could not be saved.")
                                st.caption(str(exc))

        st.divider()
        st.markdown("### Extracted fields")

        extracted_fields = get_list(detail, "extracted_fields")

        if extracted_fields:
            rows = []

            for field in extracted_fields:
                rows.append({
                    "Field": str(field.get("name", "Field"))
                        .replace("_", " ").title(),
                    "Extracted value": field.get("normalized_value")
                        or field.get("raw_value")
                        or "—",
                    "Document ID": field.get("document_id", "—"),
                    "Review status": field.get("status", "—"),
                })

            fields_df = pd.DataFrame(rows)

            # Group by field name so repeated extractions are easy to compare.
            fields_df = fields_df.sort_values(
                by=["Field", "Document ID"],
                kind="stable",
            )

            st.dataframe(
                fields_df,
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.caption("No extracted fields are available for this case.")


        st.caption(
            "Applicants submit their own documents. Coordinators review "
            "the results and record decisions here."
        )


# ============================================================
# SIGNATURE TRACKING
# ============================================================

if tab_signatures is not None:
    with tab_signatures:
        st.markdown("### Signing progress")

        if not signatures:
            st.info(
                "No signing request has been recorded yet. "
                "The normal workflow initiates signing automatically when "
                "the required checks are satisfied."
            )

        for signature in signatures:
            signature_status = signature_status_label(
                first_value(signature, "status", "state", default="unknown")
            )

            requested_at = friendly_time(
                first_value(
                    signature,
                    "requested_at",
                    "created_at",
                    "sent_at",
                    default="",
                )
            )

            signed_at = friendly_time(
                first_value(
                    signature,
                    "signed_at",
                    "completed_at",
                    default="",
                )
            )

            with st.container(border=True):
                st.markdown(f"**{signature_status}**")
                st.caption(f"Request date: {requested_at}")

                if signed_at != "Time unavailable":
                    st.caption(f"Signed date: {signed_at}")

                message = first_value(
                    signature,
                    "message",
                    "status_message",
                    "description",
                    default="",
                )

                if message:
                    st.write(str(message))

        st.divider()
        st.markdown("### Recovery check")

        st.write(
            "If the applicant has already signed but FlowOps still shows "
            "an outdated status, reconcile the signing status with the provider."
        )

        if st.button(
            "Reconcile provider status",
            key=f"reconcile_signature_{selected_id}",
        ):
            try:
                result = api_request(
                    "POST",
                    "/signing/reconcile",
                )

                st.success(
                    "The reconciliation request completed. "
                    "Refresh the case to see the latest signing status."
                )

                if isinstance(result, dict):
                    results = result.get("results", [])

                    if isinstance(results, list) and results:
                        for item in results:
                            if not isinstance(item, dict):
                                continue

                            outcome = first_value(
                                item,
                                "message",
                                "status",
                                "result",
                                default="Status check processed.",
                            )
                            st.write(str(outcome))

                st.rerun()

            except Exception as exc:
                st.error("The signing status could not be reconciled.")
                st.caption(str(exc))

        st.caption(
            "Signing invitations are managed by the onboarding workflow. "
            "This page provides status visibility and a recovery check; "
            "it does not start or resend signing requests."
        )


# ============================================================
# ACTIVITY & AUDIT
# ============================================================

if page == "Activity & audit":
    st.markdown("### Case activity")

    # Only show events related to the selected case where a case identifier
    # is available on the audit record.
    try:
        all_events = get_audit()
    except Exception as exc:
        st.error("The activity history could not be loaded.")
        st.caption(str(exc))
        all_events = []

    selected_id_text = str(selected_id)

    def event_case_id(event: dict) -> str:
        value = first_value(event, "case_id", "case", default="")
        if isinstance(value, dict):
            value = first_value(value, "id", "case_id", default="")
        return str(value)

    selected_events = [
        event for event in all_events
        if not event_case_id(event)
        or event_case_id(event) == selected_id_text
    ]

    selected_events.sort(
        key=lambda event: str(
            first_value(event, "created_at", "timestamp", "occurred_at", default="")
        ),
        reverse=True,
    )

    if selected_events:
        st.markdown('<div class="timeline-line">', unsafe_allow_html=True)

        for event in selected_events:
            title = human_event_title(event)

            timestamp = friendly_time(
                first_value(
                    event,
                    "created_at",
                    "timestamp",
                    "occurred_at",
                    default="",
                )
            )

            description = human_event_description(event)

            st.markdown(
                f"""
                <div class="timeline-item">
                    <div class="timeline-title">{title}</div>
                    <div class="small-mono">{timestamp}</div>
                    <div class="timeline-description">{description}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

        st.markdown("</div>", unsafe_allow_html=True)

    else:
        st.info("No activity events are available for this case.")

    st.divider()
    st.markdown("### Emails and communications")

    all_communications = communications + messages

    # Avoid showing the same item twice if the API exposes the same record
    # through both communications and messages.
    seen_communication_keys = set()
    unique_communications = []

    for item in all_communications:
        unique_key = (
            str(first_value(item, "id", "communication_id", "message_id", default="")),
            str(first_value(item, "created_at", "sent_at", "timestamp", default="")),
            str(first_value(item, "subject", "template", "kind", "type", default="")),
        )

        if unique_key not in seen_communication_keys:
            seen_communication_keys.add(unique_key)
            unique_communications.append(item)

    unique_communications.sort(
        key=lambda item: str(
            first_value(item, "sent_at", "created_at", "updated_at", default="")
        ),
        reverse=True,
    )

    if not unique_communications:
        st.caption("No email or communication records are available.")

    for communication in unique_communications:
        subject = display_text(
            first_value(
                communication,
                "subject",
                "template_name",
                "template",
                "kind",
                "type",
                default="Onboarding email",
            )
        ).replace("_", " ").title()

        recipient = first_value(
            communication,
            "recipient",
            "to",
            "email",
            "recipient_email",
            default="",
        )

        status = display_text(
            first_value(
                communication,
                "status",
                "delivery_status",
                "state",
                default="Recorded",
            )
        ).replace("_", " ").title()

        sent_time = friendly_time(
            first_value(
                communication,
                "sent_at",
                "created_at",
                "updated_at",
                "timestamp",
                default="",
            )
        )

        body = first_value(
            communication,
            "body",
            "message_body",
            "content",
            "text",
            "html_body",
            default="",
        )

        with st.container(border=True):
            top_left, top_right = st.columns([3, 1])

            with top_left:
                st.markdown(f"**{subject}**")
                if recipient:
                    st.caption(f"To: {recipient}")
                st.caption(sent_time)

            with top_right:
                st.markdown(f"**{status}**")

            if body:
                st.write(str(body))

    st.divider()
    st.markdown("### Investigation notes")

    # Show only an actual human-readable summary, never raw investigation
    # payloads, provider metadata, model internals, or tool-call arguments.
    readable_investigations = []

    for investigation in investigations:
        summary = first_value(
            investigation,
            "summary",
            "recommendation",
            "human_summary",
            "reason",
            default="",
        )

        if summary:
            readable_investigations.append(
                {
                    "summary": str(summary),
                    "created_at": first_value(
                        investigation,
                        "created_at",
                        "timestamp",
                        default="",
                    ),
                }
            )

    if readable_investigations:
        for investigation in readable_investigations:
            with st.container(border=True):
                st.write(investigation["summary"])
                st.caption(friendly_time(investigation["created_at"]))
    else:
        st.caption("No coordinator-facing investigation notes are available.")


# ============================================================
# FOOTER
# ============================================================

st.divider()

st.markdown(
    """
    <div style="
        display:flex;
        justify-content:space-between;
        gap:12px;
        flex-wrap:wrap;
        color:#65717c;
        font-size:0.75rem;
    ">
        <span>FLOWOPS · ONBOARDING OPERATIONS</span>
        <span>Completion checks are enforced by FlowOps.</span>
    </div>
    """,
    unsafe_allow_html=True,
)