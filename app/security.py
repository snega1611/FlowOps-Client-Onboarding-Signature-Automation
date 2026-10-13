from __future__ import annotations

import hmac
import re

INJECTION_PATTERNS = [
    r"ignore (all |any )?(previous|prior|above) (instructions|rules)",
    r"disregard (the )?(system|previous) (prompt|instructions)",
    r"you are now",
    r"system prompt",
    r"(waive|skip|bypass) (all )?(requirements|verification|approval)",
    r"mark (this|the) (case|client) as (complete|completed|verified)",
    r"approve (this|the) (request|action) (yourself|automatically)",
    r"run (the following )?(command|shell|sql)",
]
_RX = [re.compile(p, re.I) for p in INJECTION_PATTERNS]
EMAIL_RX = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")


def detect_injection(text: str) -> list[str]:
    return [rx.pattern for rx in _RX if rx.search(text or "")]


def mask_pii(text: str) -> str:
    return EMAIL_RX.sub(lambda m: m.group(0)[0] + "***@" + m.group(0).split("@")[1], text or "")


def safe_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def wrap_untrusted(text: str, limit: int = 600) -> str:
    return "<untrusted_data>" + (text or "")[:limit].replace("<", "&lt;") + "</untrusted_data>"
