
from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import httpx

from app.retry import IntegrationError, UncertainOutcomeError


RANK = {
    "created": 0,
    "sent": 1,
    "viewed": 2,
    "signed": 3,
    "declined": 3,
    "expired": 3,
}


@dataclass
class SigningStatus:
    external_id: str
    status: str
    sandbox: bool
    signing_url: str | None = None



class SigningAdapter(Protocol):
    provider: str
    sandbox: bool

    def create_request(
        self,
        signer_email: str,
        document_path: str,
        idem_key: str,
        signer_name: str | None = None,
    ) -> SigningStatus: ...

    def get_status(self, external_id: str) -> SigningStatus: ...


class OpenSignSandboxSigner:
    """OpenSign sandbox adapter for engagement-letter signing."""

    provider = "opensign_sandbox"
    sandbox = True

    def __init__(
        self,
        api_url: str,
        api_token: str,
        timeout: float = 20,
    ):
        if not api_token.strip():
            raise ValueError("OPENSIGN_API_TOKEN is not configured")

        self.api_url = api_url.rstrip("/")
        self.api_token = api_token.strip()
        self.timeout = timeout

    def _request(self, method: str, path: str, **kwargs):
        try:
            response = httpx.request(
                method,
                f"{self.api_url}/{path.lstrip('/')}",
                headers={
                    "x-api-token": self.api_token,
                    "Accept": "application/json",
                },
                timeout=self.timeout,
                **kwargs,
            )
        except httpx.RequestError as exc:
            raise UncertainOutcomeError(
                "OpenSign connection failed. Check the sandbox document "
                "list before retrying a document-creation request."
            ) from exc

        if response.status_code >= 500:
            raise UncertainOutcomeError(
                f"OpenSign returned HTTP {response.status_code}; "
                "the outcome may be uncertain."
            )

        if not response.is_success:
            raise IntegrationError(
                f"OpenSign returned HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise IntegrationError(
                "OpenSign returned a non-JSON response."
            ) from exc

    def create_request(
        self,
        signer_email: str,
        document_path: str,
        idem_key: str,
        signer_name: str | None = None,
    ) -> SigningStatus:
        path = Path(document_path)

        if not path.is_file():
            raise IntegrationError("Engagement PDF does not exist")

        if path.suffix.lower() != ".pdf":
            raise IntegrationError("OpenSign requires a PDF document")

        try:
            pdf_b64 = base64.b64encode(
                path.read_bytes()
            ).decode("ascii")
        except OSError as exc:
            raise IntegrationError(
                "Could not read the engagement PDF"
            ) from exc

        name = (
            signer_name or signer_email.split("@", 1)[0]
        ).strip() or "Client"

        payload = {
            "file": pdf_b64,
            "title": f"FlowOps Engagement Letter {idem_key}",
            "note": "Please review and sign the engagement letter.",
            "signers": [
                {
                    "role": "Client",
                    "email": signer_email,
                    "name": name,
                    "widgets": [{
                        "type": "signature",
                        "page": 2,
                        "x": 50,
                        "y": 120,
                        "w": 180,
                        "h": 40,
                    }],
                                    }
            ],
            "send_email": False,
            "sendInOrder": True,
            "enableOTP": False,
            "allow_modifications": False,
        }

        data = self._request(
            "POST",
            "/createdocument",
            json=payload,
        )
        
        print("OpenSign raw response:", repr(data))
        
        print("OpenSign response keys:", list(data.keys()))
        print("OpenSign signing URL data:", data.get("signurl")) 

        
        external_id = data.get("objectId")

        if not isinstance(external_id, str) or not external_id.strip():
            raise UncertainOutcomeError(
                "OpenSign response lacked objectId. Check the sandbox "
                "document list before attempting another request."
            )

        signing_url = None
        signers = data.get("signurl", [])

        if isinstance(signers, list):
            for entry in signers:
                if not isinstance(entry, dict):
                    continue

                email = entry.get("email", "")
                if (
                    isinstance(email, str)
                    and email.strip().lower() == signer_email.strip().lower()
                ):
                    candidate = entry.get("url")
                    if (
                        isinstance(candidate, str)
                        and candidate.startswith("https://")
                    ):
                        signing_url = candidate
                        break

        return SigningStatus(
            external_id=external_id,
            status="sent",
            sandbox=True,
            signing_url=signing_url,
        )

    
    def get_status(self, external_id: str) -> SigningStatus:
        data = self._request(
            "GET",
            f"/document/{quote(external_id, safe='')}",
        )
        
        print("OpenSign existing document response:", repr(data))
        
        print("Response keys:", list(data.keys()))
        print("Signer data:", repr(data.get("signers")))
        print("Sign URL data:", repr(data.get("signurl")))

        if not isinstance(data, dict):
            raise IntegrationError(
                "OpenSign document response must be a JSON object."
            )

        raw_value = data.get("status")
        if not isinstance(raw_value, str) or not raw_value.strip():
            raise IntegrationError(
                "OpenSign document response is missing a valid status."
            )

        raw_status = raw_value.strip().lower()

        if raw_status == "completed":
            status = "signed"
        elif raw_status == "declined":
            status = "declined"
        elif raw_status == "expired":
            status = "expired"
        elif raw_status in {"draft", "created"}:
            status = "created"
        elif raw_status in {"in-progress", "in progress", "sent"}:
            status = "sent"
        else:
            raise IntegrationError(
                f"Unrecognized OpenSign document status: {raw_status!r}"
            )

        return SigningStatus(
            external_id=external_id,
            status=status,
            sandbox=True,
        )
        
    def download_signed_pdf(self, external_id: str) -> bytes:
        import httpx

        # Never download or send an unsigned document.
        status = self.get_status(external_id)
        if status.status != "signed":
            raise IntegrationError(
                f"Cannot download signed PDF: status is {status.status!r}"
            )

        data = self._request(
            "GET",
            f"/document/{quote(external_id, safe='')}",
        )

        file_url = data.get("file")
        if not isinstance(file_url, str) or not file_url.startswith("https://"):
            raise IntegrationError(
                "OpenSign response is missing a valid PDF URL."
            )

        try:
            response = httpx.get(
                file_url,
                timeout=self.timeout,
                follow_redirects=True,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise IntegrationError(
                f"Failed to download signed PDF: {exc}"
            ) from exc

        pdf_bytes = response.content

        if not pdf_bytes.startswith(b"%PDF-"):
            raise IntegrationError(
                "OpenSign download did not return a valid PDF."
            )

        return pdf_bytes
    
    def get_signing_url(
        self,
        external_id: str,
        signer_email: str,
    ) -> str | None:
        data = self._request(
            "GET",
            f"/signinglinks/{quote(external_id, safe='')}",
        )

        signers = data.get("signurl", [])

        if not isinstance(signers, list):
            return None

        for entry in signers:
            if not isinstance(entry, dict):
                continue

            email = entry.get("email", "")
            url = entry.get("url")

            if (
                isinstance(email, str)
                and email.strip().lower() == signer_email.strip().lower()
                and isinstance(url, str)
                and url.startswith("https://")
            ):
                return url

        return None



def sign_callback(secret: str, body: bytes) -> str:
    return hmac.new(
        secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()


def verify_callback(
    secret: str,
    body: bytes,
    signature: str,
) -> bool:
    return hmac.compare_digest(
        sign_callback(secret, body),
        signature or "",
    )


def build_signer(settings) -> SigningAdapter:
    if settings.signing_mode != "opensign_sandbox":
        raise ValueError(
            f"Unsupported signing mode: {settings.signing_mode!r}. "
            "Only opensign_sandbox is supported."
        )

    return OpenSignSandboxSigner(
        api_url=settings.opensign_api_url,
        api_token=settings.opensign_api_token,
    )
