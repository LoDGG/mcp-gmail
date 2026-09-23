"""Conservative local label proposals: no live APIs, mutations, or waits."""

from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from gmail_agent.history import HistoryDB, ground_truth
from gmail_agent.label_suggestions import SuggestionPolicy
from gmail_agent.taxonomy import load_taxonomy
from gmail_agent.taxonomy_analysis import Proposal, calculate_stats
from gmail_agent import taxonomy_stats
from test_history import prediction
from test_review import run_review


def seed(db, count, *, concept="job_offer", senders=3, runs=2, category="NOTIFICATION"):
    for run in range(runs):
        messages = [
            prediction(f"m-{index}", category, subtype_hint=concept,
                       review_metadata={"sender": f"Sender <sender{index % senders}@example.test>"} if senders else {})
            for index in range(count) if index % runs == run
        ]
        if messages:
            db.save_run(messages)
    return db.records()


@pytest.mark.parametrize("count", [1, 2, 3, 7])
def test_raw_concepts_below_threshold_are_not_suggested(tmp_path, count):
    with HistoryDB(tmp_path / "db.sqlite") as db:
        rows = seed(db, count)
        assert db.label_suggestion(rows[0]["id"]) is None
        assert db.proposals() == []


def test_normal_mail_has_no_suggestion_or_p_action(tmp_path, monkeypatch, capsys):
    path = tmp_path / "db.sqlite"
    with HistoryDB(path) as db:
        seed(db, 10, concept="subscription_invoice", category="FINANCE")
    run_review(monkeypatch, path, ["p", "q"], "--limit", "1")
    output = capsys.readouterr().out
    assert "Suggested new label" not in output
    assert "[p]" not in output
    assert "No strong label suggestion" in output
    assert "gap" not in output.lower()
    with HistoryDB(path) as db:
        assert db.proposals() == []
        assert all(row["primary_review_action"] is None for row in db.records())


def test_model_recurrence_above_threshold_has_inspectable_score(tmp_path):
    with HistoryDB(tmp_path / "db.sqlite") as db:
        rows = seed(db, 8)
        suggestion = db.label_suggestion(rows[0]["id"])
        assert suggestion.concept_key == "job_offer"
        assert suggestion.score == 8 + 3 + 2
        assert suggestion.evidence == {
            "distinct_messages": 8, "distinct_senders": 3, "human_approved_subtypes": 0,
            "manual_new_label_messages": 0, "proposal_approval_messages": 0,
            "human_confirmed_messages": 0, "category_correction_messages": 0,
            "distinct_runs": 2, "raw_subtype_messages": 8,
        }
        assert db.proposals() == []  # Merely displaying a suggestion is read-only.


@pytest.mark.parametrize("senders,runs", [(1, 2), (2, 2), (0, 2), (3, 1)])
def test_model_evidence_requires_sender_and_run_diversity(tmp_path, senders, runs):
    with HistoryDB(tmp_path / "db.sqlite") as db:
        rows = seed(db, 12, senders=senders, runs=runs)
        assert db.label_suggestion(rows[0]["id"]) is None


def test_sender_normalization_does_not_count_display_names(tmp_path):
    with HistoryDB(tmp_path / "db.sqlite") as db:
        rows = seed(db, 8)
        with db.conn:
            for index, row in enumerate(rows):
                db.conn.execute("UPDATE classifications SET email_sender = ? WHERE id = ?",
                                (f"Different Name {index} <SAME@example.test>", row["id"]))
        assert db.label_suggestion(rows[0]["id"]) is None


def test_human_approved_subtypes_reach_threshold_earlier(tmp_path):
    with HistoryDB(tmp_path / "db.sqlite") as db:
        rows = seed(db, 3, runs=1)
        for row in rows[:2]:
            db.review_subtype(row["id"], "accepted")
        suggestion = db.label_suggestion(rows[-1]["id"])
        assert suggestion is not None
        assert suggestion.evidence["human_confirmed_messages"] == 2
        assert suggestion.evidence["human_approved_subtypes"] == 2
        assert suggestion.evidence["distinct_messages"] == 3
        assert suggestion.score == 3 + 5 * 2 + 3 + 1


