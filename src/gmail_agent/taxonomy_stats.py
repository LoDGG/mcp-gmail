"""Local taxonomy statistics and deterministic proposal signals; no LLM."""

import argparse
import json

from gmail_agent.history import HistoryDB
from gmail_agent.taxonomy import load_taxonomy
from gmail_agent.taxonomy_analysis import calculate_stats, generate_candidates


def main() -> None:
    parser = argparse.ArgumentParser(description="Show local taxonomy statistics")
    parser.add_argument("--db", help="SQLite database path")
    parser.add_argument("--save-proposals", action="store_true", help="Persist candidate proposals without applying them")
    args = parser.parse_args()
    taxonomy = load_taxonomy()
    with HistoryDB(args.db) as db:
        rows = db.records()
        stats = calculate_stats(rows)
        candidates = generate_candidates(rows, taxonomy)
        if args.save_proposals:
            for proposal in candidates:
                db.save_proposal(proposal, taxonomy.version)
    print(json.dumps({"taxonomy_version": taxonomy.version, "statistics": stats,
                      "proposal_candidates": [candidate.__dict__ for candidate in candidates]}, indent=2))


if __name__ == "__main__":
    main()
