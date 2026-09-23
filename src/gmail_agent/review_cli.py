"""Fast local review of saved classifier predictions."""

import argparse

from gmail_agent.history import HistoryDB, UNSET
from gmail_agent.taxonomy import IMPORTANCE_VALUES, load_taxonomy


def _optional_bool(value: str) -> bool | None:
    if not value:
        return None
    if value.lower() in {"y", "yes"}:
        return True
    if value.lower() in {"n", "no"}:
        return False
    raise ValueError("Enter y, n, or leave blank")


def main() -> None:
    parser = argparse.ArgumentParser(description="Review saved classifications locally")
    parser.add_argument("--db", help="SQLite database path")
    args = parser.parse_args()
    taxonomy = load_taxonomy()
    skipped: set[int] = set()
    with HistoryDB(args.db) as db:
        while pending := db.pending(exclude_ids=skipped):
            row = pending[0]
            print(f"\nMessage {row['message_id']} | taxonomy {row['taxonomy_version']}")
            print(f"Predicted {row['predicted_category']} ({row['predicted_confidence']:.2f}), reply={bool(row['predicted_needs_reply'])}, importance={row['predicted_importance']}, deadline={row['predicted_deadline'] or '-'}")
            print(f"Subtype hint (unverified): {row['subtype_hint'] or '-'}")
            print(f"Reason: {row['short_reason']}")
            action = input("[a]ccept, [c]orrect, [s]kip, [q]uit: ").strip().lower()
            if action == "q":
                break
            if action == "s":
                skipped.add(row["id"])
                continue
            if action == "a":
                db.review(row["id"], "accepted")
                continue
            if action != "c":
                print("Unknown choice")
                continue
            try:
                category = input(f"Category ({', '.join(taxonomy.active_names)}; blank=keep): ").strip().upper() or None
                if category is not None and category not in taxonomy.active_names:
                    raise ValueError("Unknown category")
                needs_reply = _optional_bool(input("Needs reply (y/n; blank=keep): ").strip())
                importance = input(f"Importance ({'/'.join(IMPORTANCE_VALUES)}; blank=keep): ").strip().lower() or None
                if importance is not None and importance not in IMPORTANCE_VALUES:
                    raise ValueError("Unknown importance")
                deadline_input = input("Deadline (short value; '-'=clear; blank=keep): ").strip()
                deadline = UNSET if not deadline_input else None if deadline_input == "-" else deadline_input
                db.review(row["id"], "corrected", category=category, needs_reply=needs_reply,
                          importance=importance, deadline=deadline, taxonomy=taxonomy)
            except ValueError as exc:
                print(exc)
        else:
            print("No more pending classifications")


if __name__ == "__main__":
    main()