def test_policy_is_configurable_and_single_message_does_not_supply_two_confirmations(tmp_path):
    with HistoryDB(tmp_path / "db.sqlite") as db:
        rows = seed(db, 3)
        db.review_subtype(rows[0]["id"], "accepted")
        db.propose_label(rows[0]["id"], "job_offer")
        assert db.label_suggestion(rows[-1]["id"]) is None
        db.review_subtype(rows[1]["id"], "accepted")
        assert db.label_suggestion(rows[-1]["id"]) is not None
        assert db.label_suggestion(rows[-1]["id"], policy=replace(SuggestionPolicy(), human_min_messages=4)) is None
    with pytest.raises(ValueError):
        SuggestionPolicy(model_min_messages=0)


def test_duplicate_messages_cannot_inflate_support_or_manual_signals(tmp_path):
    with HistoryDB(tmp_path / "db.sqlite") as db:
        rows = seed(db, 2)
        for row in rows:
            db.propose_label(row["id"], "job_offer")
        for _ in range(8):
            db.save_run([prediction("m-0", "NOTIFICATION", subtype_hint="job_offer",
                                    review_metadata={"sender": "new-name@example.test"})])
            db.propose_label(db.records()[-1]["id"], "job_offer")
        db.save_run([prediction("m-1", "NOTIFICATION", subtype_hint="job_offer")])
        assert db.label_suggestion(db.records()[-1]["id"]) is None
        assert len(db.proposals()) == 1
        assert len(db.proposal_supports()) == 2


@pytest.mark.parametrize("concept", [
    "notification", "professional_message", "billing", "security_alert",
    "indeed_email", "september_invoice_from_company_x", "invoice_2026",
])
def test_redundant_or_specific_concepts_are_not_suggested(tmp_path, concept):
    with HistoryDB(tmp_path / "db.sqlite") as db:
        rows = seed(db, 8, concept=concept, category="UNCERTAIN")
        assert db.label_suggestion(rows[0]["id"]) is None


def test_consistent_existing_human_category_fit_suppresses_create(tmp_path):
    with HistoryDB(tmp_path / "db.sqlite") as db:
        rows = seed(db, 8)
        for row in rows[:2]:
            db.review(row["id"], "corrected", category="PROFESSIONAL",
                      subtype_action="accepted")
        assert db.label_suggestion(rows[-1]["id"]) is None


def test_existing_category_acceptances_are_not_taxonomy_insufficiency(tmp_path):
    with HistoryDB(tmp_path / "db.sqlite") as db:
        rows = seed(db, 8)
        for row in rows[:2]:
            db.review(row["id"], "accepted", subtype_action="accepted")
        assert db.label_suggestion(rows[-1]["id"]) is None


def test_conflicting_category_corrections_add_evidence(tmp_path):
    with HistoryDB(tmp_path / "db.sqlite") as db:
        rows = seed(db, 3, category="NEWSLETTER")
        for row, category in zip(rows[:2], ["PROFESSIONAL", "NOTIFICATION"]):
            db.review(row["id"], "corrected", category=category, subtype_action="accepted")
        suggestion = db.label_suggestion(rows[-1]["id"])
        assert suggestion is not None
        assert suggestion.evidence["category_correction_messages"] == 2
        assert suggestion.score >= 15


def test_manual_new_label_immediate_and_repeated_signals_reuse_candidate(tmp_path, monkeypatch, capsys):
    path = tmp_path / "db.sqlite"
    with HistoryDB(path) as db:
        seed(db, 2)
        before = db.records()
    run_review(monkeypatch, path, ['N job_offer "Offre"', 'n job-offers "Offre"'])
    with HistoryDB(path) as db:
        proposals = db.proposals()
        assert len(proposals) == 1
        candidate = proposals[0]
        assert candidate["proposal_type"] == "CREATE"
        assert candidate["concept_key"] == "job_offer"
        assert candidate["display_label"] == "Offre"
        assert candidate["ready_for_approval"] == 0
        assert candidate["status"] == "proposed"
        assert candidate["evidence_counts"]["human_manual"] == 2
        supports = db.proposal_supports()
        assert {item["message_id"] for item in supports} == {"m-0", "m-1"}
        assert all(item["source"] == "human_manual" and item["created_at"] for item in supports)
        rows = db.records()
        for old, new in zip(before, rows):
            assert new["primary_review_action"] == "new_label"
            assert ground_truth(new) is None
            for key in ("predicted_category", "subtype_hint", "predicted_confidence", "model_id", "prompt_version"):
                assert old[key] == new[key]
        assert db.pending() == []
        stats = calculate_stats(rows)
        assert stats["reviewed"] == 0
        assert stats["category_correction_rate"] == 0
        assert stats["taxonomy_insufficiency_decisions"]["new_label"] == 2
        assert stats["performance_by_provenance"][0]["category_correction_rate"] is None
    assert "[p]" not in capsys.readouterr().out
    run_review(monkeypatch, path, [], "--only-unreviewed")


