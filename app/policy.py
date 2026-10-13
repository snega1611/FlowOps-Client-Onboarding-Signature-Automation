"""Deterministic policy engine. Independent of the LLM: the agent proposes, this decides."""
from __future__ import annotations

from dataclasses import dataclass

from app.db import hours_ago, parse_iso

SAFE_ACTIONS = {"send_reminder", "request_missing_info", "request_correction", "reconcile_signature",
                "reconcile_email", "escalate_to_human"}
SENSITIVE_ACTIONS = {"waive_requirement", "close_case", "change_client_email"}
ALL_ACTIONS = SAFE_ACTIONS | SENSITIVE_ACTIONS


@dataclass
class Decision:
    allowed: bool
    requires_approval: bool
    reason: str


def decide(action: str, case: dict, requirements: list[dict], settings, origin: str = "system",
           approved: bool = False) -> Decision:
    if action not in ALL_ACTIONS:
        return Decision(False, False, f"unknown action '{action}'")
    if origin in ("email", "document", "untrusted_content"):
        return Decision(False, False, "actions may not be initiated by untrusted email/document content")
    if case["state"] in ("COMPLETED", "CLOSED"):
        return Decision(False, False, f"case is {case['state']}")
    if action in SENSITIVE_ACTIONS:
        if approved:
            return Decision(True, False, "sensitive action with valid human approval")
        return Decision(True, True, "sensitive action requires human approval")
    open_reqs = [r for r in requirements if r["status"] in ("pending", "needs_correction")]
    if action in ("send_reminder", "request_missing_info", "request_correction"):
        if not open_reqs:
            return Decision(False, False, "no outstanding requirements: reminder suppressed")
        eligible = [r for r in open_reqs if r["reminder_count"] < settings.max_reminders]
        if not eligible:
            return Decision(False, False, "maximum reminders reached: escalate instead")
        cutoff = hours_ago(settings.reminder_interval_hours)
        if action == "send_reminder" and all(r["last_reminder_at"] and r["last_reminder_at"] > cutoff
                                             for r in eligible):
            return Decision(False, False, "reminder interval has not elapsed")
    return Decision(True, False, "safe action permitted")


def is_overdue(req: dict, settings) -> bool:
    if req["status"] not in ("pending", "needs_correction"):
        return False
    last = req["last_reminder_at"] or req["created_at"]
    return parse_iso(last) <= parse_iso(hours_ago(settings.reminder_interval_hours))
