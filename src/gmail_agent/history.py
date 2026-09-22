"""Local classification and human-review history. No email content is stored."""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

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

    def save_run(self, results: list[dict], taxonomy: Taxonomy | None = None, *, run_key: str | None = None) -> str:
        taxonomy = taxonomy or load_taxonomy()
        run_key = run_key or str(uuid4())
        if len({item["message_id"] for item in results}) != len(results):
            raise ValueError("Duplicate message ID in classification run")
        for item in results:
            if item["category"] not in taxonomy.active_names or item["importance"] not in IMPORTANCE_VALUES:
                raise ValueError("Classification does not match active taxonomy")
            if not 0 <= item["confidence"] <= 1 or not isinstance(item["needs_reply"], bool):
                raise ValueError("Invalid classification attributes")
            if len(item["short_reason"]) > 120 or (item["deadline"] is not None and len(item["deadline"]) > 80):
                raise ValueError("Classification text is too long")
        timestamp = _now()
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO classification_runs(run_key, created_at, taxonomy_version) VALUES (?, ?, ?)",
                (run_key, timestamp, taxonomy.version),
            )
            run_id = self.conn.execute("SELECT id FROM classification_runs WHERE run_key = ?", (run_key,)).fetchone()["id"]
            for item in results:
                # Explicit columns prevent subject, snippet, body, or other API fields from being persisted.
                self.conn.execute("""
                    INSERT OR IGNORE INTO classifications (
                        run_id, message_id, classified_at, taxonomy_version, predicted_category,
                        predicted_confidence, predicted_needs_reply, predicted_importance,
                        predicted_deadline, short_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    run_id, item["message_id"], timestamp, taxonomy.version, item["category"],
                    item["confidence"], int(item["needs_reply"]), item["importance"],
                    item["deadline"], item["short_reason"],
                ))
        return run_key

    def records(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute("SELECT * FROM classifications ORDER BY id")]

    def pending(self, *, exclude_ids: set[int] | None = None) -> list[dict]:
        excluded = exclude_ids or set()
        return [row for row in self.records() if row["review_status"] == "pending" and row["id"] not in excluded]

    def review(
        self, classification_id: int, action: str, *, category: str | None = None,
        needs_reply: bool | None = None, importance: str | None = None, deadline=UNSET,
        taxonomy: Taxonomy | None = None,
    ) -> None:
        row = self.conn.execute("SELECT * FROM classifications WHERE id = ?", (classification_id,)).fetchone()
        if row is None or row["review_status"] != "pending":
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
        with self.conn:
            self.conn.execute("""
                UPDATE classifications SET review_status = ?, reviewed_at = ?, corrected_category = ?,
                    corrected_needs_reply = ?, corrected_importance = ?, corrected_deadline = ?,
                    corrected_deadline_set = ? WHERE id = ?
            """, (
                action, _now(), category, None if needs_reply is None else int(needs_reply),
                importance, None if deadline is UNSET else deadline, int(deadline is not UNSET), classification_id,
            ))

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
