"""Deterministic local statistics and taxonomy proposal signals."""

from collections import Counter, defaultdict
from dataclasses import dataclass

from gmail_agent.history import ground_truth
from gmail_agent.taxonomy import Taxonomy

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


def calculate_stats(rows: list[dict]) -> dict:
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
        "reviewed": reviewed_count,
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
