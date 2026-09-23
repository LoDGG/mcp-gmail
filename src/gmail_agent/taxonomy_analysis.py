"""Deterministic local statistics and taxonomy proposal signals."""

from collections import Counter, defaultdict
from dataclasses import dataclass

from gmail_agent.history import ground_truth
from gmail_agent.taxonomy import Taxonomy

DEFAULT_SUBTYPE_MIN_MESSAGES = 3

PROPOSAL_TYPES = {"CREATE", "MERGE", "SPLIT", "DEPRECATE", "REFINE_DEFINITION"}


@dataclass(frozen=True)
class Proposal:
    proposal_type: str
    affected_categories: list[str]
    evidence_counts: dict[str, int]
    rationale: str
    expected_benefit: str
    evidence_strength: str
    status: str = "proposed"

    def __post_init__(self):
        if self.proposal_type not in PROPOSAL_TYPES or self.status not in {"proposed", "accepted", "rejected"}:
            raise ValueError("Invalid proposal type or status")


def recurring_subtypes(rows: list[dict], *, min_messages: int = DEFAULT_SUBTYPE_MIN_MESSAGES) -> list[dict]:
    """Unverified hints, grouped by reviewed category when available.

    Count each message once: prefer its latest reviewed record, otherwise its
    latest prediction. HistoryDB supplies records in ascending ID order.
    """
    if isinstance(min_messages, bool) or not isinstance(min_messages, int) or min_messages < 1:
        raise ValueError("subtype minimum messages must be a positive integer")
    selected = {}
    for row in rows:
        previous = selected.get(row["message_id"])
        if previous is None or ground_truth(row) is not None or ground_truth(previous) is None:
            selected[row["message_id"]] = row
    counts = Counter()
    reviewed_counts = Counter()
    for row in selected.values():
        hint = row.get("subtype_hint")
        if hint is None:
            continue
        truth = ground_truth(row)
        category = truth["category"] if truth is not None else row["predicted_category"]
        key = (category, hint)
        counts[key] += 1
        reviewed_counts[key] += int(truth is not None)
    return [
        {"category": category, "subtype_hint": hint, "distinct_messages": count,
         "reviewed_category_messages": reviewed_counts[(category, hint)],
         "unreviewed_category_messages": count - reviewed_counts[(category, hint)],
         "hint_status": "unverified"}
        for (category, hint), count in sorted(counts.items())
        if count >= min_messages
    ]


def subtype_review_stats(rows: list[dict], *, min_messages: int) -> dict:
    reviewed = [row for row in rows if row.get("subtype_review_status", "pending") != "pending"]
    decisions = Counter(row["subtype_review_status"] for row in reviewed)
    disagreements = Counter()
    selected = {}
    for row in sorted(reviewed, key=lambda row: (row.get("subtype_reviewed_at") or "", row.get("id", 0))):
        selected[row["message_id"]] = row
        if row["subtype_review_status"] in {"rejected", "corrected"}:
            disagreements[(row.get("subtype_hint"), row.get("corrected_subtype_hint"),
                           row["subtype_review_status"])] += 1
    approved = Counter()
    for row in selected.values():
        truth = ground_truth(row)
        if truth is None:
            continue
        status = row["subtype_review_status"]
        hint = row.get("corrected_subtype_hint") if status == "corrected" else row.get("subtype_hint")
        if status in {"accepted", "corrected"} and hint is not None:
            approved[(truth["category"], hint)] += 1
    disagreement_count = sum(disagreements.values())
    return {
        "subtype_reviewed": len(reviewed),
        "subtype_review_counts": {action: decisions[action] for action in ("accepted", "rejected", "corrected")},
        "model_subtype_disagreement_count": disagreement_count,
        "model_subtype_disagreement_rate": disagreement_count / len(reviewed) if reviewed else 0.0,
        "model_subtype_disagreements": [
            {"model_subtype": model, "human_subtype": human, "decision": decision, "count": count}
            for (model, human, decision), count in sorted(
                disagreements.items(), key=lambda item: tuple(value or "" for value in item[0]))
        ],
        "human_approved_subtype_candidates": [
            {"category": category, "subtype_hint": hint, "distinct_messages": count}
            for (category, hint), count in sorted(approved.items()) if count >= min_messages
        ],
    }


