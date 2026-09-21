"""Opt-in live Gemini smoke test; never used by automated tests."""

import asyncio
import logging
import sys

from gmail_agent.backends import create_configured_server
from gmail_agent.agent import run_agent
from gmail_agent.gemini import GeminiProvider
from gmail_agent.mcp_client import GmailMCPClient


async def main() -> None:
    async with GmailMCPClient(create_configured_server()) as mcp:
        print(await run_agent(sys.argv[1], GeminiProvider(), mcp))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
