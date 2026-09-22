"""Production-lite batch triage with explicit real-Gmail write opt-ins."""

import argparse
import asyncio
import os

from fake_gmail_mcp.server import create_server as create_fake_server
from gmail_agent.gemini_classifier import GeminiBatchClassifier
from gmail_agent.history import HistoryDB
from gmail_agent.mcp_client import GmailMCPClient
from gmail_agent.triage import BATCH_SIZE, DEFAULT_QUERY, MAX_EMAILS, triage_search
from real_gmail_mcp.backend import RealGmailBackend
from real_gmail_mcp.server import create_server as create_real_server


def _count(value: int | None) -> str:
    return "n/a" if value is None else str(value)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Batch triage; real writes need two explicit opt-ins")
    parser.add_argument("--query", default=DEFAULT_QUERY)
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--max-emails", type=int, default=MAX_EMAILS)
    parser.add_argument("--db", help="SQLite database path")
    parser.add_argument("--apply", action="store_true", help="Ensure configured labels, then apply them after classification")
    args = parser.parse_args()
    backend_name = os.environ.get("GMAIL_BACKEND", "fake").lower()
    write_opt_in = os.environ.get("GMAIL_LABEL_WRITES", "0") == "1"
    if args.apply and (backend_name != "real" or not write_opt_in):
        parser.error("real writes require GMAIL_BACKEND=real, GMAIL_LABEL_WRITES=1, and --apply")
    if backend_name == "real":
        backend = RealGmailBackend(allow_label_writes=args.apply and write_opt_in)
        server = create_real_server(backend, label_writes=False)
    elif backend_name == "fake":
        backend = None
        server = create_fake_server()
    else:
        parser.error("GMAIL_BACKEND must be 'fake' or 'real'")
    with HistoryDB(args.db) as db:
        async with GmailMCPClient(server) as mcp:
            report = await triage_search(
                args.query, GeminiBatchClassifier(), mcp, db,
                apply=args.apply, label_writes=write_opt_in, label_backend=backend,
                batch_size=args.batch_size, max_emails=args.max_emails,
            )
    print(f"emails found={report.emails_found} classified={report.emails_classified}")
    for item in report.outcomes:
        print(f"{item.message_id} category={item.category} confidence={item.confidence:.2f} action={item.action_label} processed={item.processed}" + (f" error={item.error}" if item.error else ""))
    for usage, size in zip(report.usage, report.batch_sizes):
        print(f"usage model={usage.model} emails_batch={size} input={_count(usage.input_tokens)} output={_count(usage.output_tokens)} thinking={_count(usage.thinking_tokens)} total={_count(usage.total_tokens)}")
    print(f"Gemini requests={report.request_count}")


if __name__ == "__main__":
    asyncio.run(main())
