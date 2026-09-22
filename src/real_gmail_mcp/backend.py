"""Map Gmail API responses to the MCP result shapes."""

import base64
from typing import Any

from googleapiclient.discovery import build

from gmail_agent.triage import load_label_mapping
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
    else:
        message["date"] = headers.get("date", "")
        message["snippet"] = raw.get("snippet", "")
    return message


class RealGmailBackend:
    """Read operations plus an explicitly gated existing-label addition."""

    def __init__(self, service=None, *, allow_label_writes: bool = False) -> None:
        self._service = service if service is not None else build("gmail", "v1", credentials=load_credentials(), cache_discovery=False)
        self.allow_label_writes = allow_label_writes
        self._user_label_ids: dict[str, str] | None = None
        self._system_label_names: set[str] = set()

    def existing_user_labels(self) -> set[str]:
        """List existing user-label names and retain their IDs for this run."""
        labels = self._service.users().labels().list(userId="me").execute().get("labels", [])
        self._system_label_names = {item["name"] for item in labels if item.get("type") == "system" and item.get("name")}
        self._user_label_ids = {
            item["name"]: item["id"] for item in labels
            if item.get("type") == "user" and item.get("name") and item.get("id")
        }
        return set(self._user_label_ids)

    def ensure_triage_labels(self) -> dict[str, str]:
        """Create only missing names from the static triage configuration."""
        if not self.allow_label_writes:
            raise PermissionError("Real Gmail label writes are disabled")
        required = load_label_mapping().required_labels
        existing = self.existing_user_labels()
        for name in sorted(required - existing):
            try:
                created = self._service.users().labels().create(
                    userId="me", body={"name": name}
                ).execute()
            except Exception:
                raise ValueError(f"Failed to create configured Gmail label {name!r}") from None
            if created.get("name") != name or created.get("type") != "user" or not created.get("id"):
                raise ValueError(f"Gmail did not return the expected user label {name!r}")
            self._user_label_ids[name] = created["id"]
        return {name: self._user_label_ids[name] for name in sorted(required)}

    def search_emails(self, query: str = "") -> list[dict[str, Any]]:
        if not isinstance(query, str):
            raise ValueError("query must be a string")
        found = self._service.users().messages().list(userId="me", q=query, maxResults=20).execute().get("messages", [])
        return [self._get_message(item["id"], include_body=False) for item in found]

    def _get_message(self, message_id: str, *, include_body: bool) -> dict[str, Any]:
        raw = self._service.users().messages().get(
            userId="me", id=message_id, format="full" if include_body else "metadata",
            **({} if include_body else {"metadataHeaders": ["From", "Subject", "Date"]}),
        ).execute()
        return _map_message(raw, include_body=include_body)

    def get_email(self, message_id: str) -> dict[str, Any]:
        return self._get_message(message_id, include_body=True)

    def get_thread(self, thread_id: str) -> list[dict[str, Any]]:
        raw = self._service.users().threads().get(userId="me", id=thread_id, format="full").execute()
        return [_map_message(message, include_body=True) for message in raw.get("messages", [])]

    def apply_label(self, message_id: str, label: str) -> dict[str, Any]:
        if not self.allow_label_writes:
            raise PermissionError("Real Gmail label writes are disabled")
        if not isinstance(label, str) or not label or label != label.strip() or any(ord(char) < 32 for char in label):
            raise ValueError("label must be a non-empty exact label name")
        if label.upper() in {"INBOX", "SPAM", "TRASH", "UNREAD", "STARRED"}:
            raise ValueError("System labels cannot be applied")
        if self._user_label_ids is None:
            self.existing_user_labels()
        label_id = self._user_label_ids.get(label)
        if label_id is None:
            if label in self._system_label_names:
                raise ValueError("System labels cannot be applied")
            raise ValueError("Unknown Gmail label")
        updated = self._service.users().messages().modify(
            userId="me", id=message_id, body={"addLabelIds": [label_id]}
        ).execute()
        return {"message_id": updated.get("id", message_id), "label": label, "label_id": label_id, "labels": updated.get("labelIds", [])}
