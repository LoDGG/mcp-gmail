"""Explicit human-approved CREATE activation with recoverable local state."""

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
from uuid import uuid4

from gmail_agent.gmail_labels import validate_user_label
from gmail_agent.history import HistoryDB, _now
from gmail_agent.label_suggestions import normalize_concept, represented_concept
from gmail_agent.taxonomy import DEFAULT_TAXONOMY_PATH, taxonomy_from_data
from gmail_agent.triage import DEFAULT_LABEL_MAP_PATH, load_label_mapping


class ActivationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ActivationPlan:
    proposal_id: int
    concept_key: str
    display_label: str
    category: dict
    taxonomy_version: str
    approved_at: str
    original: bytes
    updated: dict
    audit: dict | None
    configured: bool

    def summary(self) -> dict:
        return {
            "proposal_id": self.proposal_id, "concept_key": self.concept_key,
            "category_id": self.category["name"], "display_label": self.display_label,
            "definition": self.category["definition"], "taxonomy_version": self.taxonomy_version,
            "approved_at": self.approved_at,
            "status": (self.audit or {}).get("status", "human_approved"),
            "gmail_label_id": (self.audit or {}).get("gmail_label_id"),
        }


def _next_version(version: str) -> str:
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
        raise ValueError("Taxonomy version must use major.minor.patch")
    major, minor, _ = map(int, version.split("."))
    return f"{major}.{minor + 1}.0"


