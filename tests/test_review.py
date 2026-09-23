"""Offline human review, additive migrations, and feedback statistics."""

import sqlite3

import pytest

from gmail_agent import review_cli
from gmail_agent.history import HistoryDB, ground_truth
from gmail_agent.taxonomy_analysis import calculate_stats
from test_history import prediction


def run_review(monkeypatch, path, commands, *options):
    answers = iter(commands)
    monkeypatch.setattr("sys.argv", ["review", "--db", str(path), *options])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    review_cli.main()


def save(path, *items):
    with HistoryDB(path) as db:
        db.save_run(list(items))


def records(path):
    with HistoryDB(path) as db:
        return db.records()


@pytest.mark.parametrize("command", ["a", "accept"])
def test_accept_and_compact_headers(monkeypatch, capsys, tmp_path, command):
    path = tmp_path / "db.sqlite"
    save(path, prediction(subtype_hint="subscription_invoice", deadline="Friday", review_metadata={
        "date": "2026-09-23", "sender": "sender@example.test", "subject": "Invoice",
        "body": "NEVER DISPLAY OR SAVE THIS BODY", "snippet": "NO SNIPPET",
    }))
    run_review(monkeypatch, path, [command], "--limit", "1")
    row = records(path)[0]
    assert row["review_status"] == "accepted"
    assert row["subtype_review_status"] == "pending"
    output = capsys.readouterr().out
    for value in ["opaque-1", "2026-09-23", "sender@example.test", "Invoice", "FINANCE",
                  "0.80", "subscription_invoice", "reply=False", "importance=normal", "Friday"]:
        assert value in output
    assert "NEVER DISPLAY" not in output
    assert b"NEVER DISPLAY" not in path.read_bytes()
    assert b"NO SNIPPET" not in path.read_bytes()


def test_invalid_category_then_correction_preserves_prediction(monkeypatch, capsys, tmp_path):
    path = tmp_path / "db.sqlite"
    save(path, prediction(subtype_hint="subscription_invoice"))
    before = records(path)[0]
    run_review(monkeypatch, path, ["c invented", "correct", "admin"], "--limit", "1")
    row = records(path)[0]
    assert row["corrected_category"] == "ADMIN"
    assert row["review_status"] == "corrected"
    assert ground_truth(row)["category"] == "ADMIN"
    for field in ["predicted_category", "predicted_confidence", "subtype_hint",
                  "predicted_needs_reply", "predicted_importance", "predicted_deadline"]:
        assert row[field] == before[field]
    output = capsys.readouterr().out
    assert "Unknown category" in output
    assert "Categories:" in output


def test_skip_quit_and_limit_leave_pending(monkeypatch, tmp_path):
    path = tmp_path / "db.sqlite"
    save(path, prediction("one"), prediction("two"), prediction("three"))
    run_review(monkeypatch, path, ["skip", "a"], "--limit", "2", "--only-unreviewed")
    assert [row["review_status"] for row in records(path)] == ["pending", "accepted", "pending"]
    run_review(monkeypatch, path, ["quit"])
    assert [row["review_status"] for row in records(path)] == ["pending", "accepted", "pending"]


@pytest.mark.parametrize("command,status,hint", [
    ("a sa", "accepted", None),
    ("a sr", "rejected", None),
    ("c ADMIN sc account_notice", "corrected", "account_notice"),
])
def test_subtype_decisions_and_immutable_original(monkeypatch, tmp_path, command, status, hint):
    path = tmp_path / "db.sqlite"
    save(path, prediction(subtype_hint="subscription_invoice"))
    run_review(monkeypatch, path, [command])
    row = records(path)[0]
    assert row["subtype_review_status"] == status
    assert row["corrected_subtype_hint"] == hint
    assert row["subtype_reviewed_at"] is not None
    assert row["subtype_hint"] == "subscription_invoice"
    assert row["predicted_category"] == "FINANCE"
    assert "subtype_hint" not in ground_truth(row)


