"""Dry-run batch classifier CLI."""

import argparse
import asyncio
import os

from gmail_agent.backends import create_configured_server
from gmail_agent.classifier import BATCH_SIZE, SNIPPET_CHARS, classify_search
from gmail_agent.gemini_classifier import GeminiBatchClassifier
from gmail_agent.mcp_client import GmailMCPClient


def _count(value: int | None) -> str:
    return "n/a" if value is None else str(value)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Classify Gmail search results without changing Gmail")
    parser.add_argument("--query", help="Gmail query (required for real backend)")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--max-emails", type=int, default=20)
    parser.add_argument("--snippet-chars", type=int, default=SNIPPET_CHARS)
    args = parser.parse_args()
    if os.environ.get("GMAIL_BACKEND", "fake").lower() == "real" and not args.query:
        parser.error("--query is required for real Gmail")
    async with GmailMCPClient(create_configured_server(label_writes=False)) as mcp:
        report = await classify_search(
            args.query or "", GeminiBatchClassifier(), mcp,
            batch_size=args.batch_size, max_emails=args.max_emails, snippet_chars=args.snippet_chars,
        )
    for item in report.results:
        print(f"{item['message_id']}  {item['category']}  {item['confidence']:.2f}  reply={item['needs_reply']}  deadline={item['deadline'] or '-'}  {item['short_reason']}")
    for usage, batch_count in zip(report.usage, report.batch_sizes):
        print(f"usage model={usage.model} emails_batch={batch_count} input={_count(usage.input_tokens)} output={_count(usage.output_tokens)} total={_count(usage.total_tokens)}")
    print(f"Gemini requests={report.request_count}")


if __name__ == "__main__":
    asyncio.run(main())