def provenance_performance(rows: list[dict]) -> list[dict]:
    """Per-observation review metrics; never infer reviews for new predictions."""
    fields = ("provider_id", "model_id", "taxonomy_version", "prompt_version")
    groups = defaultdict(list)
    for row in rows:
        groups[tuple(row.get(field) for field in fields)].append(row)
    result = []
    for key, observations in sorted(groups.items(), key=lambda item: tuple(value or "" for value in item[0])):
        reviewed = [row for row in observations if ground_truth(row) is not None]
        category_corrections = sum(
            ground_truth(row)["category"] != row["predicted_category"] for row in reviewed
        )
        subtype_counts = Counter(row.get("subtype_review_status", "pending") for row in observations)
        subtype_reviewed = sum(subtype_counts[action] for action in ("accepted", "corrected", "rejected"))
        result.append({
            **dict(zip(fields, key)),
            "prediction_count": len(observations),
            "reviewed_count": len(reviewed),
            "category_correction_count": category_corrections,
            "category_correction_rate": category_corrections / len(reviewed) if reviewed else None,
            "subtype_reviewed_count": subtype_reviewed,
            "subtype_acceptance_rate": subtype_counts["accepted"] / subtype_reviewed if subtype_reviewed else None,
            "subtype_correction_rate": subtype_counts["corrected"] / subtype_reviewed if subtype_reviewed else None,
            "subtype_rejection_rate": subtype_counts["rejected"] / subtype_reviewed if subtype_reviewed else None,
        })
    return result


def calculate_stats(rows: list[dict], *, subtype_min_messages: int = DEFAULT_SUBTYPE_MIN_MESSAGES) -> dict:
    reviewed = [row for row in rows if ground_truth(row) is not None]
    corrected = [row for row in reviewed if row["review_status"] == "corrected"]
    distribution = Counter(row["predicted_category"] for row in rows)
    confidence = {
        "below_0_5": sum(row["predicted_confidence"] < 0.5 for row in rows),
        "0_5_to_0_8": sum(0.5 <= row["predicted_confidence"] < 0.8 for row in rows),
        "0_8_and_above": sum(row["predicted_confidence"] >= 0.8 for row in rows),
    }
    confusion = defaultdict(Counter)
    correction_categories = Counter()
    for row in corrected:
        truth = ground_truth(row)
        if truth["category"] != row["predicted_category"]:
            confusion[row["predicted_category"]][truth["category"]] += 1
            correction_categories[truth["category"]] += 1
    reviewed_count = len(reviewed)
    return {
        "classified_emails": len({row["message_id"] for row in rows}),
        "classification_records": len(rows),
        "taxonomy_insufficiency_decisions": {
            action: sum(row.get("primary_review_action") == action for row in rows)
            for action in ("new_label", "accept_proposed_label")
        },
        "aggregate_scope": "All provenance groups combined; use performance_by_provenance for model comparisons",
        "performance_by_provenance": provenance_performance(rows),
        "subtype_min_messages": subtype_min_messages,
        "subtype_candidates": recurring_subtypes(rows, min_messages=subtype_min_messages),
        "reviewed": reviewed_count,
        "accepted_category_rate": sum(ground_truth(row)["category"] == row["predicted_category"] for row in reviewed) / reviewed_count if reviewed_count else 0.0,
        "category_correction_rate": sum(correction_categories.values()) / reviewed_count if reviewed_count else 0.0,
        **subtype_review_stats(rows, min_messages=subtype_min_messages),
        "category_distribution": dict(distribution),
        "uncertain_rate": distribution["UNCERTAIN"] / len(rows) if rows else 0.0,
        "confidence_distribution": confidence,
        "correction_rate": len(corrected) / reviewed_count if reviewed_count else 0.0,
        "confusion_matrix": {source: dict(targets) for source, targets in confusion.items()},
        "needs_reply_disagreement_rate": sum(ground_truth(row)["needs_reply"] != bool(row["predicted_needs_reply"]) for row in reviewed) / reviewed_count if reviewed_count else 0.0,
        "importance_disagreement_rate": sum(ground_truth(row)["importance"] != row["predicted_importance"] for row in reviewed) / reviewed_count if reviewed_count else 0.0,
        "category_frequency_among_corrections": dict(correction_categories),
    }