def test_subtype_only_does_not_create_category_ground_truth(monkeypatch, tmp_path):
    path = tmp_path / "db.sqlite"
    save(path, prediction(subtype_hint="subscription_invoice"))
    run_review(monkeypatch, path, ["sa"])
    row = records(path)[0]
    assert row["subtype_review_status"] == "accepted"
    assert ground_truth(row) is None
    assert calculate_stats([row], subtype_min_messages=1)["human_approved_subtype_candidates"] == []


def test_optional_subtype_can_be_reviewed_later(monkeypatch, tmp_path):
    path = tmp_path / "db.sqlite"
    save(path, prediction(subtype_hint="subscription_invoice"))
    run_review(monkeypatch, path, ["a"])
    run_review(monkeypatch, path, ["sr"], "--only-unreviewed")
    row = records(path)[0]
    assert row["review_status"] == "accepted"
    assert row["subtype_review_status"] == "rejected"
    run_review(monkeypatch, path, [], "--only-unreviewed")


@pytest.mark.parametrize("hint", ["Bad_Hint", "job-alert", "a" * 41, "admin", "subscription_invoice"])
def test_invalid_subtype_rejects_entire_combined_decision(tmp_path, hint):
    path = tmp_path / "db.sqlite"
    save(path, prediction(subtype_hint="subscription_invoice"))
    with HistoryDB(path) as db:
        before = db.records()[0]
        with pytest.raises(ValueError):
            db.review(before["id"], "corrected", category="ADMIN",
                      subtype_action="corrected", corrected_subtype=hint)
        assert db.records()[0] == before


def test_invalid_cli_subtype_then_retry(monkeypatch, tmp_path, capsys):
    path = tmp_path / "db.sqlite"
    save(path, prediction(subtype_hint="subscription_invoice"))
    run_review(monkeypatch, path, ["a sc Bad_Hint", "a sc invoice_notice"])
    assert records(path)[0]["corrected_subtype_hint"] == "invoice_notice"
    assert "Invalid subtype_hint" in capsys.readouterr().out


def test_existing_decisions_cannot_be_overwritten(tmp_path):
    path = tmp_path / "db.sqlite"
    save(path, prediction(subtype_hint="subscription_invoice"))
    with HistoryDB(path) as db:
        row = db.records()[0]
        db.review(row["id"], "accepted", subtype_action="accepted")
        before = db.records()[0]
        with pytest.raises(ValueError, match="already reviewed"):
            db.review_subtype(row["id"], "rejected")
        with pytest.raises(ValueError, match="already reviewed"):
            db.review(row["id"], "corrected", category="ADMIN")
        assert db.records()[0] == before


def test_review_columns_migrate_existing_database_idempotently(tmp_path):
    path = tmp_path / "legacy.sqlite"
    save(path, prediction(subtype_hint="subscription_invoice"))
    with HistoryDB(path) as db:
        db.review(db.records()[0]["id"], "corrected", category="ADMIN")
        original = db.records()[0]
    added = ["email_date", "email_sender", "email_subject", "subtype_review_status",
             "corrected_subtype_hint", "subtype_reviewed_at"]
    with sqlite3.connect(path) as conn:
        for name in added:
            conn.execute(f"ALTER TABLE classifications DROP COLUMN {name}")
    for _ in range(2):
        with HistoryDB(path) as db:
            assert db.records()[0] == original
            assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    with HistoryDB(path) as db:
        db.review_subtype(original["id"], "corrected", corrected_subtype="account_notice")
        assert db.records()[0]["corrected_category"] == "ADMIN"


