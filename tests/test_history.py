"""Local persistence and taxonomy analysis tests; no network or models."""

import dataclasses
import sqlite3

import pytest

from gmail_agent.history import HistoryDB, ground_truth
from gmail_agent.taxonomy import load_taxonomy
from gmail_agent.taxonomy_analysis import Proposal, calculate_stats, generate_candidates


def prediction(message_id="opaque-1", category="FINANCE", **changes):
    item = {
        "message_id": message_id, "category": category, "confidence": 0.8,
        "needs_reply": False, "importance": "normal", "deadline": None,
        "short_reason": "Looks like finance", "body": "PRIVATE BODY MUST NOT BE STORED",
        "subject": "PRIVATE SUBJECT MUST NOT BE STORED",
        "snippet": "PRIVATE SNIPPET MUST NOT BE STORED",
    }
    item.update(changes)
    return item


def test_db_initialization_history_and_no_raw_email_content(tmp_path):
    path = tmp_path / "gmail_agent.db"
    taxonomy = load_taxonomy()
    with HistoryDB(path) as db:
        run_key = db.save_run([prediction()], taxonomy, run_key="run-1")
        db.save_run([prediction()], taxonomy, run_key="run-1")
        rows = db.records()
        assert run_key == "run-1"
        assert len(rows) == 1
        assert rows[0]["message_id"] == "opaque-1"
        assert rows[0]["taxonomy_version"] == taxonomy.version
        assert rows[0]["review_status"] == "pending"
        assert ground_truth(rows[0]) is None
        columns = {row[1] for row in db.conn.execute("PRAGMA table_info(classifications)")}
        assert not {"body", "subject", "snippet"} & columns
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM classification_runs").fetchone()[0] == 1
    data = path.read_bytes()
    assert b"PRIVATE BODY MUST NOT BE STORED" not in data
    assert b"PRIVATE SUBJECT MUST NOT BE STORED" not in data
    assert b"PRIVATE SNIPPET MUST NOT BE STORED" not in data


def test_acceptance_and_correction_create_ground_truth(tmp_path):
    with HistoryDB(tmp_path / "db.sqlite") as db:
        db.save_run([prediction("accepted"), prediction("corrected", deadline="Friday")])
        first, second = db.records()
        assert ground_truth(first) is None
        db.review(first["id"], "accepted")
        db.review(second["id"], "corrected", category="ADMIN", needs_reply=True,
                  importance="high", deadline=None)
        accepted, corrected = db.records()
        assert ground_truth(accepted) == {
            "category": "FINANCE", "needs_reply": False, "importance": "normal", "deadline": None,
        }
        assert ground_truth(corrected) == {
            "category": "ADMIN", "needs_reply": True, "importance": "high", "deadline": None,
        }
        assert corrected["predicted_category"] == "FINANCE"
        assert corrected["predicted_deadline"] == "Friday"
        assert corrected["corrected_deadline_set"] == 1
        with pytest.raises(ValueError, match="already reviewed"):
            db.review(first["id"], "accepted")


def test_statistics_and_confusion_matrix_use_human_reviews_only(tmp_path):
    with HistoryDB(tmp_path / "db.sqlite") as db:
        db.save_run([
            prediction("m-1", "FINANCE", confidence=0.4),
            prediction("m-2", "NEWSLETTER", confidence=0.7),
            prediction("m-3", "UNCERTAIN", confidence=0.9),
        ])
        rows = db.records()
        db.review(rows[0]["id"], "corrected", category="ADMIN", needs_reply=True, importance="high")
        db.review(rows[1]["id"], "accepted")
        stats = calculate_stats(db.records())
    assert stats["classified_emails"] == 3
    assert stats["reviewed"] == 2
    assert stats["category_distribution"] == {"FINANCE": 1, "NEWSLETTER": 1, "UNCERTAIN": 1}
    assert stats["uncertain_rate"] == pytest.approx(1 / 3)
    assert stats["confidence_distribution"] == {"below_0_5": 1, "0_5_to_0_8": 1, "0_8_and_above": 1}
    assert stats["correction_rate"] == 0.5
    assert stats["confusion_matrix"] == {"FINANCE": {"ADMIN": 1}}
    assert stats["needs_reply_disagreement_rate"] == 0.5
    assert stats["importance_disagreement_rate"] == 0.5
    assert stats["category_frequency_among_corrections"] == {"ADMIN": 1}


def reviewed_row(predicted, actual, *, reply=False, importance="normal", message_id="x"):
    return {
        "message_id": message_id, "predicted_category": predicted,
        "predicted_confidence": 0.5, "predicted_needs_reply": int(reply),
        "predicted_importance": importance, "predicted_deadline": None,
        "review_status": "accepted" if predicted == actual else "corrected",
        "corrected_category": None if predicted == actual else actual,
        "corrected_needs_reply": None, "corrected_importance": None,
        "corrected_deadline": None, "corrected_deadline_set": 0,
    }


def test_candidate_generation_and_hard_cap():
    taxonomy = load_taxonomy()
    uncertain = [reviewed_row("UNCERTAIN", "UNCERTAIN", message_id=f"u-{i}") for i in range(3)]
    rows = uncertain + [reviewed_row("FINANCE", "ADMIN", message_id=f"f-{i}") for i in range(3)]
    candidates = generate_candidates(rows, taxonomy)
    assert {item.proposal_type for item in candidates} >= {"CREATE", "REFINE_DEFINITION"}
    create = next(item for item in candidates if item.proposal_type == "CREATE")
    assert create.status == "proposed"
    assert create.evidence_counts == {"reviewed_unresolved_uncertain": 3}
    capped = dataclasses.replace(taxonomy, categories=taxonomy.categories + ({
        "name": "OTHER", "definition": "Other", "positive_guidance": "Other",
        "negative_guidance": "Other", "active": True,
    },))
    capped_create = next(item for item in generate_candidates(rows, capped) if item.proposal_type == "CREATE")
    assert "merge or deprecation is required" in capped_create.rationale


def test_merge_split_deprecate_signals_and_proposal_persistence(tmp_path):
    taxonomy = load_taxonomy()
    rows = []
    rows += [reviewed_row("FINANCE", "ADMIN", message_id=f"a-{i}") for i in range(2)]
    rows += [reviewed_row("ADMIN", "FINANCE", message_id=f"b-{i}") for i in range(2)]
    rows += [reviewed_row("ADMIN", "ADMIN", message_id=f"c-{i}", reply=i < 2) for i in range(6)]
    rows += [reviewed_row("FINANCE", "FINANCE", message_id=f"d-{i}") for i in range(3)]
    rows += [reviewed_row("PERSONAL", "PERSONAL", message_id=f"e-{i}") for i in range(7)]
    candidates = generate_candidates(rows, taxonomy)
    kinds = {item.proposal_type for item in candidates}
    assert {"MERGE", "SPLIT", "DEPRECATE"} <= kinds
    with HistoryDB(tmp_path / "db.sqlite") as db:
        proposal_id = db.save_proposal(candidates[0], taxonomy.version)
        stored = db.proposals()[0]
    assert stored["id"] == proposal_id
    assert stored["proposal_type"] == candidates[0].proposal_type
    assert stored["affected_categories"] == candidates[0].affected_categories
    assert stored["evidence_counts"] == candidates[0].evidence_counts
    assert stored["status"] == "proposed"
    with pytest.raises(ValueError):
        Proposal("APPLY", [], {}, "", "", "weak")
