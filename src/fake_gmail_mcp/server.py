"""MCP adapter for the local fake email store."""

import logging
from typing import TypedDict

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from fake_gmail_mcp.store import FakeEmailStore


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


class LabelResult(TypedDict):
    message_id: str
    label: str
    added: bool
    labels: list[str]


def create_server(store: FakeEmailStore | None = None) -> MCPServer:
    """Bind four MCP tools to one resettable store instance."""
    store = store if store is not None else FakeEmailStore()
    server = MCPServer("fake-gmail")

    def execute(name: str, arguments: dict, operation):
        # IDs and query text can be sensitive. Log only argument names and query length.
        logger.info("tool=%s args=%s status=start", name, arguments)
        try:
            result = operation()
        except ValueError as exc:
            logger.info("tool=%s status=failure", name)
            raise ToolError(str(exc)) from exc
        except Exception:
            logger.error("tool=%s status=failure", name)
            raise
        logger.info("tool=%s status=success", name)
        return result

    @server.tool()
    async def search_emails(query: str = "") -> SearchResult:
        """Search fake emails by plain case-insensitive substring; empty query lists all."""
        return execute("search_emails", {"query_length": len(query)}, lambda: {"messages": store.search_emails(query)})

    @server.tool()
    async def get_email(message_id: str) -> Email:
        """Get one fake email by ID, including its body and labels."""
        return execute("get_email", {"message_id": "[redacted]"}, lambda: store.get_email(message_id))

    @server.tool()
    async def get_thread(thread_id: str) -> ThreadResult:
        """Get all fake emails with the requested thread ID in fixture order."""
        return execute("get_thread", {"thread_id": "[redacted]"}, lambda: {"messages": store.get_thread(thread_id)})

    @server.tool()
    async def apply_label(message_id: str, label: str) -> LabelResult:
        """Add a label to one fake email in memory; this grants no agent authorization."""
        return execute("apply_label", {"message_id": "[redacted]", "label": "[redacted]"}, lambda: store.apply_label(message_id, label))

    return server


server = create_server()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    server.run()
