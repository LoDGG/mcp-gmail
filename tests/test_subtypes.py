"""Non-binding subtype validation, migration, analysis, and Gmail safety."""

import json
import sqlite3

import pytest

from fake_gmail_mcp.server import create_server
from gmail_agent.classifier import BatchResponse, ClassificationError, parse_results
from gmail_agent.history import HistoryDB, ground_truth
from gmail_agent.mcp_client import GmailMCPClient
from gmail_agent.taxonomy_analysis import calculate_stats, recurring_subtypes
from gmail_agent import taxonomy_stats
from gmail_agent.triage import triage_search
from test_history import prediction
from test_triage import Labels, Model, SearchStore


@pytest.fixture
def anyio_backend():
    return "asyncio"


def model_result(hint):
    return {"index": 0, "category": "NEWSLETTER", "confidence": 0.95,
            "needs_reply": False, "importance": "normal", "deadline": None,
            "short_reason": "Brief", "subtype_hint": hint}


@pytest.mark.parametrize("hint", ["job_alert", "product_marketing", "recruiter", "a" * 40, None])
def test_valid_and_null_hints(hint):
    result = parse_results(json.dumps({"results": [model_result(hint)]}), ["m-0"])[0]
    assert result["subtype_hint"] == hint


@pytest.mark.parametrize("hint", [
    "Job_Alert", "job-alert", "job alert", "_job", "job_", "job__alert",
    "", "a" * 41, "job_alert\n", "newsletter", 42, ["job_alert"],
])
def test_invalid_hints_rejected(hint):
    with pytest.raises(ClassificationError):
        parse_results(json.dumps({"results": [model_result(hint)]}), ["m-0"])


def test_model_must_explicitly_supply_hint_or_null():
    item = model_result(None)
    del item["subtype_hint"]
    with pytest.raises(ClassificationError):
        parse_results(json.dumps({"results": [item]}), ["m-0"])


def test_persistence_review_and_ground_truth_exclude_hint(tmp_path):
    path = tmp_path / "history.sqlite"
    with HistoryDB(path) as db:
        db.save_run([
            prediction("accepted", subtype_hint="subscription_invoice"),
            prediction("corrected", subtype_hint="subscription_invoice"),
            prediction("null", subtype_hint=None),
            prediction("legacy"),
        ])
        first, second, *_ = db.records()
        db.review(first["id"], "accepted")
        db.review(second["id"], "corrected", category="ADMIN")
        with pytest.raises(ValueError, match="subtype_hint"):
            db.save_run([prediction("invalid", subtype_hint="INVALID")])
    with HistoryDB(path) as db:
        accepted, corrected, null, legacy = db.records()
        assert accepted["subtype_hint"] == corrected["subtype_hint"] == "subscription_invoice"
        assert null["subtype_hint"] is legacy["subtype_hint"] is None
        assert ground_truth(corrected)["category"] == "ADMIN"
        assert ground_truth(accepted)["category"] == "FINANCE"
        assert "subtype_hint" not in ground_truth(accepted)
        assert "subtype_hint" not in ground_truth(corrected)


def test_existing_database_migration_preserves_records_and_reviews(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with HistoryDB(path) as db:
        db.save_run([prediction()], run_key="old-run")
        db.review(db.records()[0]["id"], "corrected", category="ADMIN")
        before = db.records()[0]
    # Recreate the pre-increment schema, retaining its data and constraints.
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE classifications DROP COLUMN subtype_hint")
        assert "subtype_hint" not in {row[1] for row in conn.execute("PRAGMA table_info(classifications)")}
    for _ in range(2):
        with HistoryDB(path) as db:
            assert db.records()[0] == before
            assert db.records()[0]["subtype_hint"] is None
            assert ground_truth(db.records()[0])["category"] == "ADMIN"
            assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    with HistoryDB(path) as db:
        db.save_run([prediction("new", subtype_hint="subscription_invoice")])
        assert db.records()[1]["subtype_hint"] == "subscription_invoice"


def test_recurring_hints_use_distinct_messages_and_reviewed_categories(tmp_path):
    with HistoryDB(tmp_path / "history.sqlite") as db:
        db.save_run([
            prediction("a", "NEWSLETTER", subtype_hint="job_alert"),
            prediction("b", "NEWSLETTER", subtype_hint="job_alert"),
            prediction("c", "NEWSLETTER", subtype_hint="job_alert"),
            prediction("d", "NEWSLETTER", subtype_hint="job_alert"),
            prediction("e", "NEWSLETTER", subtype_hint="product_marketing"),
            prediction("f", "FINANCE", subtype_hint=None),
        ])
        a, b, c, d, *_ = db.records()
        db.review(a["id"], "accepted")
        db.review(d["id"], "corrected", category="PROFESSIONAL")
        # Repeated scheduled predictions do not inflate evidence or override review.
        db.save_run([prediction("a", "FINANCE", subtype_hint="subscription_invoice"),
                     prediction("b", "NEWSLETTER", subtype_hint="job_alert")])
        rows = db.records()
    assert calculate_stats(rows)["subtype_candidates"] == [{
        "category": "NEWSLETTER", "subtype_hint": "job_alert", "distinct_messages": 3,
        "reviewed_category_messages": 1, "unreviewed_category_messages": 2,
        "hint_status": "unverified",
    }]
    assert recurring_subtypes(rows, min_messages=4) == []
    candidates = recurring_subtypes(rows, min_messages=1)
    assert {(item["category"], item["subtype_hint"], item["distinct_messages"]) for item in candidates} == {
        ("NEWSLETTER", "job_alert", 3), ("NEWSLETTER", "product_marketing", 1),
        ("PROFESSIONAL", "job_alert", 1),
    }
    with pytest.raises(ValueError):
        recurring_subtypes(rows, min_messages=0)


def test_stats_cli_configurable_threshold(tmp_path, monkeypatch, capsys):
    path = tmp_path / "history.sqlite"
    with HistoryDB(path) as db:
        db.save_run([prediction("a", subtype_hint="subscription_invoice"),
                     prediction("b", subtype_hint="subscription_invoice")])
    monkeypatch.setattr("sys.argv", ["stats", "--db", str(path), "--subtype-min-messages", "2"])
    taxonomy_stats.main()
    output = json.loads(capsys.readouterr().out)
    assert output["statistics"]["subtype_candidates"][0]["distinct_messages"] == 2
    with HistoryDB(path) as db:
        assert db.proposals() == []


@pytest.mark.anyio
async def test_subtype_never_selects_or_creates_gmail_labels(tmp_path):
    class HintModel(Model):
        async def classify(self, emails):
            response = await super().classify(emails)
            data = json.loads(response.text)
            for item in data["results"]:
                item["subtype_hint"] = "arbitrary_new_label"
            return BatchResponse(json.dumps(data), response.usage)

    model = HintModel({0: ("FINANCE", 0.90), 1: ("FINANCE", 0.89), 2: ("UNCERTAIN", 0.99)})
    labels = Labels()
    with HistoryDB(tmp_path / "history.sqlite") as db:
        async with GmailMCPClient(create_server(SearchStore(3))) as mcp:
            await triage_search("", model, mcp, db, apply=True, label_writes=True, label_backend=labels)
        assert all(row["subtype_hint"] == "arbitrary_new_label" for row in db.records())
    assert labels.creates == []
    assert labels.calls == [
        ("m-0", "Billing"), ("m-0", "Processed"),
        ("m-1", "Review"), ("m-1", "Processed"),
        ("m-2", "Review"), ("m-2", "Processed"),
    ]
    assert "arbitrary_new_label" not in labels.names
