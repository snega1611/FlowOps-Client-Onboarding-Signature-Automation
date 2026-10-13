
from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Protocol

from app.retry import (
    AuthExpiredError,
    RateLimitedError,
    TransientError,
    UncertainOutcomeError,
)


class MalformedMessage(ValueError):
    """Raised when an incoming email payload is invalid."""


@dataclass
class Attachment:
    filename: str
    mime_type: str
    attachment_id: str = ""
    data: bytes | None = None


@dataclass
class ParsedMessage:
    message_id: str
    thread_id: str
    sender: str
    subject: str
    body: str
    received_at: str = ""
    attachments: list[Attachment] = field(default_factory=list)


@dataclass
class SentResult:
    message_id: str
    thread_id: str
    status: str = "sent"  # Accepted by Gmail; not confirmed delivery.


def _b64(data: str) -> str:
    return base64.urlsafe_b64decode(
        data + "=" * (-len(data) % 4)
    ).decode("utf-8", "replace")


def _walk(part: dict, texts: list[str], attachments: list[Attachment]) -> None:
    mime = part.get("mimeType", "")
    body = part.get("body", {}) or {}

    if part.get("filename"):
        attachments.append(
            Attachment(
                filename=part["filename"],
                mime_type=mime,
                attachment_id=body.get("attachmentId", ""),
            )
        )
    elif mime == "text/plain" and body.get("data"):
        texts.append(_b64(body["data"]))

    for child in part.get("parts", []) or []:
        _walk(child, texts, attachments)


def parse_message(raw: dict) -> ParsedMessage:
    """Parse a Gmail API message or n8n's simplified Gmail trigger payload."""
    if not isinstance(raw, dict):
        raise MalformedMessage("payload is not an object")

    message_id = raw.get("id") or raw.get("message_id")
    thread_id = raw.get("threadId") or raw.get("thread_id") or ""
    attachments: list[Attachment] = []

    if "payload" in raw:
        payload = raw.get("payload") or {}
        headers = {
            h["name"].lower(): h["value"]
            for h in payload.get("headers", [])
            if isinstance(h, dict) and "name" in h and "value" in h
        }

        texts: list[str] = []
        _walk(payload, texts, attachments)

        sender = headers.get("from", "")
        subject = headers.get("subject", "")
        body = "\n".join(texts)
        received = raw.get("internalDate", headers.get("date", ""))
    else:
        sender = raw.get("from") or raw.get("From") or ""

        if isinstance(sender, dict):
            sender = sender.get("text") or (
                (sender.get("value") or [{}])[0].get("address", "")
            )

        subject = raw.get("subject") or raw.get("Subject") or ""
        body = raw.get("text") or raw.get("body") or ""
        received = str(raw.get("date") or raw.get("internalDate") or "")

        for attachment in raw.get("attachments", []) or []:
            if isinstance(attachment, dict) and attachment.get("filename"):
                attachments.append(
                    Attachment(
                        filename=attachment["filename"],
                        mime_type=attachment.get("mimeType", ""),
                        attachment_id=attachment.get("attachmentId", ""),
                    )
                )

    address = parseaddr(str(sender))[1].lower()

    if not message_id or not re.fullmatch(
        r"[^@\s]+@[^@\s]+\.[^@\s]+", address
    ):
        raise MalformedMessage(
            "missing message id or valid sender address"
        )

    return ParsedMessage(
        message_id=str(message_id),
        thread_id=str(thread_id),
        sender=address,
        subject=str(subject)[:300],
        body=str(body)[:20000],
        received_at=str(received),
        attachments=attachments,
    )


def message_id_header(idem_key: str) -> str:
    """Create a stable Message-ID from the FlowOps idempotency key."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", idem_key)
    return f"<flowops-{safe}@flowops.local>"


class GmailAdapter(Protocol):
    label: str

    def send(
        self,
        to: str,
        subject: str,
        body: str,
        idem_key: str,
        thread_id: str | None = None,
        attachments: list[tuple[str, bytes, str]] | None = None,
    ) -> SentResult:
        ...

    def find_sent(self, idem_key: str) -> SentResult | None:
        ...


class GoogleGmailAdapter:
    """Real Gmail API adapter using gmail.send and gmail.readonly scopes."""

    label = "google_gmail_api"

    SCOPES = [
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.readonly",
    ]

    def __init__(self, token_file: str, sender: str):
        self.token_file = token_file
        self.sender = sender
        self._svc = None

    def _service(self):
        if self._svc is None:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            credentials = Credentials.from_authorized_user_file(
                self.token_file,
                self.SCOPES,
            )

            self._svc = build(
                "gmail",
                "v1",
                credentials=credentials,
                cache_discovery=False,
            )

        return self._svc

    @staticmethod
    def _map(error: Exception) -> Exception:
        import socket

        from google.auth.exceptions import RefreshError
        from googleapiclient.errors import HttpError

        if isinstance(error, RefreshError):
            return AuthExpiredError(str(error))

        if isinstance(error, (socket.timeout, TimeoutError)):
            return UncertainOutcomeError(
                "timeout after request was sent"
            )

        if isinstance(error, HttpError):
            status_code = error.resp.status

            if status_code == 401:
                return AuthExpiredError("401 unauthorized")

            if status_code in (403, 429):
                retry_after = float(
                    error.resp.get("retry-after", 0) or 0
                )
                return RateLimitedError(
                    f"HTTP {status_code}",
                    retry_after=retry_after,
                )

            if status_code >= 500:
                return TransientError(f"HTTP {status_code}")

        return error

    def send(
        self,
        to: str,
        subject: str,
        body: str,
        idem_key: str,
        thread_id: str | None = None,
        attachments: list[tuple[str, bytes, str]] | None = None,
    ) -> SentResult:
        message = EmailMessage()
        message["To"] = to
        message["From"] = self.sender
        message["Subject"] = subject
        message["Message-ID"] = message_id_header(idem_key)
        message["X-FlowOps-Key"] = idem_key
        message.set_content(body)

        for filename, content, content_type in attachments or []:
            maintype, subtype = content_type.split("/", 1)
            message.add_attachment(
                content,
                maintype=maintype,
                subtype=subtype,
                filename=filename,
            )

        payload = {
            "raw": base64.urlsafe_b64encode(
                message.as_bytes()
            ).decode("ascii")
        }

        if thread_id:
            payload["threadId"] = thread_id

        try:
            response = (
                self._service()
                .users()
                .messages()
                .send(userId="me", body=payload)
                .execute()
            )
        except Exception as error:
            raise self._map(error) from error

        return SentResult(
            message_id=response["id"],
            thread_id=response.get("threadId", ""),
        )

    def find_sent(self, idem_key: str) -> SentResult | None:
        query = (
            "in:sent rfc822msgid:"
            f"{message_id_header(idem_key).strip('<>')}"
        )

        try:
            response = (
                self._service()
                .users()
                .messages()
                .list(
                    userId="me",
                    q=query,
                    maxResults=1,
                )
                .execute()
            )
        except Exception as error:
            raise self._map(error) from error

        message = (response.get("messages") or [None])[0]

        if not message:
            return None

        return SentResult(
            message_id=message["id"],
            thread_id=message.get("threadId", ""),
        )


def build_gmail(settings) -> GmailAdapter:
    """Build the configured production Gmail adapter; never fall back to fake."""
    if settings.gmail_mode == "google":
        return GoogleGmailAdapter(
            settings.gmail_token_file,
            settings.sender_address,
        )

    raise ValueError(
        f"Unsupported Gmail mode: {settings.gmail_mode!r}. "
        "Configure FLOWOPS_GMAIL_MODE=google."
    )
