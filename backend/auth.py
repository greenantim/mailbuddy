"""Google OAuth handling for MailBuddy.

Uses the installed-app / web flow with a localhost redirect. The user supplies
their own `credentials.json` (an OAuth client they create in Google Cloud — see
README). The resulting token is cached in `token.json` so we only authorize once.
"""

import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

ROOT = os.path.dirname(os.path.dirname(__file__))
CREDENTIALS_PATH = os.environ.get(
    "MAILBUDDY_CREDENTIALS", os.path.join(ROOT, "credentials.json")
)
TOKEN_PATH = os.environ.get("MAILBUDDY_TOKEN", os.path.join(ROOT, "token.json"))

# Scopes:
#  - gmail.modify     read metadata, trash, add/remove labels (NOT permanent delete)
#  - gmail.labels     create/manage labels (folders)
#  - gmail.settings.basic  create filters (auto-rules)
# Permanent deletion needs the full `https://mail.google.com/` scope; we only
# request it so the explicit "permanent delete" option can work. If you never
# want that capability, drop it from this list.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://mail.google.com/",
]

REDIRECT_URI = os.environ.get(
    "MAILBUDDY_REDIRECT", "http://localhost:8000/api/auth/callback"
)


def credentials_present():
    return os.path.exists(CREDENTIALS_PATH)


def load_credentials():
    """Return valid Credentials, refreshing if needed, or None if not authorized."""
    if not os.path.exists(TOKEN_PATH):
        return None
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        save_credentials(creds)
    return creds if creds and creds.valid else None


def save_credentials(creds):
    with open(TOKEN_PATH, "w") as f:
        f.write(creds.to_json())


def is_authorized():
    try:
        return load_credentials() is not None
    except Exception:
        return False


def build_flow():
    return Flow.from_client_secrets_file(
        CREDENTIALS_PATH, scopes=SCOPES, redirect_uri=REDIRECT_URI
    )


def auth_url():
    flow = build_flow()
    url, _state = flow.authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="consent"
    )
    return url


def finish_auth(authorization_response_url):
    flow = build_flow()
    flow.fetch_token(authorization_response=authorization_response_url)
    save_credentials(flow.credentials)
    return flow.credentials
