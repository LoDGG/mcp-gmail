"""Compact local review of saved predictions; no provider or Gmail access."""

import argparse
import shlex

from gmail_agent.history import HistoryDB
from gmail_agent.label_suggestions import SuggestionPolicy
from gmail_agent.taxonomy import load_taxonomy


def _display(value, limit=120):
    text = str(value) if value is not None else "unavailable"
    return "".join(char if char.isprintable() else " " for char in text)[:limit]


def _subtype_action(parts):
    if not parts:
        return None, None
    action = {"sa": "accepted", "sr": "rejected", "sc": "corrected"}.get(parts[0].lower())
    if action is None or len(parts) != (2 if action == "corrected" else 1):
        raise ValueError("Use sa, sr, or sc snake_case_hint")
    return action, parts[1] if action == "corrected" else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Review saved classifications locally")
    parser.add_argument("--db", help="SQLite database path")
    parser.add_argument("--limit", type=int, help="Maximum items shown (including skipped items)")
    queue = parser.add_mutually_exclusive_group()
    queue.add_argument("--only-unreviewed", dest="only_unreviewed", action="store_true", default=True,
                       help="Show items with pending category or subtype decisions (default)")
    queue.add_argument("--all", dest="only_unreviewed", action="store_false",
                       help="Include completed items; existing decisions cannot be overwritten")
    for name, default in SuggestionPolicy().__dict__.items():
        parser.add_argument("--label-" + name.replace("_", "-"), type=int, default=default,
                            help=f"New-label suggestion {name.replace('_', ' ')} (default: {default})")
    args = parser.parse_args()
    try:
        policy = SuggestionPolicy(**{name: getattr(args, "label_" + name) for name in SuggestionPolicy().__dict__})
    except ValueError as exc:
        parser.error(str(exc))
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    taxonomy = load_taxonomy()
    print('[a] accept | [c CATEGORY] correct | [n concept ["Display"]] new label | [s] skip | [q] quit')
    print("Optional subtype: sa=accept, sr=reject, sc HINT=correct; use alone or after a/c.")
    print("Categories: " + ", ".join(taxonomy.active_names))
    with HistoryDB(args.db) as db:
        rows = db.records()
        if args.only_unreviewed:
            rows = [row for row in rows if row.get("primary_review_action") not in {"new_label", "accept_proposed_label"}
                    and (row["review_status"] == "pending" or (
                        row["subtype_review_status"] == "pending" and row["subtype_hint"] is not None))]
        rows = rows[:args.limit]
        for position, row in enumerate(rows, 1):
            print(f"\n[{position}/{len(rows)}] {_display(row['message_id'])} | date={_display(row['email_date'])}")
            print(f"From: {_display(row['email_sender'])} | Subject: {_display(row['email_subject'])}")
            print(f"Predicted: {row['predicted_category']} | Confidence: {row['predicted_confidence']:.2f} | "
                  f"Subtype: {_display(row['subtype_hint'])} | reply={bool(row['predicted_needs_reply'])} | "
                  f"importance={row['predicted_importance']}"
                  + (f" | deadline={_display(row['predicted_deadline'])}" if row["predicted_deadline"] else ""))
            if row["review_status"] != "pending" or row["subtype_review_status"] != "pending":
                print(f"Saved: category={row['corrected_category'] or row['predicted_category']} "
                      f"({row['review_status']}), subtype={row['corrected_subtype_hint'] or row['subtype_hint'] or '-'} "
                      f"({row['subtype_review_status']})")
            suggestion = db.label_suggestion(row["id"], taxonomy=taxonomy, policy=policy)
            if suggestion:
                evidence = suggestion.evidence
                print(f"Suggested new label: {_display(suggestion.concept_key)} | display: {_display(suggestion.display_label)}")
                print(f"Evidence: {evidence['distinct_messages']} distinct messages, "
                      f"{evidence['distinct_senders']} senders, {evidence['distinct_runs']} runs, "
                      f"{evidence['human_approved_subtypes']} human-approved subtype hints, "
                      f"{evidence['manual_new_label_messages']} manual proposals, "
                      f"{evidence['category_correction_messages']} category corrections, "
                      f"{evidence['raw_subtype_messages']} raw hints | score={suggestion.score}")
                if suggestion.proposal_id is not None:
                    print(f"Reuses pending CREATE proposal #{suggestion.proposal_id}")
                print("[p] accept proposed label")
            if row.get("primary_review_action") in {"new_label", "accept_proposed_label"}:
                print(f"Saved: {row['primary_review_action']} (taxonomy insufficiency)")
            while True:
                try:
                    parts = shlex.split(input("> ").strip())
                    if not parts:
                        continue
                    action = parts.pop(0).lower()
                    if action in {"q", "quit"} and not parts:
                        return
                    if action in {"s", "skip"} and not parts:
                        break
                    if action in {"n", "new"}:
                        if len(parts) not in {1, 2}:
                            raise ValueError('Use n concept or n concept "Display name"')
                        proposal_id = db.propose_label(row["id"], parts[0], parts[1] if len(parts) == 2 else None,
                                                       taxonomy=taxonomy)
                        print(f"Saved CREATE proposal #{proposal_id}; no labels changed.")
                        break
                    if action in {"p", "proposed"}:
                        if parts or suggestion is None:
                            raise ValueError("No strong label suggestion is available")
                        proposal_id = db.propose_label(row["id"], accept_suggested=True, taxonomy=taxonomy, policy=policy)
                        print(f"Approved candidate #{proposal_id} for later taxonomy approval; no labels changed.")
                        break
                    category = None
                    if action in {"c", "correct"}:
                        category = (parts.pop(0) if parts else input("Category: ").strip()).upper()
                        if category not in taxonomy.active_names:
                            raise ValueError("Unknown category; choose one from the category list")
                        if category == row["predicted_category"]:
                            raise ValueError("Category unchanged; use accept")
                    if action in {"a", "accept", "c", "correct"}:
                        subtype_action, hint = _subtype_action(parts)
                        db.review(row["id"], "corrected" if category else "accepted",
                                  category=category, taxonomy=taxonomy,
                                  subtype_action=subtype_action, corrected_subtype=hint)
                    else:
                        subtype_action, hint = _subtype_action([action, *parts])
                        db.review_subtype(row["id"], subtype_action, corrected_subtype=hint)
                    break
                except ValueError as exc:
                    print(exc)
                except (EOFError, KeyboardInterrupt):
                    print("\nReview stopped; completed decisions are saved.")
                    return
        print("Review session complete.")


if __name__ == "__main__":
    main()