def test_manual_pending_candidate_is_reused_for_strong_suggestion(tmp_path, monkeypatch, capsys):
    path = tmp_path / "db.sqlite"
    with HistoryDB(path) as db:
        seed(db, 3)
    run_review(monkeypatch, path, ['n job_offer "Offre"', "n job_offer", "p"])
    output = capsys.readouterr().out
    assert output.count("[p]") == 1
    assert "Reuses pending CREATE proposal" in output
    assert "display: Offre" in output
    with HistoryDB(path) as db:
        assert len(db.proposals()) == 1
        proposal = db.proposals()[0]
        assert proposal["ready_for_approval"] == 1
        assert proposal["status"] == "proposed"  # Approval/application remains a later explicit step.
        assert proposal["evidence_strength"] == "strong"
        approvals = [item for item in db.proposal_supports() if item["source"] == "human_proposal_accept"]
        assert len(approvals) == 1
        stats = calculate_stats(db.records())
        assert stats["reviewed"] == 0
        assert stats["taxonomy_insufficiency_decisions"] == {"new_label": 2, "accept_proposed_label": 1}


def test_p_snapshots_evidence_and_does_not_count_as_category_error(tmp_path, monkeypatch, capsys):
    path = tmp_path / "db.sqlite"
    with HistoryDB(path) as db:
        seed(db, 8)
    run_review(monkeypatch, path, ["p"], "--limit", "1")
    output = capsys.readouterr().out
    assert "8 distinct messages" in output and "3 senders" in output
    assert "[p] accept proposed label" in output
    with HistoryDB(path) as db:
        first, *others = db.records()
        assert first["primary_review_action"] == "accept_proposed_label"
        assert first["corrected_category"] is None
        assert ground_truth(first) is None
        assert db.label_suggestion(others[0]["id"]) is None  # Ready candidates stop being pushed.
        proposal = db.proposals()[0]
        assert proposal["evidence_counts"]["score_at_approval"] == 13
        assert proposal["evidence_counts"]["model_subtype"] == 8
        supports = db.proposal_supports()
        assert len({item["message_id"] for item in supports}) == 8
        assert len([item for item in supports if item["source"] == "human_proposal_accept"]) == 1
        assert calculate_stats(db.records())["category_correction_rate"] == 0
    monkeypatch.setattr("sys.argv", ["stats", "--db", str(path)])
    taxonomy_stats.main()
    stored = json.loads(capsys.readouterr().out)["stored_create_proposals"]
    assert stored[0]["ready_for_approval"] == 1
    assert len(stored[0]["supporting_evidence"]) == 9


def test_mutually_exclusive_actions_cannot_overwrite_decisions(tmp_path):
    with HistoryDB(tmp_path / "db.sqlite") as db:
        rows = seed(db, 3)
        db.propose_label(rows[0]["id"], "job_offer")
        with pytest.raises(ValueError, match="already reviewed"):
            db.review(rows[0]["id"], "accepted")
        with pytest.raises(ValueError, match="already reviewed"):
            db.propose_label(rows[0]["id"], "different_concept")
        db.review(rows[1]["id"], "corrected", category="PROFESSIONAL")
        with pytest.raises(ValueError, match="already reviewed"):
            db.propose_label(rows[1]["id"], "job_offer")
        with pytest.raises(ValueError, match="No strong"):
            db.propose_label(rows[2]["id"], accept_suggested=True)
        assert db.records()[2]["primary_review_action"] is None


