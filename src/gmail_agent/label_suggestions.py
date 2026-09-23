"""Conservative, inspectable local CREATE suggestions. No API access."""

from collections import Counter
from dataclasses import dataclass
from email.utils import parseaddr
import re

from gmail_agent.subtypes import validate_subtype_hint
from gmail_agent.taxonomy import Taxonomy

# Only conservative lexical equivalences; no fuzzy semantic guessing.
CONCEPT_ALIASES = {
    "job_offers": "job_offer", "employment_offer": "job_offer",
    "shipping_updates": "shipping_update", "delivery_update": "shipping_update",
    "subscription_invoices": "subscription_invoice",
}
CATEGORY_ALIASES = {
    "ADMIN": {"administration", "administrative", "account_administration"},
    "FINANCE": {"financial", "billing", "banking"},
    "PURCHASE": {"purchases", "shopping", "orders", "order", "delivery", "deliveries"},
    "APPOINTMENT": {"appointments", "meeting", "meetings", "booking", "bookings"},
    "TRAVEL": {"journey", "journeys", "trip", "trips"},
    "PROFESSIONAL": {"work", "career", "business", "professional_message"},
    "PERSONAL": {"friends", "family", "personal_message"},
    "SECURITY": {"security_alert", "account_security", "security_event"},
    "NEWSLETTER": {"newsletters", "digest", "digests"},
    "NOTIFICATION": {"notifications", "notification_message", "automated_notification"},
    "UNCERTAIN": {"unknown", "uncategorized", "other"},
}
MONTHS = set("january february march april may june july august september october november december".split())


