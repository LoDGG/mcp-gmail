"""Safe batch triage: classify, persist, then optionally add existing labels."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from gmail_agent.classifier import BATCH_SIZE, BatchClassifierProvider, BatchReport, Usage, classify_search
from gmail_agent.history import HistoryDB
from gmail_agent.mcp_client import GmailMCPClient
from gmail_agent.taxonomy import Taxonomy, load_taxonomy

DEFAULT_LABEL_MAP_PATH = Path(__file__).resolve().parents[2] / "config" / "gmail_labels.json"
DEFAULT_QUERY = 'in:inbox -label:"Processed" newer_than:14d'
MAX_EMAILS = 10
AUTO_CONFIDENCE_THRESHOLD = 0.90


@dataclass(frozen=True)
class LabelMapping:
    category_labels: dict[str, str]
    review_label: str
    processed_label: str

    @property
    def required_labels(self) -> set[str]:
        return set(self.category_labels.values()) | {self.review_label, self.processed_label}


def load_label_mapping(path: Path = DEFAULT_LABEL_MAP_PATH, taxonomy: Taxonomy | None = None) -> LabelMapping:
    taxonomy = taxonomy or load_taxonomy()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    category_labels = dict(data["category_labels"])
    for category in taxonomy.categories:
        if category["active"] and "gmail_label" in category:
            name, label = category["name"], category["gmail_label"]
            if name == "UNCERTAIN" or (name in category_labels and category_labels[name] != label):
                raise ValueError("Taxonomy label conflicts with existing static mapping")
            category_labels[name] = label
    mapping = LabelMapping(category_labels, data["review_label"], data["processed_label"])
    expected = set(taxonomy.active_names) - {"UNCERTAIN"}
    if set(mapping.category_labels) != expected or len(mapping.required_labels) != len(expected) + 2:
        raise ValueError("Gmail label mapping must cover each active category with unique labels")
    if not all(isinstance(name, str) and name and name.strip() == name for name in mapping.required_labels):
        raise ValueError("Gmail label mapping contains an invalid label name")
    return mapping


class LabelBackend(Protocol):
    def ensure_triage_labels(self) -> dict[str, str]: ...
    def apply_label(self, message_id: str, label: str) -> dict: ...


@dataclass(frozen=True)
class TriageOutcome:
    message_id: str
    category: str
    confidence: float
    action_label: str
    processed: bool
    error: str | None = None


@dataclass(frozen=True)
class TriageReport:
    emails_found: int
    emails_classified: int
    outcomes: list[TriageOutcome]
    usage: list[Usage]
    batch_sizes: list[int]
    request_count: int
    saved_run: str | None


def _choice(result: dict, mapping: LabelMapping) -> str:
    if result["category"] != "UNCERTAIN" and result["confidence"] >= AUTO_CONFIDENCE_THRESHOLD:
        return mapping.category_labels[result["category"]]
    return mapping.review_label


async def triage_search(
    query: str, provider: BatchClassifierProvider, mcp: GmailMCPClient, db: HistoryDB,
    *, apply: bool = False, label_writes: bool = False, label_backend: LabelBackend | None = None,
    batch_size: int = BATCH_SIZE, max_emails: int = MAX_EMAILS,
    taxonomy: Taxonomy | None = None, mapping: LabelMapping | None = None,
) -> TriageReport:
    if apply and (not label_writes or label_backend is None):
        raise PermissionError("Applying labels requires --apply and GMAIL_LABEL_WRITES=1")
    taxonomy = taxonomy or load_taxonomy()
    mapping = mapping or load_label_mapping(taxonomy=taxonomy)

    if apply:
        # The backend reads the same static config and can create only its allowlist.
        label_backend.ensure_triage_labels()

    classified: BatchReport = await classify_search(
        query, provider, mcp, batch_size=batch_size, max_emails=max_emails, taxonomy=taxonomy,
    )
    saved_run = db.save_run(classified.results, taxonomy, provenance=classified.provenance) if classified.results else None
    outcomes = []
    for result in classified.results:
        label = _choice(result, mapping)
        processed = False
        error = None
        if apply:
            try:
                label_backend.apply_label(result["message_id"], label)
            except Exception:
                error = "primary label failed"
            else:
                try:
                    label_backend.apply_label(result["message_id"], mapping.processed_label)
                    processed = True
                except Exception:
                    error = "processed label failed"
        outcomes.append(TriageOutcome(result["message_id"], result["category"], result["confidence"], label, processed, error))
    return TriageReport(
        classified.emails_found, len(classified.results), outcomes,
        classified.usage, classified.batch_sizes, classified.request_count, saved_run,
    )
