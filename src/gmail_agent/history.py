"""Local classification and human-review history. Only bounded review headers are stored; never bodies or snippets."""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from gmail_agent.label_suggestions import SuggestionPolicy, normalize_concept, represented_concept, suggest_label
from gmail_agent.provenance import PredictionProvenance
from gmail_agent.subtypes import validate_subtype_hint
from gmail_agent.taxonomy import IMPORTANCE_VALUES, Taxonomy, load_taxonomy

DEFAULT_DB_PATH = Path(".local/gmail_agent.db")
UNSET = object()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class HistoryDB:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or os.environ.get("GMAIL_AGENT_DB", DEFAULT_DB_PATH))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._initialize()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def _initialize(self) -> None:
        with self.conn:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS classification_runs (
                    id INTEGER PRIMARY KEY,
                    run_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    taxonomy_version TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS classifications (
                    id INTEGER PRIMARY KEY,
                    run_id INTEGER NOT NULL REFERENCES classification_runs(id),
                    message_id TEXT NOT NULL,
                    classified_at TEXT NOT NULL,
                    taxonomy_version TEXT NOT NULL,
                    predicted_category TEXT NOT NULL,
                    predicted_confidence REAL NOT NULL CHECK(predicted_confidence BETWEEN 0 AND 1),
                    predicted_needs_reply INTEGER NOT NULL CHECK(predicted_needs_reply IN (0, 1)),
                    predicted_importance TEXT NOT NULL CHECK(predicted_importance IN ('low', 'normal', 'high')),
                    predicted_deadline TEXT,
                    subtype_hint TEXT,
                    short_reason TEXT NOT NULL,
                    review_status TEXT NOT NULL DEFAULT 'pending' CHECK(review_status IN ('pending', 'accepted', 'corrected')),
                    reviewed_at TEXT,
                    corrected_category TEXT,
                    corrected_needs_reply INTEGER CHECK(corrected_needs_reply IN (0, 1)),
                    corrected_importance TEXT CHECK(corrected_importance IN ('low', 'normal', 'high')),
                    corrected_deadline TEXT,
                    corrected_deadline_set INTEGER NOT NULL DEFAULT 0 CHECK(corrected_deadline_set IN (0, 1)),
                    UNIQUE(run_id, message_id)
                );
                CREATE TABLE IF NOT EXISTS taxonomy_proposals (
                    id INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    taxonomy_version TEXT NOT NULL,
                    proposal_type TEXT NOT NULL,
                    affected_categories TEXT NOT NULL,
                    evidence_counts TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    expected_benefit TEXT NOT NULL,
                    evidence_strength TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('proposed', 'accepted', 'rejected'))
                );
            """)

            proposal_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(taxonomy_proposals)")}
            for name, definition in {
                "concept_key": "TEXT", "display_label": "TEXT", "updated_at": "TEXT",
                "ready_for_approval": "INTEGER NOT NULL DEFAULT 0 CHECK(ready_for_approval IN (0, 1))",
            }.items():
                if name not in proposal_columns:
                    self.conn.execute(f"ALTER TABLE taxonomy_proposals ADD COLUMN {name} {definition}")
            self.conn.executescript("""
                CREATE UNIQUE INDEX IF NOT EXISTS create_proposal_concept
                ON taxonomy_proposals(concept_key) WHERE proposal_type = 'CREATE' AND concept_key IS NOT NULL;
                CREATE TABLE IF NOT EXISTS taxonomy_proposal_support (
                    id INTEGER PRIMARY KEY,
                    proposal_id INTEGER NOT NULL REFERENCES taxonomy_proposals(id),
                    classification_id INTEGER NOT NULL REFERENCES classifications(id),
                    message_id TEXT NOT NULL,
                    source TEXT NOT NULL CHECK(source IN ('human_manual', 'human_proposal_accept',
                        'model_subtype', 'human_subtype', 'category_correction')),
                    display_label TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(proposal_id, message_id, source)
                );
            """)

            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS taxonomy_activations (
                    proposal_id INTEGER PRIMARY KEY REFERENCES taxonomy_proposals(id),
                    activation_id TEXT NOT NULL UNIQUE,
                    concept_key TEXT NOT NULL,
                    category_id TEXT NOT NULL,
                    display_label TEXT NOT NULL,
                    category_json TEXT NOT NULL,
                    taxonomy_path TEXT NOT NULL,
                    gmail_label_id TEXT,
                    approved_at TEXT NOT NULL,
                    activated_at TEXT,
                    taxonomy_version TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('activating', 'active', 'failed')),
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                )
            """)

            run_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(classification_runs)")}
            for name in ("provider_id", "model_id", "prompt_version"):
                if name not in run_columns:
                    # Unknown historical provenance stays NULL; never infer it.
                    self.conn.execute(f"ALTER TABLE classification_runs ADD COLUMN {name} TEXT")

            columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(classifications)")}
            additions = {
                "primary_review_action": "TEXT CHECK(primary_review_action IN ('accept', 'correct', 'new_label', 'accept_proposed_label'))",
                "subtype_hint": "TEXT",
                "email_date": "TEXT",
                "email_sender": "TEXT",
                "email_subject": "TEXT",
                "subtype_review_status": "TEXT NOT NULL DEFAULT 'pending' CHECK(subtype_review_status IN ('pending', 'accepted', 'rejected', 'corrected'))",
                "corrected_subtype_hint": "TEXT",
                "subtype_reviewed_at": "TEXT",
            }
            for name, definition in additions.items():
                if name not in columns:
                    self.conn.execute(f"ALTER TABLE classifications ADD COLUMN {name} {definition}")

    def save_run(
        self, results: list[dict], taxonomy: Taxonomy | None = None, *,
        run_key: str | None = None, provenance: PredictionProvenance | None = None,
    ) -> str:
        taxonomy = taxonomy or load_taxonomy()
        provenance = provenance or PredictionProvenance()
        if provenance.taxonomy_version is not None and provenance.taxonomy_version != taxonomy.version:
            raise ValueError("Provenance taxonomy version does not match classification taxonomy")
        run_key = run_key or str(uuid4())
        if len({item["message_id"] for item in results}) != len(results):
            raise ValueError("Duplicate message ID in classification run")
        for item in results:
            validate_subtype_hint(item.get("subtype_hint"), item["category"])
            if item["category"] not in taxonomy.active_names or item["importance"] not in IMPORTANCE_VALUES:
                raise ValueError("Classification does not match active taxonomy")
            if not 0 <= item["confidence"] <= 1 or not isinstance(item["needs_reply"], bool):
                raise ValueError("Invalid classification attributes")
            if len(item["short_reason"]) > 120 or (item["deadline"] is not None and len(item["deadline"]) > 80):
                raise ValueError("Classification text is too long")
        timestamp = _now()
        with self.conn:
            self.conn.execute(
                """INSERT OR IGNORE INTO classification_runs
                   (run_key, created_at, taxonomy_version, provider_id, model_id, prompt_version)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (run_key, timestamp, taxonomy.version, provenance.provider_id, provenance.model_id, provenance.prompt_version),
            )
            run = self.conn.execute("SELECT * FROM classification_runs WHERE run_key = ?", (run_key,)).fetchone()
            if (run["taxonomy_version"], run["provider_id"], run["model_id"], run["prompt_version"]) != (
                taxonomy.version, provenance.provider_id, provenance.model_id, provenance.prompt_version,
            ):
                raise ValueError("Run key already exists with different provenance")
            run_id = run["id"]
            for item in results:
                # Only explicitly selected, bounded header metadata is persisted.
                metadata = item.get("review_metadata") or {}
                headers = tuple(
                    metadata.get(key)[:limit] if isinstance(metadata.get(key), str) else None
                    for key, limit in (("date", 100), ("sender", 320), ("subject", 500))
                )
                self.conn.execute("""
                    INSERT OR IGNORE INTO classifications (
                        run_id, message_id, classified_at, taxonomy_version, predicted_category,
                        predicted_confidence, predicted_needs_reply, predicted_importance,
                        predicted_deadline, short_reason, subtype_hint, email_date, email_sender, email_subject
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    run_id, item["message_id"], timestamp, taxonomy.version, item["category"],
                    item["confidence"], int(item["needs_reply"]), item["importance"],
                    item["deadline"], item["short_reason"], item.get("subtype_hint"), *headers,
                ))
        return run_key

    def records(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute("""
            SELECT c.*, r.provider_id, r.model_id, r.prompt_version
            FROM classifications AS c JOIN classification_runs AS r ON r.id = c.run_id
            ORDER BY c.id
        """)]

    def pending(self, *, exclude_ids: set[int] | None = None) -> list[dict]:
        excluded = exclude_ids or set()
        return [row for row in self.records() if row["review_status"] == "pending" and row["primary_review_action"] is None and row["id"] not in excluded]

    def review(
        self, classification_id: int, action: str, *, category: str | None = None,
        needs_reply: bool | None = None, importance: str | None = None, deadline=UNSET,
        taxonomy: Taxonomy | None = None,
        subtype_action: str | None = None, corrected_subtype: str | None = None,
    ) -> None:
        row = self.conn.execute("SELECT * FROM classifications WHERE id = ?", (classification_id,)).fetchone()
        if row is None or row["review_status"] != "pending" or row["primary_review_action"] is not None:
            raise ValueError("Classification is missing or already reviewed")
        if action not in {"accepted", "corrected"}:
            raise ValueError("Review action must be accepted or corrected")
        if action == "accepted" and any(value is not None for value in (category, needs_reply, importance)):
            raise ValueError("Accepted review cannot include corrections")
        if action == "accepted" and deadline is not UNSET:
            raise ValueError("Accepted review cannot include corrections")
        if action == "corrected" and all(value is None for value in (category, needs_reply, importance)) and deadline is UNSET:
            raise ValueError("Correction requires a changed field")
        taxonomy = taxonomy or load_taxonomy()
        if category is not None and category not in taxonomy.active_names:
            raise ValueError("Unknown active category")
        if needs_reply is not None and not isinstance(needs_reply, bool):
            raise ValueError("needs_reply must be boolean")
        if importance is not None and importance not in IMPORTANCE_VALUES:
            raise ValueError("Invalid importance")
        if deadline is not UNSET and deadline is not None and (not isinstance(deadline, str) or len(deadline) > 80):
            raise ValueError("Invalid deadline")
        if subtype_action is not None:
            self._validate_subtype_review(row, subtype_action, corrected_subtype, category or row["predicted_category"])
        elif corrected_subtype is not None:
            raise ValueError("Subtype correction requires a subtype decision")
        with self.conn:
            self.conn.execute("""
                UPDATE classifications SET review_status = ?, reviewed_at = ?, corrected_category = ?,
                    corrected_needs_reply = ?, corrected_importance = ?, corrected_deadline = ?,
                    corrected_deadline_set = ?, primary_review_action = ? WHERE id = ?
            """, (
                action, _now(), category, None if needs_reply is None else int(needs_reply),
                importance, None if deadline is UNSET else deadline, int(deadline is not UNSET),
                "accept" if action == "accepted" else "correct", classification_id,
            ))
            if subtype_action is not None:
                self._write_subtype_review(classification_id, subtype_action, corrected_subtype)

    @staticmethod
    def _validate_subtype_review(row, action, corrected_subtype, category) -> None:
        if row["subtype_review_status"] != "pending":
            raise ValueError("Subtype is already reviewed")
        if action not in {"accepted", "rejected", "corrected"}:
            raise ValueError("Invalid subtype decision")
        if action != "corrected" and corrected_subtype is not None:
            raise ValueError("Only subtype correction can include a new hint")
        if action == "accepted":
            if row["subtype_hint"] is None:
                raise ValueError("No subtype hint to accept")
            validate_subtype_hint(row["subtype_hint"], category)
        if action == "rejected" and row["subtype_hint"] is None:
            raise ValueError("No subtype hint to reject")
        if action == "corrected":
            if corrected_subtype is None or corrected_subtype == row["subtype_hint"]:
                raise ValueError("Subtype correction requires a changed hint")
            validate_subtype_hint(corrected_subtype, category)

    def _write_subtype_review(self, classification_id, action, corrected_subtype) -> None:
        self.conn.execute("""
            UPDATE classifications SET subtype_review_status = ?, corrected_subtype_hint = ?,
                subtype_reviewed_at = ? WHERE id = ?
        """, (action, corrected_subtype, _now(), classification_id))

    def review_subtype(self, classification_id: int, action: str, *, corrected_subtype: str | None = None) -> None:
        row = self.conn.execute("SELECT * FROM classifications WHERE id = ?", (classification_id,)).fetchone()
        if row is None:
            raise ValueError("Classification is missing")
        self._validate_subtype_review(row, action, corrected_subtype,
                                      row["corrected_category"] or row["predicted_category"])
        with self.conn:
            self._write_subtype_review(classification_id, action, corrected_subtype)

    def activation_records(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute("SELECT * FROM taxonomy_activations ORDER BY proposal_id")]

    def proposal_supports(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute("SELECT * FROM taxonomy_proposal_support ORDER BY id")]

    def label_suggestion(self, classification_id: int, *, taxonomy: Taxonomy | None = None,
                         policy: SuggestionPolicy = SuggestionPolicy()):
        rows = self.records()
        current = next((row for row in rows if row["id"] == classification_id), None)
        if current is None:
            raise ValueError("Classification is missing")
        return suggest_label(current, rows, self.proposals(), self.proposal_supports(),
                             taxonomy or load_taxonomy(), policy)

    def propose_label(
        self, classification_id: int, concept: str | None = None, display_label: str | None = None,
        *, accept_suggested: bool = False, taxonomy: Taxonomy | None = None,
        policy: SuggestionPolicy = SuggestionPolicy(),
    ) -> int:
        taxonomy = taxonomy or load_taxonomy()
        row = self.conn.execute("SELECT * FROM classifications WHERE id = ?", (classification_id,)).fetchone()
        if row is None or row["review_status"] != "pending" or row["primary_review_action"] is not None:
            raise ValueError("Classification is missing or already reviewed")
        suggestion = None
        if accept_suggested:
            suggestion = self.label_suggestion(classification_id, taxonomy=taxonomy, policy=policy)
            if suggestion is None:
                raise ValueError("No strong label suggestion is available")
            concept, display_label = suggestion.concept_key, suggestion.display_label
        concept = normalize_concept(concept)
        if represented_concept(concept, taxonomy):
            raise ValueError("Concept is already represented by an existing category")
        if display_label is not None:
            display_label = display_label.strip()
            if not display_label or len(display_label) > 80 or not all(c.isprintable() for c in display_label):
                raise ValueError("Display label must be printable and at most 80 characters")
        existing = next((item for item in self.proposals()
                         if item["proposal_type"] == "CREATE" and item.get("concept_key") == concept), None)
        if existing and existing["status"] != "proposed":
            raise ValueError("This concept already has a resolved proposal")
        if not existing:
            for item in self.proposals():
                if item["proposal_type"] == "CREATE" and item.get("concept_key") is None:
                    if concept in {normalize_concept(name) for name in item["affected_categories"]}:
                        raise ValueError("A legacy CREATE proposal already represents this concept")
        now = _now()
        source = "human_proposal_accept" if accept_suggested else "human_manual"
        with self.conn:
            if existing:
                proposal_id = existing["id"]
            else:
                cursor = self.conn.execute("""
                    INSERT INTO taxonomy_proposals (
                        created_at, taxonomy_version, proposal_type, affected_categories,
                        evidence_counts, rationale, expected_benefit, evidence_strength, status,
                        concept_key, display_label, updated_at
                    ) VALUES (?, ?, 'CREATE', ?, '{}', ?, ?, ?, 'proposed', ?, ?, ?)
                """, (now, taxonomy.version, json.dumps([row["predicted_category"]]),
                      "Human reports taxonomy insufficiency; inspect evidence before taxonomy approval.",
                      "Represent a recurring mailbox concept if approved.",
                      "strong" if accept_suggested else "human_proposed", concept, display_label, now))
                proposal_id = cursor.lastrowid
            self.conn.execute("""
                INSERT OR IGNORE INTO taxonomy_proposal_support
                (proposal_id, classification_id, message_id, source, display_label, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (proposal_id, classification_id, row["message_id"], source, display_label, now))
            if suggestion:
                for supporting_id, supporting_source in suggestion.supporting_evidence:
                    self.conn.execute("""
                        INSERT OR IGNORE INTO taxonomy_proposal_support
                        (proposal_id, classification_id, message_id, source, created_at)
                        SELECT ?, id, message_id, ?, ? FROM classifications WHERE id = ?
                    """, (proposal_id, supporting_source, now, supporting_id))
            evidence = dict(suggestion.evidence) if suggestion else {}
            counts = self.conn.execute("""
                SELECT source, COUNT(DISTINCT message_id) AS count FROM taxonomy_proposal_support
                WHERE proposal_id = ? GROUP BY source
            """, (proposal_id,)).fetchall()
            evidence.update({item["source"]: item["count"] for item in counts})
            if suggestion:
                evidence["score_at_approval"] = suggestion.score
            elif existing:
                evidence = {**existing["evidence_counts"], **evidence}
            self.conn.execute("""
                UPDATE taxonomy_proposals SET display_label = COALESCE(display_label, ?),
                    evidence_counts = ?, updated_at = ?, ready_for_approval = MAX(ready_for_approval, ?),
                    evidence_strength = CASE WHEN ? THEN 'strong' ELSE evidence_strength END
                WHERE id = ?
            """, (display_label, json.dumps(evidence), now, int(accept_suggested), int(accept_suggested), proposal_id))
            # This records insufficiency, not a category acceptance or correction.
            self.conn.execute("""
                UPDATE classifications SET primary_review_action = ?, reviewed_at = ? WHERE id = ?
            """, ("accept_proposed_label" if accept_suggested else "new_label", now, classification_id))
        return proposal_id

    def save_proposal(self, proposal, taxonomy_version: str) -> int:
        with self.conn:
            cursor = self.conn.execute("""
                INSERT INTO taxonomy_proposals (
                    created_at, taxonomy_version, proposal_type, affected_categories,
                    evidence_counts, rationale, expected_benefit, evidence_strength, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                _now(), taxonomy_version, proposal.proposal_type,
                json.dumps(proposal.affected_categories), json.dumps(proposal.evidence_counts),
                proposal.rationale, proposal.expected_benefit, proposal.evidence_strength, proposal.status,
            ))
        return cursor.lastrowid

    def proposals(self) -> list[dict]:
        rows = [dict(row) for row in self.conn.execute("SELECT * FROM taxonomy_proposals ORDER BY id")]
        for row in rows:
            row["affected_categories"] = json.loads(row["affected_categories"])
            row["evidence_counts"] = json.loads(row["evidence_counts"])
        return rows


def ground_truth(row: dict) -> dict | None:
    """Only explicit human acceptance or correction can create ground truth."""
    if row["review_status"] == "pending":
        return None
    return {
        "category": row["corrected_category"] or row["predicted_category"],
        "needs_reply": bool(row["predicted_needs_reply"] if row["corrected_needs_reply"] is None else row["corrected_needs_reply"]),
        "importance": row["corrected_importance"] or row["predicted_importance"],
        "deadline": row["corrected_deadline"] if row["corrected_deadline_set"] else row["predicted_deadline"],
    }