def prepare_activation(
    db: HistoryDB, concept: str, *, taxonomy_path: Path = DEFAULT_TAXONOMY_PATH,
    label_map_path: Path = DEFAULT_LABEL_MAP_PATH, definition: str | None = None,
    display_label: str | None = None,
) -> ActivationPlan:
    concept = normalize_concept(concept)
    proposal = next((item for item in db.proposals()
                     if item["proposal_type"] == "CREATE" and item.get("concept_key") == concept), None)
    if proposal is None:
        raise ValueError("CREATE proposal does not exist")
    approved = [item for item in db.proposal_supports() if item["proposal_id"] == proposal["id"]
                and item["source"] in {"human_manual", "human_proposal_accept"}]
    if not approved:
        raise ValueError("Proposal has no explicit human approval from NEW LABEL or ACCEPT PROPOSED LABEL")
    audit = next((item for item in db.activation_records() if item["proposal_id"] == proposal["id"]), None)
    if proposal["status"] != "proposed" and not (
        proposal["status"] == "accepted" and audit
    ):
        raise ValueError("Proposal is rejected or resolved incompatibly")
    if audit and audit["taxonomy_path"] != str(Path(taxonomy_path).resolve()):
        raise ValueError("Activation is already bound to a different taxonomy file")
    if proposal["concept_key"] != normalize_concept(proposal["concept_key"]):
        raise ValueError("Stored concept must be normalized")
    category_id = concept.upper()
    if len(category_id) > 40 or not re.fullmatch(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*", category_id):
        raise ValueError("Invalid internal category identifier")
    saved_category = json.loads(audit["category_json"]) if audit else None
    label = display_label if display_label is not None else (
        audit["display_label"] if audit else proposal.get("display_label")
    )
    validate_user_label(label)
    definition = definition if definition is not None else (saved_category or {}).get("definition")
    if not isinstance(definition, str) or not definition.strip() or len(definition) > 500 or not all(c.isprintable() for c in definition):
        raise ValueError("Supply --definition: a meaningful printable category definition of at most 500 characters")
    definition = definition.strip()
    if len(definition.split()) < 3:
        raise ValueError("Definition must describe the concept, not just repeat its name")
    if audit and (label != audit["display_label"] or definition != saved_category["definition"]):
        raise ValueError("An activation already started with a different display label or definition")
    original = Path(taxonomy_path).read_bytes()
    data = json.loads(original)
    taxonomy = taxonomy_from_data(data)
    mapping = load_label_mapping(label_map_path, taxonomy)
    token = audit["activation_id"] if audit else str(uuid4())
    category = saved_category or {
        "name": category_id,
        "definition": definition,
        "positive_guidance": "Use for messages matching this definition: " + definition,
        "negative_guidance": "Use another existing category when this definition does not fit.",
        "active": True,
        "concept_key": concept,
        "gmail_label": label,
        "activation_id": token,
    }
    existing = next((item for item in taxonomy.categories if item["name"].casefold() == category_id.casefold()), None)
    configured = existing is not None
    if configured:
        if not audit or existing != category:
            raise ValueError("Category duplicates an existing taxonomy category")
    elif audit and audit["status"] == "active":
        raise ValueError("Active proposal is missing its taxonomy category; inspect configuration before retrying")
    elif represented_concept(concept, taxonomy):
        raise ValueError("Concept duplicates an existing taxonomy category")
    for item in taxonomy.categories:
        if item["name"] != category_id and label.casefold() in {
            item["name"].casefold(), str(item.get("gmail_label", "")).casefold(),
        }:
            raise ValueError("Display label conflicts with an existing taxonomy category")
    existing_labels = {name: value for name, value in mapping.category_labels.items() if name != category_id}
    if label.casefold() in {value.casefold() for value in (
        *existing_labels.values(), mapping.review_label, mapping.processed_label,
    )}:
        raise ValueError("Display label conflicts with an existing configured label")
    if not configured and len(taxonomy.active_names) >= taxonomy.max_primary_categories:
        raise ValueError("Taxonomy primary-category cap would be exceeded")
    version = audit["taxonomy_version"] if configured else _next_version(taxonomy.version)
    updated = data if configured else {
        **data, "version": version, "categories": [*data["categories"], category],
    }
    # Validate the complete future taxonomy and mapping before Gmail is contacted.
    load_label_mapping(label_map_path, taxonomy_from_data(updated))
    return ActivationPlan(proposal["id"], concept, label, category, version,
                          min(item["created_at"] for item in approved), original, updated, audit, configured)


def atomic_write_taxonomy(path: Path, data: dict, expected: bytes) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), path.stat().st_mode & 0o777)
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if path.read_bytes() != expected:
            raise ValueError("Taxonomy changed during activation; retry against the new configuration")
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def _activation_lock(path: Path):
    with path.with_name(path.name + ".activation.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ActivationError("Another taxonomy activation is in progress") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _finish_activation(db: HistoryDB, plan: ActivationPlan) -> None:
    with db.conn:
        db.conn.execute("""
            UPDATE taxonomy_activations SET status = 'active', activated_at = COALESCE(activated_at, ?),
                updated_at = ?, last_error = NULL WHERE proposal_id = ?
        """, (_now(), _now(), plan.proposal_id))
        db.conn.execute("UPDATE taxonomy_proposals SET status = 'accepted', updated_at = ? WHERE id = ?",
                        (_now(), plan.proposal_id))


def activate_proposal(
    db: HistoryDB, concept: str, *, apply: bool = False, label_writes: bool = False,
    taxonomy_path: Path = DEFAULT_TAXONOMY_PATH, label_map_path: Path = DEFAULT_LABEL_MAP_PATH,
    definition: str | None = None, display_label: str | None = None, backend_factory=None,
) -> dict:
    taxonomy_path = Path(taxonomy_path)
    options = dict(taxonomy_path=taxonomy_path, label_map_path=label_map_path,
                   definition=definition, display_label=display_label)
    if not apply:
        return {**prepare_activation(db, concept, **options).summary(), "dry_run": True}
    if not label_writes:
        raise PermissionError("Activation requires --apply AND GMAIL_LABEL_WRITES=1")
    with _activation_lock(taxonomy_path):
        plan = prepare_activation(db, concept, **options)
        if plan.audit and plan.audit["status"] == "active":
            return {**plan.summary(), "dry_run": False}
        if backend_factory is None and not plan.configured:
            raise ValueError("An explicitly gated Gmail backend is required")
        with db.conn:
            db.conn.execute("""
                INSERT INTO taxonomy_activations (
                    proposal_id, activation_id, concept_key, category_id, display_label, category_json,
                    taxonomy_path, approved_at, taxonomy_version, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'activating', ?)
                ON CONFLICT(proposal_id) DO UPDATE SET status = 'activating',
                    taxonomy_version = excluded.taxonomy_version, updated_at = excluded.updated_at, last_error = NULL
            """, (plan.proposal_id, plan.category["activation_id"], plan.concept_key, plan.category["name"],
                  plan.display_label, json.dumps(plan.category), str(taxonomy_path.resolve()),
                  plan.approved_at, plan.taxonomy_version, _now()))
        gmail_id = (plan.audit or {}).get("gmail_label_id")
        stage = "Gmail label resolution"
        try:
            if not plan.configured:
                backend = backend_factory()
                gmail_id = backend.ensure_approved_label(plan.display_label, approved_names=frozenset({plan.display_label}))
                if not isinstance(gmail_id, str) or not gmail_id:
                    raise ValueError("Gmail backend returned no user label ID")
                with db.conn:
                    db.conn.execute("UPDATE taxonomy_activations SET gmail_label_id = ?, updated_at = ? WHERE proposal_id = ?",
                                    (gmail_id, _now(), plan.proposal_id))
                stage = "taxonomy update"
                atomic_write_taxonomy(taxonomy_path, plan.updated, plan.original)
            elif not gmail_id:
                raise ValueError("Configured activation lacks its recorded Gmail label ID")
            stage = "audit finalization"
            _finish_activation(db, plan)
        except Exception as exc:
            message = (
                f"Activation failed during {stage}. "
                + (f"Gmail user label {plan.display_label!r} ({gmail_id}) is retained. " if gmail_id
                   else "Taxonomy was not changed; Gmail creation may have completed before an API failure. ")
                + "Retry the same proposal; existing Gmail labels and this activation's category will be reused."
            )
            try:
                with db.conn:
                    db.conn.execute("UPDATE taxonomy_activations SET status = 'failed', last_error = ?, updated_at = ? WHERE proposal_id = ?",
                                    (message, _now(), plan.proposal_id))
            except Exception:
                pass  # The committed 'activating' record still supports recovery.
            raise ActivationError(message) from exc
        return {**plan.summary(), "status": "active", "gmail_label_id": gmail_id, "dry_run": False}