def test_legacy_named_create_and_rejected_proposals_suppress_duplicates(tmp_path):
    with HistoryDB(tmp_path / "db.sqlite") as db:
        rows = seed(db, 8)
        db.save_proposal(Proposal("CREATE", ["job_offer"], {}, "Legacy", "Later", "weak"), load_taxonomy().version)
        assert db.label_suggestion(rows[0]["id"]) is None
        with pytest.raises(ValueError, match="legacy CREATE"):
            db.propose_label(rows[0]["id"], "job_offer")
    with HistoryDB(tmp_path / "rejected.sqlite") as db:
        rows = seed(db, 8)
        proposal_id = db.propose_label(rows[0]["id"], "job_offer")
        with db.conn:
            db.conn.execute("UPDATE taxonomy_proposals SET status = 'rejected' WHERE id = ?", (proposal_id,))
        assert db.label_suggestion(rows[1]["id"]) is None
        with pytest.raises(ValueError, match="resolved proposal"):
            db.propose_label(rows[1]["id"], "job_offer")


def test_existing_database_migration_preserves_reviews_predictions_and_proposals(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with HistoryDB(path) as db:
        db.save_run([prediction(subtype_hint="subscription_invoice")])
        db.review(db.records()[0]["id"], "corrected", category="ADMIN",
                  subtype_action="corrected", corrected_subtype="account_notice")
        db.save_proposal(Proposal("CREATE", ["UNCERTAIN"], {}, "Legacy", "Later", "weak"), load_taxonomy().version)
        before = db.records()[0]
        proposal_before = db.proposals()[0]
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE taxonomy_proposal_support")
        conn.execute("DROP INDEX create_proposal_concept")
        conn.execute("ALTER TABLE classifications DROP COLUMN primary_review_action")
        for column in ("concept_key", "display_label", "updated_at", "ready_for_approval"):
            conn.execute(f"ALTER TABLE taxonomy_proposals DROP COLUMN {column}")
    for _ in range(2):
        with HistoryDB(path) as db:
            row = db.records()[0]
            # Old reviews are not rewritten to fabricate a new primary action.
            assert row == {**before, "primary_review_action": None}
            assert db.proposals()[0] == proposal_before
            assert ground_truth(row)["category"] == "ADMIN"
            assert row["corrected_subtype_hint"] == "account_notice"
            assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_review_has_no_gmail_or_model_calls_or_taxonomy_changes(tmp_path, monkeypatch):
    from gmail_agent.gemini_classifier import GeminiBatchClassifier
    from real_gmail_mcp.backend import RealGmailBackend

    def forbidden(*args, **kwargs):
        raise AssertionError("Review must not access Gmail or Gemini")

    monkeypatch.setattr(GeminiBatchClassifier, "__init__", forbidden)
    monkeypatch.setattr(RealGmailBackend, "__init__", forbidden)
    config_paths = [Path("config/taxonomy.json"), Path("config/gmail_labels.json")]
    before = [path.read_bytes() for path in config_paths]
    path = tmp_path / "db.sqlite"
    with HistoryDB(path) as db:
        seed(db, 8)
    run_review(monkeypatch, path, ["p", "n shipping_update"], "--limit", "2")
    assert [path.read_bytes() for path in config_paths] == before


def test_actual_taxonomy_category_suppresses_concept(tmp_path):
    taxonomy = load_taxonomy()
    extended = replace(taxonomy, categories=taxonomy.categories + ({
        "name": "JOB_OFFER", "definition": "Job offers", "positive_guidance": "Offers",
        "negative_guidance": "Other", "active": True,
    },))
    with HistoryDB(tmp_path / "db.sqlite") as db:
        rows = seed(db, 8)
        assert db.label_suggestion(rows[0]["id"], taxonomy=extended) is None
        with pytest.raises(ValueError, match="existing category"):
            db.propose_label(rows[0]["id"], "job_offer", taxonomy=extended)


def test_rejected_subtypes_do_not_supply_positive_evidence(tmp_path):
    with HistoryDB(tmp_path / "db.sqlite") as db:
        rows = seed(db, 8)
        db.review_subtype(rows[0]["id"], "rejected")
        assert db.label_suggestion(rows[0]["id"]) is None
        assert db.label_suggestion(rows[-1]["id"]) is None
