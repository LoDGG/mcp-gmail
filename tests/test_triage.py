"""Triage policy tests with only fake search, model, labels, and SQLite."""

import json

import pytest

from fake_gmail_mcp.server import create_server
from gmail_agent.classifier import BatchResponse, ClassificationError, Usage
from gmail_agent.history import HistoryDB, ground_truth
from gmail_agent.mcp_client import GmailMCPClient
from gmail_agent.triage import (
    AUTO_CONFIDENCE_THRESHOLD, DEFAULT_QUERY,
    load_label_mapping, triage_search,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


class SearchStore:
    def __init__(self, count):
        self.count = count
        self.calls = []

    def search_emails(self, query):
        self.calls.append(("search", query))
        return [
            {"id": f"m-{index}", "thread_id": f"t-{index}", "sender": "sender@example.test",
             "subject": "subject", "labels": ["INBOX"]}
            for index in range(self.count)
        ]

    def get_email(self, message_id):
        raise AssertionError("full body fetch is forbidden")

    def get_thread(self, thread_id):
        raise AssertionError("thread fetch is forbidden")

    def apply_label(self, message_id, label):
        raise AssertionError("MCP label mutation is forbidden")


class Model:
    def __init__(self, choices=None):
        self.batches = []
        self.choices = choices or {}

    async def classify(self, emails):
        self.batches.append(emails)
        results = []
        for email in emails:
            category, confidence = self.choices.get(email["index"], ("FINANCE", 0.95))
            results.append({
                "index": email["index"], "category": category, "confidence": confidence,
                "needs_reply": False, "importance": "normal", "deadline": None, "short_reason": "Brief",
            })
        return BatchResponse(json.dumps({"results": results}), Usage("fake", 10, 5, 17, 2))


class Labels:
    def __init__(self, *, missing=(), fail_on=(), fail_create=False):
        self.names = load_label_mapping().required_labels - set(missing)
        self.fail_on = set(fail_on)
        self.fail_create = fail_create
        self.calls = []
        self.creates = []
        self.preflights = 0
        self.applied = {}

    def ensure_triage_labels(self):
        self.preflights += 1
        for name in sorted(load_label_mapping().required_labels - self.names):
            if self.fail_create:
                raise ValueError("mock creation failure")
            self.creates.append(name)
            self.names.add(name)
        return {name: name for name in self.names}

    def apply_label(self, message_id, label):
        self.calls.append((message_id, label))
        if (message_id, label) in self.fail_on:
            raise ValueError("mock write failure")
        self.applied.setdefault(message_id, set()).add(label)
        return {"message_id": message_id, "label": label}


@pytest.mark.anyio
async def test_no_emails_makes_zero_model_requests_and_no_writes(tmp_path):
    model = Model()
    labels = Labels()
    with HistoryDB(tmp_path / "db.sqlite") as db:
        async with GmailMCPClient(create_server(SearchStore(0))) as mcp:
            report = await triage_search(DEFAULT_QUERY, model, mcp, db, apply=True, label_writes=True, label_backend=labels)
        assert db.records() == []
    assert report.emails_found == 0
    assert report.emails_classified == 0
    assert report.request_count == 0
    assert model.batches == []
    assert labels.preflights == 1
    assert labels.creates == []
    assert labels.calls == []


@pytest.mark.anyio
async def test_ten_emails_one_model_batch_persisted_before_labels(tmp_path):
    model = Model()
    labels = Labels()
    with HistoryDB(tmp_path / "db.sqlite") as db:
        async with GmailMCPClient(create_server(SearchStore(10))) as mcp:
            report = await triage_search(DEFAULT_QUERY, model, mcp, db, apply=True, label_writes=True, label_backend=labels)
        rows = db.records()
    assert len(model.batches) == 1
    assert len(model.batches[0]) == 10
    assert report.request_count == 1
    assert report.emails_classified == 10
    assert report.usage == [Usage("fake", 10, 5, 17, 2)]
    assert len(rows) == 10
    assert all(row["review_status"] == "pending" and ground_truth(row) is None for row in rows)
    assert labels.preflights == 1
    assert len(labels.calls) == 20
    assert all(labels.calls[i + 1] == (labels.calls[i][0], "Processed") for i in range(0, 20, 2))


@pytest.mark.anyio
async def test_both_opt_ins_required_and_dry_run_never_mutates(tmp_path):
    model = Model()
    labels = Labels()
    with HistoryDB(tmp_path / "db.sqlite") as db:
        async with GmailMCPClient(create_server(SearchStore(1))) as mcp:
            with pytest.raises(PermissionError, match="GMAIL_LABEL_WRITES"):
                await triage_search("", model, mcp, db, apply=True, label_writes=False, label_backend=labels)
            report = await triage_search("", model, mcp, db, apply=False, label_writes=True, label_backend=labels)
    assert report.emails_classified == 1
    assert report.outcomes[0].processed is False
    assert labels.calls == []
    assert labels.preflights == 0
    assert labels.creates == []
    assert len(model.batches) == 1


@pytest.mark.anyio
async def test_high_low_and_uncertain_actions_and_processed_last(tmp_path):
    assert AUTO_CONFIDENCE_THRESHOLD == 0.90
    model = Model({0: ("FINANCE", 0.90), 1: ("FINANCE", 0.89), 2: ("UNCERTAIN", 0.99), 3: ("TRAVEL", 0.95)})
    labels = Labels()
    with HistoryDB(tmp_path / "db.sqlite") as db:
        async with GmailMCPClient(create_server(SearchStore(4))) as mcp:
            report = await triage_search("", model, mcp, db, apply=True, label_writes=True, label_backend=labels)
    assert labels.calls == [
        ("m-0", "Billing"), ("m-0", "Processed"),
        ("m-1", "Review"), ("m-1", "Processed"),
        ("m-2", "Review"), ("m-2", "Processed"),
        ("m-3", "Journeys"), ("m-3", "Processed"),
    ]
    assert all(item.processed for item in report.outcomes)
    assert [item.action_label for item in report.outcomes] == ["Billing", "Review", "Review", "Journeys"]


@pytest.mark.anyio
async def test_primary_label_failure_prevents_processed_for_that_message(tmp_path):
    model = Model()
    labels = Labels(fail_on={("m-0", "Billing")})
    with HistoryDB(tmp_path / "db.sqlite") as db:
        async with GmailMCPClient(create_server(SearchStore(2))) as mcp:
            report = await triage_search("", model, mcp, db, apply=True, label_writes=True, label_backend=labels)
        assert len(db.records()) == 2
    assert labels.calls == [("m-0", "Billing"), ("m-1", "Billing"), ("m-1", "Processed")]
    assert report.outcomes[0].processed is False
    assert report.outcomes[0].error == "primary label failed"
    assert report.outcomes[1].processed is True


@pytest.mark.anyio
async def test_missing_labels_are_created_from_config_before_model(tmp_path):
    model = Model()
    labels = Labels(missing={"Journeys", "Processed"})
    with HistoryDB(tmp_path / "db.sqlite") as db:
        async with GmailMCPClient(create_server(SearchStore(1))) as mcp:
            report = await triage_search("", model, mcp, db, apply=True, label_writes=True, label_backend=labels)
        assert len(db.records()) == 1
    assert labels.creates == ["Journeys", "Processed"]
    assert set(labels.creates) <= load_label_mapping().required_labels
    assert len(model.batches) == 1
    assert report.outcomes[0].processed is True


@pytest.mark.anyio
async def test_label_creation_failure_aborts_before_model_and_message_mutation(tmp_path):
    model = Model()
    labels = Labels(missing={"Journeys"}, fail_create=True)
    with HistoryDB(tmp_path / "db.sqlite") as db:
        async with GmailMCPClient(create_server(SearchStore(1))) as mcp:
            with pytest.raises(ValueError, match="creation failure"):
                await triage_search("", model, mcp, db, apply=True, label_writes=True, label_backend=labels)
        assert db.records() == []
    assert model.batches == []
    assert labels.calls == []


@pytest.mark.anyio
async def test_model_cannot_inject_a_gmail_label_name(tmp_path):
    model = Model({0: ("Arbitrary Gmail Label", 0.99)})
    labels = Labels(missing={"Billing"})
    with HistoryDB(tmp_path / "db.sqlite") as db:
        async with GmailMCPClient(create_server(SearchStore(1))) as mcp:
            with pytest.raises(ClassificationError):
                await triage_search("", model, mcp, db, apply=True, label_writes=True, label_backend=labels)
        assert db.records() == []
    assert labels.creates == ["Billing"]
    assert labels.calls == []
    assert "Arbitrary Gmail Label" not in labels.names


def test_label_mapping_is_complete_and_unique():
    mapping = load_label_mapping()
    assert mapping.category_labels["ADMIN"] == "Admin"
    assert mapping.category_labels["FINANCE"] == "Billing"
    assert mapping.category_labels["TRAVEL"] == "Journeys"
    assert mapping.category_labels["NOTIFICATION"] == "Notification"
    assert mapping.review_label == "Review"
    assert mapping.processed_label == "Processed"
    assert len(mapping.required_labels) == 12
