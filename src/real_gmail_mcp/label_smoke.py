"""Opt-in single-message label check; invokes MCP without an LLM."""

import asyncio
import os
import sys

from gmail_agent.core import ToolCall
from gmail_agent.mcp_client import GmailMCPClient
from real_gmail_mcp.server import create_server


async def main() -> None:
    if os.environ.get("GMAIL_BACKEND") != "real" or os.environ.get("GMAIL_LABEL_WRITES") != "1":
        raise SystemExit("Set GMAIL_BACKEND=real and GMAIL_LABEL_WRITES=1 explicitly")
    if len(sys.argv) != 3:
        raise SystemExit("Usage: python -m real_gmail_mcp.label_smoke MESSAGE_ID EXISTING_LABEL_NAME")
    async with GmailMCPClient(create_server()) as mcp:
        result = await mcp.call(ToolCall("apply_label", {"message_id": sys.argv[1], "label": sys.argv[2]}))
    if result.is_error:
        raise SystemExit("Gmail label operation failed")
    print("Label applied to one message")


if __name__ == "__main__":
    asyncio.run(main())
