"""Local validation shared by approved activation and Gmail label creation."""

RESERVED_LABELS = {
    "INBOX", "SENT", "DRAFT", "DRAFTS", "SPAM", "TRASH", "UNREAD", "STARRED",
    "IMPORTANT", "CHAT", "CHATS", "ALL", "ALL MAIL", "ALL_MAIL",
    "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS",
    "CATEGORY_UPDATES", "CATEGORY_FORUMS",
}


def validate_user_label(name: str) -> None:
    if (
        not isinstance(name, str) or not name or name != name.strip()
        or len(name) > 80 or not all(char.isprintable() for char in name)
    ):
        raise ValueError("Display label must be a non-empty exact printable name of at most 80 characters")
    if name.upper() in RESERVED_LABELS or name.upper().startswith(("CATEGORY_", "[GMAIL]", "[GOOGLE MAIL]")):
        raise ValueError("Reserved Gmail/system label cannot be used")
