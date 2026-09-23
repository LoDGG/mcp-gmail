"""Explicit approved-proposal activation; read-only preview by default."""

import argparse
import json
import os
from pathlib import Path

from gmail_agent.history import HistoryDB
from gmail_agent.taxonomy import DEFAULT_TAXONOMY_PATH
from gmail_agent.taxonomy_activation import ActivationError, activate_proposal
from gmail_agent.triage import DEFAULT_LABEL_MAP_PATH
from real_gmail_mcp.backend import RealGmailBackend


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview or activate a human-approved CREATE proposal")
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--list", action="store_true", help="List stored CREATE proposals and activation status")
    selector.add_argument("--proposal", help="Normalized proposed concept, e.g. job_offer")
    parser.add_argument("--db", help="SQLite history path")
    parser.add_argument("--taxonomy", type=Path, default=DEFAULT_TAXONOMY_PATH)
    parser.add_argument("--label-map", type=Path, default=DEFAULT_LABEL_MAP_PATH)
    parser.add_argument("--display-label", help="Exact Gmail user label if the proposal has no display name")
    parser.add_argument("--definition", help="Human-written category definition (required on first activation)")
    parser.add_argument("--apply", action="store_true", help="Activate; also requires GMAIL_LABEL_WRITES=1")
    args = parser.parse_args()
    writes = os.environ.get("GMAIL_LABEL_WRITES", "0") == "1"
    if args.apply and (args.list or not writes):
        parser.error("--apply requires --proposal and GMAIL_LABEL_WRITES=1")
    try:
        with HistoryDB(args.db) as db:
            if args.list:
                audits = {item["proposal_id"]: item for item in db.activation_records()}
                approved = {item["proposal_id"] for item in db.proposal_supports()
                            if item["source"] in {"human_manual", "human_proposal_accept"}}
                output = [
                    {"proposal_id": item["id"], "concept_key": item["concept_key"],
                     "display_label": item["display_label"],
                     "status": audits[item["id"]]["status"] if item["id"] in audits else (
                         "human_approved" if item["id"] in approved and item["status"] == "proposed" else item["status"]),
                     "activation": audits.get(item["id"])}
                    for item in db.proposals() if item["proposal_type"] == "CREATE"
                ]
            else:
                output = activate_proposal(
                    db, args.proposal, apply=args.apply, label_writes=writes,
                    taxonomy_path=args.taxonomy, label_map_path=args.label_map,
                    display_label=args.display_label, definition=args.definition,
                    backend_factory=lambda: RealGmailBackend(allow_label_writes=args.apply and writes),
                )
        print(json.dumps(output, ensure_ascii=False, indent=2))
    except (ValueError, PermissionError, OSError, ActivationError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