def generate_candidates(rows: list[dict], taxonomy: Taxonomy) -> list[Proposal]:
    """Emit evidence signals only; never change taxonomy or infer new names."""
    reviewed = [row for row in rows if ground_truth(row) is not None]
    corrections = Counter(
        (row["predicted_category"], ground_truth(row)["category"])
        for row in reviewed if ground_truth(row)["category"] != row["predicted_category"]
    )
    candidates: list[Proposal] = []
    uncertain = [row for row in reviewed if row["predicted_category"] == "UNCERTAIN" and ground_truth(row)["category"] == "UNCERTAIN"]
    if len(uncertain) >= 3:
        at_cap = len(taxonomy.active_names) >= taxonomy.max_primary_categories
        candidates.append(Proposal(
            "CREATE", ["UNCERTAIN"], {"reviewed_unresolved_uncertain": len(uncertain)},
            "Repeated reviewed UNCERTAIN cases may indicate an unmet need; semantic analysis is required before naming a category."
            + (" A merge or deprecation is required before adoption at the hard category cap." if at_cap else ""),
            "Reduce recurring unresolved cases if a distinct need is confirmed.", "moderate" if len(uncertain) < 6 else "strong",
        ))
    merged_pairs = set()
    for (source, target), count in sorted(corrections.items()):
        reverse = corrections[(target, source)]
        pair = tuple(sorted((source, target)))
        if source == target or pair in merged_pairs:
            continue
        if count >= 2 and reverse >= 2 and _similar_action_profiles(reviewed, source, target):
            merged_pairs.add(pair)
            candidates.append(Proposal(
                "MERGE", list(pair), {"forward": count, "reverse": reverse},
                "Repeated mutual corrections and similar reviewed action profiles suggest overlap; inspect examples before adoption.",
                "Simplify boundaries without adding categories.", "moderate" if count + reverse < 8 else "strong",
            ))
    for (source, target), count in sorted(corrections.items()):
        if count >= 3 and tuple(sorted((source, target))) not in merged_pairs:
            candidates.append(Proposal(
                "REFINE_DEFINITION", [source, target], {"corrections": count},
                "Repeated one-way boundary correction; inspect examples to clarify positive and exclusion guidance.",
                "Reduce ambiguity while retaining useful categories.", "moderate" if count < 6 else "strong",
            ))
    for category in taxonomy.active_names:
        if category == "UNCERTAIN":
            continue
        category_rows = [row for row in reviewed if ground_truth(row)["category"] == category]
        replies = sum(ground_truth(row)["needs_reply"] for row in category_rows)
        if len(category_rows) >= 6 and replies >= 2 and len(category_rows) - replies >= 2:
            candidates.append(Proposal(
                "SPLIT", [category], {"reviewed": len(category_rows), "needs_reply": replies, "no_reply": len(category_rows) - replies},
                "Reviewed action profiles differ within this category; semantic analysis is required before defining a split.",
                "Separate materially different handling if examples support it.", "weak",
            ))
    if len(reviewed) >= 20:
        predicted = Counter(row["predicted_category"] for row in rows)
        actual = Counter(ground_truth(row)["category"] for row in reviewed)
        for category in taxonomy.active_names:
            if category != "UNCERTAIN" and predicted[category] == 0 and actual[category] == 0:
                candidates.append(Proposal(
                    "DEPRECATE", [category], {"predictions": 0, "reviewed_assignments": 0, "reviewed_sample": len(reviewed)},
                    "No observed use in a reviewed sample; inspect mailbox coverage before adoption.",
                    "Reduce unused taxonomy complexity.", "weak",
                ))
    return candidates


def _similar_action_profiles(reviewed: list[dict], left: str, right: str) -> bool:
    profiles = []
    for category in (left, right):
        values = [ground_truth(row) for row in reviewed if ground_truth(row)["category"] == category]
        if len(values) < 2:
            return False
        profiles.append((
            sum(item["needs_reply"] for item in values) / len(values),
            sum(item["importance"] == "high" for item in values) / len(values),
        ))
    return all(abs(a - b) <= 0.25 for a, b in zip(*profiles))
