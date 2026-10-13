from __future__ import annotations

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


def _approvers(raw: str) -> dict[str, str]:
    out = {}
    for pair in filter(None, (p.strip() for p in raw.split(","))):
        if ":" in pair:
            tok, name = pair.split(":", 1)
            out[tok.strip()] = name.strip()
    return out


@dataclass
class Settings:
    db_path: str = "data/flowops.db"
    upload_dir: str = "data/uploads"
    generated_dir: str = "data/generated"
    webhook_secret: str = ""
    signing_secret: str = "change-me-signing"
    approver_tokens: dict[str, str] = field(default_factory=lambda: {"local-approver-token": "alice"})
    max_upload_bytes: int = 5 * 1024 * 1024
    reminder_interval_hours: float = 24
    max_reminders: int = 3
    stall_hours: float = 48
    approval_ttl_minutes: int = 60
    reconcile_min_age_seconds: int = 120
    send_max_attempts: int = 3
    backoff_base_seconds: float = 0.5
    llm_model: str = "qwen2.5:1.5b"
    ollama_url: str = "http://localhost:11434"
    llm_timeout: float = 30
    confidence_threshold: float = 0.6
    max_tool_calls: int = 12
    gmail_mode: str = "fake"
    gmail_token_file: str = "token.json"
    sender_address: str = "onboarding@example.com"
    signing_mode: str = "opensign_sandbox"
    opensign_api_token: str = ""
    opensign_api_url: str = "https://sandbox.opensignlabs.com/api/v1.2"
    allow_sandbox_signature: bool = True

    @classmethod
    def from_env(cls) -> Settings:
        e = os.environ.get
        d = cls()
        return cls(
            db_path=e("FLOWOPS_DB_PATH", d.db_path),
            webhook_secret=e("FLOWOPS_WEBHOOK_SECRET", d.webhook_secret),
            signing_secret=e("FLOWOPS_SIGNING_SECRET", d.signing_secret),
            approver_tokens=_approvers(e("FLOWOPS_APPROVER_TOKENS", "local-approver-token:alice")),
            reminder_interval_hours=float(e("FLOWOPS_REMINDER_INTERVAL_HOURS", d.reminder_interval_hours)),
            max_reminders=int(e("FLOWOPS_MAX_REMINDERS", d.max_reminders)),
            stall_hours=float(e("FLOWOPS_STALL_HOURS", d.stall_hours)),
            approval_ttl_minutes=int(e("FLOWOPS_APPROVAL_TTL_MINUTES", d.approval_ttl_minutes)),
            reconcile_min_age_seconds=int(e("FLOWOPS_RECONCILE_MIN_AGE_SECONDS", d.reconcile_min_age_seconds)),
            llm_model=e("FLOWOPS_LLM_MODEL", d.llm_model),
            ollama_url=e("OLLAMA_URL", d.ollama_url),
            llm_timeout=float(e("FLOWOPS_LLM_TIMEOUT", d.llm_timeout)),
            gmail_mode=e("FLOWOPS_GMAIL_MODE", d.gmail_mode),
            gmail_token_file=e("FLOWOPS_GMAIL_TOKEN_FILE", d.gmail_token_file),
            sender_address=e("FLOWOPS_SENDER_ADDRESS", d.sender_address),
            signing_mode=e("FLOWOPS_SIGNING_MODE", d.signing_mode),
            opensign_api_token=e("OPENSIGN_API_TOKEN", d.opensign_api_token),
            opensign_api_url=e("OPENSIGN_API_URL", d.opensign_api_url),
            allow_sandbox_signature=(
                e("FLOWOPS_ALLOW_SANDBOX_SIGNATURE", "false").lower() == "true"
            ),
        )