def normalize_concept(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Concept must be a short semantic name")
    concept = re.sub(r"[\s-]+", "_", value.strip().lower())
    concept = CONCEPT_ALIASES.get(concept, concept)
    validate_subtype_hint(concept, "")
    return concept


def represented_concept(concept: str, taxonomy: Taxonomy) -> bool:
    for category in taxonomy.active_names:
        name = category.lower()
        if concept in {name, name + "s", *CATEGORY_ALIASES.get(category, set())}:
            return True
        if concept in {name + "_" + suffix for suffix in ("mail", "email", "message", "update", "notice")}:
            return True
    return False


def _eligible(concept: str, taxonomy: Taxonomy) -> bool:
    words = concept.split("_")
    return (
        not represented_concept(concept, taxonomy)
        and len(words) <= 3
        and not any(char.isdigit() for char in concept)
        and not set(words) & (MONTHS | {"from", "company", "email", "mail", "message"})
    )


def _concept(value):
    try:
        return normalize_concept(value) if value else None
    except ValueError:
        return None


@dataclass(frozen=True)
class SuggestionPolicy:
    human_min_messages: int = 3
    human_min_confirmations: int = 2
    model_min_messages: int = 8
    model_min_senders: int = 3
    model_min_runs: int = 2
    human_min_score: int = 15
    model_min_score: int = 13

    def __post_init__(self):
        for value in self.__dict__.values():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("Suggestion thresholds must be positive integers")


@dataclass(frozen=True)
class LabelSuggestion:
    concept_key: str
    display_label: str
    evidence: dict[str, int]
    score: int
    proposal_id: int | None = None
    supporting_evidence: tuple[tuple[int, str], ...] = ()


def suggest_label(
    current: dict, rows: list[dict], proposals: list[dict], supports: list[dict],
    taxonomy: Taxonomy, policy: SuggestionPolicy = SuggestionPolicy(),
) -> LabelSuggestion | None:
    if current.get("primary_review_action") is not None or current["review_status"] != "pending":
        return None
    if current.get("subtype_review_status") == "rejected":
        return None
    concept = _concept(current.get("corrected_subtype_hint") or current.get("subtype_hint"))
    if not concept or not _eligible(concept, taxonomy):
        return None

    existing = None
    for proposal in proposals:
        if proposal["proposal_type"] != "CREATE":
            continue
        key = _concept(proposal.get("concept_key"))
        # Legacy named CREATE proposals have no concept column; avoid duplicates.
        legacy_keys = {_concept(name) for name in proposal["affected_categories"]} if key is None else set()
        if key == concept:
            if proposal["status"] != "proposed" or proposal.get("ready_for_approval"):
                return None
            existing = proposal  # Reuse a pending manual candidate; never create another.
        elif concept in legacy_keys:
            return None

    # One observation per message. A reviewed subtype takes precedence over later raw guesses.
    selected = {}
    for row in rows:
        previous = selected.get(row["message_id"])
        reviewed = row.get("subtype_review_status", "pending") != "pending"
        if previous is None or reviewed or previous.get("subtype_review_status", "pending") == "pending":
            selected[row["message_id"]] = row
    by_proposal = {item["id"]: _concept(item.get("concept_key")) for item in proposals}
    manual = {item["message_id"] for item in supports
              if item["source"] == "human_manual" and by_proposal.get(item["proposal_id"]) == concept}
    approvals = {item["message_id"] for item in supports
                 if item["source"] == "human_proposal_accept" and by_proposal.get(item["proposal_id"]) == concept}
    supporting, raw, approved, corrected, broad = set(), set(), set(), set(), set()
    accepted_categories = Counter()
    corrected_categories = set()
    for message_id, row in selected.items():
        status = row.get("subtype_review_status", "pending")
        hint = _concept(row.get("corrected_subtype_hint") if status == "corrected" else row.get("subtype_hint"))
        if status == "rejected" or hint != concept:
            continue
        supporting.add(message_id)
        if _concept(row.get("subtype_hint")) == concept:
            raw.add(message_id)
        if status in {"accepted", "corrected"}:
            approved.add(message_id)
        if row["review_status"] in {"accepted", "corrected"} and (
            not row.get("corrected_category") or row["corrected_category"] == row["predicted_category"]
        ):
            accepted_categories[row["predicted_category"]] += 1
        if row.get("corrected_category") and row["corrected_category"] != row["predicted_category"]:
            corrected.add(message_id)
            corrected_categories.add(row["corrected_category"])
        if row["predicted_category"] in {"NOTIFICATION", "UNCERTAIN"}:
            broad.add(message_id)
    supporting |= manual | approvals
    if not supporting:
        return None
    # Repeated human acceptance in one existing category is evidence of adequate fit.
    if accepted_categories and max(accepted_categories.values()) >= 2 and not manual:
        return None
    if len(corrected) >= 2 and len(corrected_categories) == 1 and not manual:
        return None
    # A subtype is not automatically a taxonomy deficiency. Require explicit human
    # proposals, conflicting human category corrections, or predominantly catch-all placement.
    insufficient_fit = (
        bool(manual)
        or (len(corrected) >= 2 and len(corrected_categories) >= 2)
        or len(broad) >= max(2, (3 * len(supporting) + 3) // 4)
    )
    if not insufficient_fit:
        return None
    senders, runs = set(), set()
    for message_id in supporting:
        row = selected.get(message_id)
        if row is None:
            continue
        address = parseaddr(row.get("email_sender") or "")[1].lower()
        if address and "@" in address:
            senders.add(address)
        if row.get("run_id") is not None:
            runs.add(row["run_id"])
    human = approved | manual | approvals
    evidence = {
        "distinct_messages": len(supporting), "distinct_senders": len(senders),
        "human_approved_subtypes": len(approved), "manual_new_label_messages": len(manual),
        "proposal_approval_messages": len(approvals), "human_confirmed_messages": len(human),
        "category_correction_messages": len(corrected), "distinct_runs": len(runs),
        "raw_subtype_messages": len(raw),
    }
    # Explicit weights; a human-confirmed message counts five times a raw hint.
    score = (len(raw) + 5 * len(human) + 2 * len(manual) + 2 * len(corrected)
             + min(len(senders), 3) + min(len(runs), 2))
    human_gate = (len(supporting) >= policy.human_min_messages
                  and len(human) >= policy.human_min_confirmations and score >= policy.human_min_score)
    model_gate = (len(supporting) >= policy.model_min_messages
                  and len(senders) >= policy.model_min_senders
                  and len(runs) >= policy.model_min_runs and score >= policy.model_min_score)
    if not (human_gate or model_gate):
        return None
    return LabelSuggestion(
        concept, (existing or {}).get("display_label") or concept.replace("_", " ").capitalize(),
        evidence, score, existing["id"] if existing else None,
        tuple((selected[message_id]["id"], source)
              for messages, source in ((raw, "model_subtype"), (approved, "human_subtype"),
                                       (corrected, "category_correction"))
              for message_id in sorted(messages)),
    )
