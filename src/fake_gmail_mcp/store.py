"""Small, resettable in-memory email fixture store."""

from copy import deepcopy


_FIXTURES = (
    {
        "id": "msg-newsletter",
        "thread_id": "thread-newsletter",
        "sender": "digest@example.test",
        "subject": "Weekly reading digest",
        "body": "This week's articles and community updates.",
        "labels": ["INBOX"],
    },
    {
        "id": "msg-action",
        "thread_id": "thread-action",
        "sender": "teammate@example.test",
        "subject": "Please review the draft",
        "body": "Could you review the draft by Friday?",
        "labels": ["INBOX"],
    },
    {
        "id": "msg-action-followup",
        "thread_id": "thread-action",
        "sender": "teammate@example.test",
        "subject": "Re: Please review the draft",
        "body": "The revised draft is ready for review.",
        "labels": ["INBOX"],
    },
    {
        "id": "msg-invoice",
        "thread_id": "thread-invoice",
        "sender": "billing@example.test",
        "subject": "September invoice",
        "body": "Your September invoice is attached for processing.",
        "labels": ["INBOX"],
    },
)


class FakeEmailStore:
    """Owns local fixture state; create a new instance or call reset for isolation."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._messages = {message["id"]: deepcopy(message) for message in _FIXTURES}

    def search_emails(self, query: str = "") -> list[dict]:
        """Case-insensitive substring search over sender, subject, and body."""
        if not isinstance(query, str):
            raise ValueError("query must be a string")
        needle = query.strip().casefold()
        matches = []
        for message in self._messages.values():
            haystack = " ".join(message[key] for key in ("sender", "subject", "body"))
            if needle in haystack.casefold():
                matches.append({key: deepcopy(message[key]) for key in ("id", "thread_id", "sender", "subject", "labels")})
        return matches

    def get_email(self, message_id: str) -> dict:
        if message_id not in self._messages:
            raise ValueError(f"Unknown message ID: {message_id}")
        return deepcopy(self._messages[message_id])

    def get_thread(self, thread_id: str) -> list[dict]:
        messages = [deepcopy(message) for message in self._messages.values() if message["thread_id"] == thread_id]
        if not messages:
            raise ValueError(f"Unknown thread ID: {thread_id}")
        return messages

    def apply_label(self, message_id: str, label: str) -> dict:
        if message_id not in self._messages:
            raise ValueError(f"Unknown message ID: {message_id}")
        if not isinstance(label, str) or not label.strip() or label != label.strip():
            raise ValueError("label must be a non-empty string without surrounding whitespace")
        if any(ord(char) < 32 for char in label):
            raise ValueError("label must not contain control characters")
        labels = self._messages[message_id]["labels"]
        added = label not in labels
        if added:
            labels.append(label)
        return {"message_id": message_id, "label": label, "added": added, "labels": labels.copy()}
