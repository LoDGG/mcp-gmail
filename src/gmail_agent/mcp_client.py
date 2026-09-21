"""Discovery and calls through the official MCP client."""

from typing import Any

from mcp import Client
from mcp.server import MCPServer

from gmail_agent.core import ToolCall, ToolResult, ToolSpec


class GmailMCPClient:
    def __init__(self, server: MCPServer) -> None:
        self._server = server
        self._client: Client | None = None

    async def __aenter__(self):
        self._context = Client(self._server, raise_exceptions=True)
        self._client = await self._context.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        # The context manager must be retained for correct MCP teardown.
        await self._context.__aexit__(exc_type, exc, tb)

    async def discover(self) -> list[ToolSpec]:
        assert self._client is not None
        result = await self._client.list_tools()
        return [ToolSpec(tool.name, tool.description or "", tool.input_schema) for tool in result.tools]

    async def call(self, call: ToolCall) -> ToolResult:
        assert self._client is not None
        result = await self._client.call_tool(call.name, call.arguments)
        if result.is_error:
            detail = " ".join(getattr(part, "text", "") for part in result.content)
            return ToolResult(call, {"error": detail}, True)
        content: dict[str, Any] = result.structured_content or {}
        return ToolResult(call, content)
