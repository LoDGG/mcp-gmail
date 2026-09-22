"""Explicit MCP backend selection; fake is the default."""

import os

from mcp.server import MCPServer


def create_configured_server(*, label_writes: bool | None = None) -> MCPServer:
    backend = os.environ.get("GMAIL_BACKEND", "fake").lower()
    if backend == "fake":
        from fake_gmail_mcp.server import create_server
        return create_server()
    if backend == "real":
        from real_gmail_mcp.server import create_server
        return create_server(label_writes=label_writes)
    raise ValueError("GMAIL_BACKEND must be 'fake' or 'real'")
