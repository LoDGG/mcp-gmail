"""Real Gmail MCP surface with explicitly opted-in label addition."""

import logging
import os
from typing import NotRequired, TypedDict

from mcp.server import MCPServer

from real_gmail_mcp.backend import RealGmailBackend

logger = logging.getLogger(__name__)


class EmailMetadata(TypedDict):
    id: str
    thread_id: str
    sender: str
    subject: str
    labels: list[str]
    date: NotRequired[str]
    snippet: NotRequired[str]


class Email(EmailMetadata):
    body: str


class SearchResult(TypedDict):
    messages: list[EmailMetadata]


class ThreadResult(TypedDict):
    messages: list[Email]


class LabelResult(TypedDict):
    message_id: str
    label: str
    label_id: str
    labels: list[str]


def _label_writes_enabled() -> bool:
    value = os.environ.get("GMAIL_LABEL_WRITES", "0")
    if value not in {"0", "1"}:
        raise ValueError("GMAIL_LABEL_WRITES must be '0' or '1'")
    return value == "1"


def create_server(backend: RealGmailBackend | None = None, *, label_writes: bool | None = None) -> MCPServer:
    label_writes = _label_writes_enabled() if label_writes is None else label_writes
    backend = backend if backend is not None else RealGmailBackend(allow_label_writes=label_writes)
    server = MCPServer("real-gmail")

    @server.tool()
    async def search_emails(query: str = "") -> SearchResult:
        """Search up to 20 Gmail messages with Gmail search syntax; returns metadata."""
        logger.info("tool=search_emails query_length=%s", len(query))
        return {"messages": backend.search_emails(query)}

    @server.tool()
    async def get_email(message_id: str) -> Email:
        """Read one Gmail message, including inline plain-text content."""
        logger.info("tool=get_email")
        return backend.get_email(message_id)

    @server.tool()
    async def get_thread(thread_id: str) -> ThreadResult:
        """Read Gmail messages in one thread, including inline plain-text content."""
        logger.info("tool=get_thread")
        return {"messages": backend.get_thread(thread_id)}

    if label_writes:
        @server.tool()
        async def apply_label(message_id: str, label: str) -> LabelResult:
            """Add an existing user label by its exact name to one Gmail message."""
            logger.info("tool=apply_label")
            return backend.apply_label(message_id, label)

    return server