def test_stats_separate_category_and_subtype_feedback_and_deduplicate(tmp_path):
    path = tmp_path / "db.sqlite"
    save(path, *(prediction(str(i), subtype_hint="subscription_invoice") for i in range(6)))
    with HistoryDB(path) as db:
        rows = db.records()
        db.review(rows[0]["id"], "accepted", subtype_action="accepted")
        db.review(rows[1]["id"], "corrected", category="ADMIN",
                  subtype_action="corrected", corrected_subtype="account_notice")
        db.review(rows[2]["id"], "accepted", subtype_action="rejected")
        # Non-category attribute corrections count as category agreement.
        db.review(rows[3]["id"], "corrected", importance="high")
        db.review_subtype(rows[4]["id"], "accepted")
        # A repeated prediction must not inflate recurring human evidence.
        db.save_run([prediction("0", subtype_hint="subscription_invoice")])
        db.review(db.records()[-1]["id"], "accepted", subtype_action="accepted")
        stats = calculate_stats(db.records(), subtype_min_messages=1)
    assert stats["reviewed"] == 5
    assert stats["accepted_category_rate"] == 0.8
    assert stats["category_correction_rate"] == 0.2
    assert stats["subtype_review_counts"] == {"accepted": 3, "rejected": 1, "corrected": 1}
    assert stats["subtype_reviewed"] == 5
    assert stats["model_subtype_disagreement_count"] == 2
    assert stats["model_subtype_disagreement_rate"] == 0.4
    assert stats["human_approved_subtype_candidates"] == [
        {"category": "ADMIN", "subtype_hint": "account_notice", "distinct_messages": 1},
        {"category": "FINANCE", "subtype_hint": "subscription_invoice", "distinct_messages": 1},
    ]
    assert stats["model_subtype_disagreements"] == [
        {"model_subtype": "subscription_invoice", "human_subtype": None, "decision": "rejected", "count": 1},
        {"model_subtype": "subscription_invoice", "human_subtype": "account_notice", "decision": "corrected", "count": 1},
    ]
    assert calculate_stats(records(path), subtype_min_messages=2)["human_approved_subtype_candidates"] == []


def test_empty_stats():
    stats = calculate_stats([])
    assert stats["accepted_category_rate"] == stats["category_correction_rate"] == 0
    assert stats["model_subtype_disagreement_rate"] == 0
    assert stats["subtype_review_counts"] == {"accepted": 0, "rejected": 0, "corrected": 0}


def test_null_hint_can_be_corrected_but_not_accepted_or_rejected(tmp_path):
    path = tmp_path / "db.sqlite"
    save(path, prediction())
    with HistoryDB(path) as db:
        row = db.records()[0]
        for action in ("accepted", "rejected"):
            with pytest.raises(ValueError, match="No subtype hint"):
                db.review_subtype(row["id"], action)
        db.review_subtype(row["id"], "corrected", corrected_subtype="subscription_invoice")
        assert db.records()[0]["subtype_hint"] is None
        assert db.records()[0]["corrected_subtype_hint"] == "subscription_invoice"
        assert ground_truth(db.records()[0]) is None


def test_later_subtype_rejection_removes_previous_human_candidate(tmp_path):
    path = tmp_path / "db.sqlite"
    save(path, prediction(subtype_hint="subscription_invoice"))
    with HistoryDB(path) as db:
        db.review(db.records()[0]["id"], "accepted", subtype_action="accepted")
        db.save_run([prediction(subtype_hint="subscription_invoice")])
        db.review(db.records()[-1]["id"], "accepted", subtype_action="rejected")
        assert calculate_stats(db.records(), subtype_min_messages=1)["human_approved_subtype_candidates"] == []


def test_headers_are_bounded_and_untrusted_terminal_controls_are_not_displayed(monkeypatch, tmp_path, capsys):
    path = tmp_path / "db.sqlite"
    save(path, prediction(review_metadata={
        "date": "d" * 150, "sender": "s" * 400, "subject": "\x1b[31m\n" + "x" * 600,
    }))
    row = records(path)[0]
    assert len(row["email_date"]) == 100
    assert len(row["email_sender"]) == 320
    assert len(row["email_subject"]) == 500
    run_review(monkeypatch, path, ["q"])
    assert "\x1b" not in capsys.readouterr().out


def test_eof_keeps_pending_item(monkeypatch, tmp_path):
    path = tmp_path / "db.sqlite"
    save(path, prediction())
    monkeypatch.setattr("sys.argv", ["review", "--db", str(path)])

    def end_input(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", end_input)
    review_cli.main()
    assert records(path)[0]["review_status"] == "pending"
