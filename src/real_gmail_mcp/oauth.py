"""Explicit desktop OAuth setup and credential loading."""

import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
SCOPES = [GMAIL_MODIFY_SCOPE]


def client_secret_path() -> Path:
    return Path(os.environ.get("GMAIL_CLIENT_SECRET_FILE", ".secrets/client_secret.json"))


def token_path() -> Path:
    return Path(os.environ.get("GMAIL_TOKEN_FILE", ".secrets/gmail_token.json"))


def _save_token(credentials: Credentials, path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as token_file:
        token_file.write(credentials.to_json())
    path.chmod(0o600)


def authorize_local() -> None:
    """Run browser consent only when invoked as this explicit setup command."""
    path = client_secret_path()
    if not path.is_file():
        raise FileNotFoundError("Gmail OAuth client file is missing")
    flow = InstalledAppFlow.from_client_secrets_file(str(path), SCOPES)
    credentials = flow.run_local_server(port=0)
    _save_token(credentials, token_path())


def load_credentials() -> Credentials:
    """Load an existing token; never launch consent implicitly."""
    path = token_path()
    if not path.is_file():
        raise FileNotFoundError("Gmail token is missing; run python -m real_gmail_mcp.oauth first")
    # Passing SCOPES here would mask an older gmail.readonly token.
    credentials = Credentials.from_authorized_user_file(str(path))
    if not credentials.has_scopes(SCOPES):
        raise ValueError("Gmail token lacks the gmail.modify scope; re-authorize locally")
    if not credentials.valid:
        if not credentials.expired or not credentials.refresh_token:
            raise ValueError("Gmail token is invalid; authorize again")
        credentials.refresh(Request())
        _save_token(credentials, path)
    return credentials


if __name__ == "__main__":
    authorize_local()
