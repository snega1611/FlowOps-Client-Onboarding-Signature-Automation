
"""One-time local OAuth flow for FlowOps Gmail integration."""
import json
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

BASE_DIR = Path(__file__).resolve().parent.parent
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "token.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

if not CREDENTIALS_FILE.is_file():
    raise FileNotFoundError(
        f"Missing {CREDENTIALS_FILE}. Download a Desktop app OAuth client "
        "JSON file from Google Cloud Console."
    )

flow = InstalledAppFlow.from_client_secrets_file(
    str(CREDENTIALS_FILE), SCOPES
)
creds = flow.run_local_server(port=0)

with TOKEN_FILE.open("w", encoding="utf-8") as f:
    json.dump(json.loads(creds.to_json()), f)

print(f"Gmail OAuth token saved to {TOKEN_FILE}")
print("Keep credentials.json and token.json private and out of Git.")
