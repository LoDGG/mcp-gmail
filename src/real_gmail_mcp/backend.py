"""Map read-only Gmail API responses to the fake MCP result shapes."""

import base64
from typing import Any

from googleapiclient.discovery import build

from real_gmail_mcp.oauth import load_credentials


def _text_body(payload: dict[str, Any]) -> str:
    """Return inline plain text only; attachments and HTML are not fetched."""
    if payload.get("mimeType") == "text/plain":
        encoded = payload.get("body", {}).get("data")
        if encoded:
            return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8", errors="replace")
    return "\n".join(filter(None, (_text_body(part) for part in payload.get("parts", []))))


def _map_message(raw: dict[str, Any], *, include_body: bool) -> dict[str, Any]:
    headers = {entry.get("name", "").lower(): entry.get("value", "") for entry in raw.get("payload", {}).get("headers", [])}
    message = {
        "id": raw["id"],
        "thread_id": raw["threadId"],
        "sender": headers.get("from", ""),
        "subject": headers.get("subject", ""),
        "labels": list(raw.get("labelIds", [])),
    }
    if include_body:
        message["body"] = _text_body(raw.get("payload", {}))
    return message


class RealGmailBackend:
    """Only exposes search and read operations. Inject a service in tests."""

    def __init__(self, service=None) -> None:
        self._service = service if service is not None else build("gmail", "v1", credentials=load_credentials(), cache_discovery=False)

    def search_emails(self, query: str = "") -> list[dict[str, Any]]:
        if not isinstance(query, str):
            raise ValueError("query must be a string")
        found = self._service.users().messages().list(userId="me", q=query, maxResults=20).execute().get("messages", [])
        return [self._get_message(item["id"], include_body=False) for item in found]

    def _get_message(self, message_id: str, *, include_body: bool) -> dict[str, Any]:
        raw = self._service.users().messages().get(
            userId="me", id=message_id, format="full" if include_body else "metadata",
            **({} if include_body else {"metadataHeaders": ["From", "Subject"]}),
        ).execute()
        return _map_message(raw, include_body=include_body)

    def get_email(self, message_id: str) -> dict[str, Any]:
        return self._get_message(message_id, include_body=True)

    def get_thread(self, thread_id: str) -> list[dict[str, Any]]:
        raw = self._service.users().threads().get(userId="me", id=thread_id, format="full").execute()
        return [_map_message(message, include_body=True) for message in raw.get("messages", [])]
