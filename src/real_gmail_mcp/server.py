"""Read-only MCP surface for real Gmail."""

import logging
from typing import TypedDict

from mcp.server import MCPServer

from real_gmail_mcp.backend import RealGmailBackend

logger = logging.getLogger(__name__)


class EmailMetadata(TypedDict):
    id: str
    thread_id: str
    sender: str
    subject: str
    labels: list[str]


class Email(EmailMetadata):
    body: str


class SearchResult(TypedDict):
    messages: list[EmailMetadata]


class ThreadResult(TypedDict):
    messages: list[Email]


def create_server(backend: RealGmailBackend | None = None) -> MCPServer:
    backend = backend if backend is not None else RealGmailBackend()
    server = MCPServer("real-gmail-readonly")

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

    return server
