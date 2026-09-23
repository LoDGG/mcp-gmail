"""Offline integration of recurring evidence, human approval, and activation."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from fake_gmail_mcp.server import create_server
from gmail_agent.classifier import build_classification_schema
from gmail_agent.history import HistoryDB
from gmail_agent.mcp_client import GmailMCPClient
from gmail_agent.taxonomy import load_taxonomy
from gmail_agent.taxonomy_activation import activate_proposal, prepare_activation
from gmail_agent.triage import load_label_mapping, triage_search
from real_gmail_mcp.backend import RealGmailBackend
from test_history import prediction
from test_real_gmail import Service
from test_triage import Model, SearchStore

DEFINITION = "Direct job offers and concrete employment opportunities requiring consideration."


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def lifecycle(tmp_path):
    taxonomy_path = tmp_path / "taxonomy.json"
    label_map_path = tmp_path / "gmail_labels.json"
    taxonomy_path.write_bytes(Path("config/taxonomy.json").read_bytes())
    label_map_path.write_bytes(Path("config/gmail_labels.json").read_bytes())
    with HistoryDB(tmp_path / "history.sqlite") as db:
        yield db, taxonomy_path, label_map_path, Service({})


def seed_predictions(db, taxonomy, count):
    for run in range(2):
        db.save_run([
            prediction(f"evidence-{index}", "NOTIFICATION", subtype_hint="job_offer",
                       review_metadata={"sender": f"source{index % 3}@example.test"})
            for index in range(count) if index % 2 == run
        ], taxonomy, run_key=f"evidence-run-{run}")
    return db.records()


@pytest.mark.anyio
async def test_human_supported_concept_activates_and_flows_into_future_triage(lifecycle, monkeypatch):
    db, taxonomy_path, label_map_path, service = lifecycle
    initial = load_taxonomy(taxonomy_path)
    assert "JOB_OFFER" not in initial.active_names
    original_predictions = seed_predictions(db, initial, 3)
    current = original_predictions[-1]
    assert db.label_suggestion(current["id"], taxonomy=initial) is None

    # Subtype review alone supplies evidence, but does not approve a CREATE.
    for row in original_predictions[:2]:
        db.review_subtype(row["id"], "accepted")
    suggestion = db.label_suggestion(current["id"], taxonomy=initial)
    assert suggestion is not None
    assert suggestion.concept_key == "job_offer"
    assert suggestion.evidence["distinct_messages"] == 3
    assert suggestion.evidence["human_confirmed_messages"] == 2
    assert db.proposals() == []

    # Same API as review_cli's n job_offer "Offre"; review remains local.
    proposal_id = db.propose_label(current["id"], suggestion.concept_key, "Offre", taxonomy=initial)
    proposal = db.proposals()[0]
    assert proposal["id"] == proposal_id
    assert proposal["proposal_type"] == "CREATE"
    assert proposal["concept_key"] == "job_offer"
    assert proposal["display_label"] == "Offre"
    assert db.proposal_supports()[0]["source"] == "human_manual"
    assert service.labels_api.calls == service.labels_api.creates == []
    reviewed_history = db.records()
    for original, reviewed in zip(original_predictions, reviewed_history):
        for key in ("predicted_category", "predicted_confidence", "subtype_hint"):
            assert reviewed[key] == original[key]

    options = dict(taxonomy_path=taxonomy_path, label_map_path=label_map_path)
    plan = prepare_activation(db, "job_offer", definition=DEFINITION, **options)
    assert plan.summary()["status"] == "human_approved"

    # Existing built-in labels isolate the one new label created by this lifecycle.
    service.labels_api.items.extend(
        {"id": f"existing-{name}", "name": name, "type": "user"}
        for name in sorted(load_label_mapping(label_map_path, initial).required_labels)
    )
    backend = RealGmailBackend(service, allow_label_writes=True)
    result = activate_proposal(
        db, "job_offer", apply=True, label_writes=True, definition=DEFINITION,
        backend_factory=lambda: backend, **options,
    )
    assert result["status"] == "active"
    assert service.labels_api.creates == [{"userId": "me", "body": {"name": "Offre"}}]
    assert service.messages_api.calls == []  # Activation itself never relabels mail.
    assert db.records() == reviewed_history

    # Reload disk configuration; do not inject a fabricated category or mapping.
    reloaded = load_taxonomy(taxonomy_path)
    major, minor, _ = map(int, initial.version.split("."))
    assert reloaded.version == f"{major}.{minor + 1}.0"
    assert reloaded.categories[:-1] == initial.categories
    category = reloaded.categories[-1]
    assert category["name"] == "JOB_OFFER"
    assert category["concept_key"] == "job_offer"
    assert category["gmail_label"] == "Offre"
    assert category["definition"] == DEFINITION
    audit = db.activation_records()[0]
    assert audit["status"] == "active"
    assert audit["proposal_id"] == proposal_id
    assert audit["approved_at"] and audit["activated_at"]
    assert audit["taxonomy_version"] == reloaded.version
    schema = build_classification_schema(reloaded)
    assert "JOB_OFFER" in schema["properties"]["results"]["items"]["properties"]["category"]["enum"]
    mapping = load_label_mapping(label_map_path, reloaded)
    assert mapping.category_labels["JOB_OFFER"] == "Offre"

    # Redirect the backend's config lookup to the temporary files, retaining its
    # real allowlist/label-ID handling. Model and email search are local fakes.
    monkeypatch.setattr(
        "real_gmail_mcp.backend.load_label_mapping",
        lambda: load_label_mapping(label_map_path, load_taxonomy(taxonomy_path)),
    )
    async with GmailMCPClient(create_server(SearchStore(1))) as mcp:
        report = await triage_search(
            "", Model({0: ("JOB_OFFER", 0.95)}), mcp, db,
            apply=True, label_writes=True, label_backend=backend,
            taxonomy=reloaded, mapping=mapping,
        )
    assert report.outcomes[0].action_label == "Offre"
    assert report.outcomes[0].processed is True
    assert service.messages_api.calls == [
        ("modify", {"userId": "me", "id": "m-0", "body": {"addLabelIds": [audit["gmail_label_id"]]}}),
        ("modify", {"userId": "me", "id": "m-0", "body": {"addLabelIds": ["existing-Processed"]}}),
    ]

    activated_bytes = taxonomy_path.read_bytes()
    label_calls = list(service.labels_api.calls)
    second = activate_proposal(
        db, "job_offer", apply=True, label_writes=True,
        backend_factory=lambda: backend, **options,
    )
    assert second["gmail_label_id"] == result["gmail_label_id"]
    assert taxonomy_path.read_bytes() == activated_bytes
    assert load_taxonomy(taxonomy_path).active_names.count("JOB_OFFER") == 1
    assert service.labels_api.calls == label_calls
    assert service.labels_api.creates == [{"userId": "me", "body": {"name": "Offre"}}]
    assert len([label for label in service.labels_api.items if label["name"] == "Offre"]) == 1
    assert db.activation_records() == [audit]
    with HistoryDB(db.path) as reopened:
        assert reopened.records()[:len(reviewed_history)] == reviewed_history
        assert reopened.records()[-1]["predicted_category"] == "JOB_OFFER"


def test_raw_recurring_suggestion_cannot_activate_without_human_approval(lifecycle):
    db, taxonomy_path, label_map_path, service = lifecycle
    taxonomy = load_taxonomy(taxonomy_path)
    history = seed_predictions(db, taxonomy, 8)
    suggestion = db.label_suggestion(history[-1]["id"], taxonomy=taxonomy)
    assert suggestion is not None  # Enough recurrence to suggest, never to approve.
    assert suggestion.evidence["distinct_messages"] == 8
    assert suggestion.evidence["human_confirmed_messages"] == 0
    assert db.proposals() == db.proposal_supports() == []
    original_bytes = taxonomy_path.read_bytes()
    backend_factory = Mock(return_value=RealGmailBackend(service, allow_label_writes=True))

    with pytest.raises(ValueError, match="CREATE proposal does not exist"):
        activate_proposal(
            db, suggestion.concept_key, apply=True, label_writes=True,
            taxonomy_path=taxonomy_path, label_map_path=label_map_path,
            display_label="Offre", definition=DEFINITION, backend_factory=backend_factory,
        )

    backend_factory.assert_not_called()
    assert service.labels_api.calls == service.labels_api.creates == []
    assert service.messages_api.calls == []
    assert taxonomy_path.read_bytes() == original_bytes
    assert "JOB_OFFER" not in load_taxonomy(taxonomy_path).active_names
    assert db.activation_records() == db.proposals() == db.proposal_supports() == []
    assert db.records() == history
