"""Explicit activation and recovery using temporary configs and fake Gmail."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fake_gmail_mcp.server import create_server
from gmail_agent import taxonomy_activation as activation, taxonomy_apply_cli
from gmail_agent.classifier import CLASSIFIER_PROMPT_VERSION
from gmail_agent.gemini_classifier import GeminiBatchClassifier
from gmail_agent.history import HistoryDB
from gmail_agent.taxonomy import load_taxonomy
from gmail_agent.taxonomy_analysis import Proposal
from gmail_agent.triage import load_label_mapping, triage_search
from gmail_agent.mcp_client import GmailMCPClient
from real_gmail_mcp.backend import RealGmailBackend
from test_history import prediction
from test_label_suggestions import seed
from test_gemini_retries import response
from test_real_gmail import Service
from test_triage import SearchStore

DEFINITION = "Direct job offers and concrete employment opportunities requiring consideration."


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def setup(tmp_path):
    taxonomy_path = tmp_path / "taxonomy.json"
    label_map = tmp_path / "gmail_labels.json"
    taxonomy_path.write_bytes(Path("config/taxonomy.json").read_bytes())
    label_map.write_bytes(Path("config/gmail_labels.json").read_bytes())
    with HistoryDB(tmp_path / "history.sqlite") as db:
        db.save_run([prediction("original", "NOTIFICATION", subtype_hint="job_offer")])
        db.propose_label(db.records()[0]["id"], "job_offer", "Offre")
        service = Service({})
        yield db, taxonomy_path, label_map, service


def apply(setup, **changes):
    db, taxonomy_path, label_map, service = setup
    options = dict(apply=True, label_writes=True, taxonomy_path=taxonomy_path,
                   label_map_path=label_map, definition=DEFINITION,
                   backend_factory=lambda: RealGmailBackend(service, allow_label_writes=True))
    options.update(changes)
    return activation.activate_proposal(db, "job_offer", **options)


def test_approved_manual_activation_and_idempotency_preserve_history(setup):
    db, path, labels, service = setup
    original = json.loads(path.read_bytes())
    db.save_run([prediction("reviewed", subtype_hint="subscription_invoice")])
    db.review(db.records()[-1]["id"], "corrected", category="ADMIN",
              subtype_action="corrected", corrected_subtype="account_notice")
    predictions = db.records()
    result = apply(setup)
    assert result["status"] == "active"
    assert result["category_id"] == "JOB_OFFER"
    assert result["display_label"] == "Offre"
    assert result["taxonomy_version"] == "1.1.0"
    updated = json.loads(path.read_bytes())
    assert updated["categories"][:-1] == original["categories"]
    assert updated["categories"][-1]["definition"] == DEFINITION
    assert updated["categories"][-1]["gmail_label"] == "Offre"
    assert load_taxonomy(path).active_names[-1] == "JOB_OFFER"
    assert db.records() == predictions
    audit = db.activation_records()[0]
    assert audit["approved_at"] and audit["activated_at"]
    assert audit["gmail_label_id"] == result["gmail_label_id"]
    assert db.proposals()[0]["status"] == "accepted"
    after = path.read_bytes()
    again = apply(setup, definition=None)
    assert again["gmail_label_id"] == result["gmail_label_id"]
    assert path.read_bytes() == after
    assert len(service.labels_api.creates) == 1
    assert len(service.labels_api.calls) == 1
    assert service.messages_api.calls == []


def test_explicit_proposed_label_acceptance_is_eligible(tmp_path):
    path = tmp_path / "taxonomy.json"
    path.write_bytes(Path("config/taxonomy.json").read_bytes())
    with HistoryDB(tmp_path / "history.sqlite") as db:
        rows = seed(db, 8)
        db.propose_label(rows[0]["id"], accept_suggested=True)
        service = Service({})
        result = activation.activate_proposal(
            db, "job_offer", taxonomy_path=path, definition=DEFINITION,
            apply=True, label_writes=True,
            backend_factory=lambda: RealGmailBackend(service, allow_label_writes=True),
        )
        assert result["status"] == "active"
        assert result["display_label"] == "Job offer"


def test_unapproved_and_missing_candidates_never_contact_gmail(setup):
    db, path, _, service = setup
    with db.conn:
        db.conn.execute("DELETE FROM taxonomy_proposal_support")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="no explicit human approval"):
        apply(setup)
    with pytest.raises(ValueError, match="does not exist"):
        activation.activate_proposal(db, "absent_concept", taxonomy_path=path, definition=DEFINITION)
    assert path.read_bytes() == before
    assert service.labels_api.calls == []
    assert db.activation_records() == []


@pytest.mark.parametrize("status", ["rejected", "accepted"])
def test_incompatibly_resolved_proposal_rejected(setup, status):
    db, path, _, service = setup
    with db.conn:
        db.conn.execute("UPDATE taxonomy_proposals SET status = ?", (status,))
    with pytest.raises(ValueError, match="resolved incompatibly"):
        apply(setup)
    assert service.labels_api.calls == []


@pytest.mark.parametrize("writes", [False, True])
def test_without_apply_is_dry_run_even_with_write_gate(setup, writes):
    db, path, _, service = setup
    before = path.read_bytes()
    result = apply(setup, apply=False, label_writes=writes)
    assert result["dry_run"] is True
    assert path.read_bytes() == before
    assert service.labels_api.calls == service.labels_api.creates == []
    assert db.activation_records() == []


def test_missing_write_gate_prevents_mutation(setup):
    db, path, _, service = setup
    before = path.read_bytes()
    with pytest.raises(PermissionError, match="GMAIL_LABEL_WRITES"):
        apply(setup, label_writes=False)
    assert path.read_bytes() == before
    assert service.labels_api.calls == []
    assert db.activation_records() == []


def test_existing_exact_user_label_is_reused(setup):
    _, _, _, service = setup
    service.labels_api.items.append({"id": "existing-offre", "name": "Offre", "type": "user"})
    result = apply(setup)
    assert result["gmail_label_id"] == "existing-offre"
    assert service.labels_api.creates == []


@pytest.mark.parametrize("name", ["INBOX", "sent", "DRAFT", "CATEGORY_SOCIAL", "[Gmail]/Trash"])
def test_reserved_names_fail_before_gmail(setup, name):
    _, _, _, service = setup
    with pytest.raises(ValueError, match="Reserved"):
        apply(setup, display_label=name)
    assert service.labels_api.calls == []


@pytest.mark.parametrize("name", ["", " ", "x" * 81, "Bad\nLabel"])
def test_invalid_display_fails_before_gmail(setup, name):
    _, _, _, service = setup
    with pytest.raises(ValueError, match="Display label"):
        apply(setup, display_label=name)
    assert service.labels_api.calls == []


def test_absent_display_and_definition_require_explicit_human_input(setup):
    db, _, _, service = setup
    with pytest.raises(ValueError, match="definition"):
        apply(setup, definition=None)
    with db.conn:
        db.conn.execute("UPDATE taxonomy_proposals SET display_label = NULL")
    with pytest.raises(ValueError, match="Display label"):
        apply(setup)
    assert service.labels_api.calls == []


@pytest.mark.parametrize("name", ["Billing", "ADMIN", "review", "Processed", "Journeys"])
def test_label_conflicts_are_local_failures(setup, name):
    _, _, _, service = setup
    with pytest.raises(ValueError, match="conflicts"):
        apply(setup, display_label=name)
    assert service.labels_api.calls == []


def test_cap_and_category_duplicate_fail_before_gmail(setup):
    _, path, _, service = setup
    data = json.loads(path.read_bytes())
    data["max_primary_categories"] = len(data["categories"])
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="cap"):
        apply(setup)
    data["max_primary_categories"] += 1
    data["categories"].append({
        "name": "JOB_OFFER", "definition": DEFINITION, "positive_guidance": "Offers",
        "negative_guidance": "Other", "active": True, "gmail_label": "Offre",
    })
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="duplicates"):
        apply(setup)
    assert service.labels_api.calls == []


def test_invalid_stored_concept_rejected_before_gmail(setup):
    db, _, _, service = setup
    with db.conn:
        db.conn.execute("UPDATE taxonomy_proposals SET concept_key = 'JOB OFFER'")
    with pytest.raises(ValueError):
        apply(setup)
    assert service.labels_api.calls == []


def test_gmail_failure_leaves_taxonomy_unchanged_and_audits_failure(setup, monkeypatch):
    db, path, _, service = setup
    before = path.read_bytes()

    def fail(**kwargs):
        raise OSError("fake Gmail failure")

    monkeypatch.setattr(service.labels_api, "create", fail)
    with pytest.raises(activation.ActivationError, match="Gmail label resolution"):
        apply(setup)
    assert path.read_bytes() == before
    assert db.activation_records()[0]["status"] == "failed"
    assert db.proposals()[0]["status"] == "proposed"


def test_unexpected_system_or_case_conflict_never_creates_label(setup):
    _, path, _, service = setup
    before = path.read_bytes()
    service.labels_api.items.append({"id": "custom-system", "name": "Offre", "type": "system"})
    with pytest.raises(activation.ActivationError):
        apply(setup)
    assert service.labels_api.creates == []
    assert path.read_bytes() == before


def test_taxonomy_write_failure_preserves_label_and_retry_reuses_it(setup, monkeypatch):
    db, path, _, service = setup
    before = path.read_bytes()
    original_writer = activation.atomic_write_taxonomy

    def fail(*args):
        raise OSError("fake disk failure")

    monkeypatch.setattr(activation, "atomic_write_taxonomy", fail)
    with pytest.raises(activation.ActivationError, match="retained"):
        apply(setup)
    assert path.read_bytes() == before
    assert db.activation_records()[0]["status"] == "failed"
    assert db.activation_records()[0]["gmail_label_id"]
    assert len(service.labels_api.creates) == 1
    monkeypatch.setattr(activation, "atomic_write_taxonomy", original_writer)
    result = apply(setup, definition=None)
    assert result["status"] == "active"
    assert len(service.labels_api.creates) == 1


def test_interruption_after_creation_recovers(setup, monkeypatch):
    db, path, _, service = setup
    original_writer = activation.atomic_write_taxonomy

    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(activation, "atomic_write_taxonomy", interrupt)
    with pytest.raises(KeyboardInterrupt):
        apply(setup)
    assert db.activation_records()[0]["status"] == "activating"
    monkeypatch.setattr(activation, "atomic_write_taxonomy", original_writer)
    apply(setup, definition=None)
    assert len(service.labels_api.creates) == 1
    assert load_taxonomy(path).version == "1.1.0"


def test_lost_gmail_creation_response_recovers_without_duplicate(setup, monkeypatch):
    _, _, _, service = setup
    original_create = service.labels_api.create

    def lost_response(**kwargs):
        original_create(**kwargs)
        raise OSError("response lost after Gmail created the label")

    monkeypatch.setattr(service.labels_api, "create", lost_response)
    with pytest.raises(activation.ActivationError):
        apply(setup)
    monkeypatch.setattr(service.labels_api, "create", original_create)
    apply(setup, definition=None)
    assert len(service.labels_api.creates) == 1


def test_interruption_after_taxonomy_write_finalizes_without_rewrite(setup, monkeypatch):
    db, path, _, service = setup
    finish = activation._finish_activation

    def fail(*args):
        raise OSError("audit temporarily unavailable")

    monkeypatch.setattr(activation, "_finish_activation", fail)
    with pytest.raises(activation.ActivationError, match="audit finalization"):
        apply(setup)
    after = path.read_bytes()
    assert load_taxonomy(path).version == "1.1.0"
    monkeypatch.setattr(activation, "_finish_activation", finish)

    def forbidden():
        raise AssertionError("Recovery of a configured activation must not need Gmail")

    apply(setup, definition=None, backend_factory=forbidden)
    assert path.read_bytes() == after
    assert len(service.labels_api.creates) == 1
    assert db.activation_records()[0]["status"] == "active"


def test_atomic_writer_does_not_overwrite_concurrent_edit(setup):
    _, path, _, _ = setup
    before = path.read_bytes()
    edited = json.loads(before)
    edited["version"] = "1.0.1"
    path.write_text(json.dumps(edited))
    with pytest.raises(ValueError, match="changed during activation"):
        activation.atomic_write_taxonomy(path, json.loads(before), before)
    assert json.loads(path.read_bytes()) == edited
    assert list(path.parent.glob(".taxonomy.json.*")) == []


def test_cli_list_dry_run_and_missing_gate_do_not_construct_backend(setup, monkeypatch, capsys):
    db, path, label_map, _ = setup

    def forbidden(**kwargs):
        raise AssertionError("No live/backend calls from list or dry-run")

    monkeypatch.setattr(taxonomy_apply_cli, "RealGmailBackend", forbidden)
    base = ["apply", "--db", str(db.path), "--taxonomy", str(path), "--label-map", str(label_map)]
    monkeypatch.setattr("sys.argv", [*base, "--list"])
    taxonomy_apply_cli.main()
    assert json.loads(capsys.readouterr().out)[0]["status"] == "human_approved"
    monkeypatch.setattr("sys.argv", [*base, "--proposal", "job_offer", "--definition", DEFINITION])
    taxonomy_apply_cli.main()
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    monkeypatch.delenv("GMAIL_LABEL_WRITES", raising=False)
    monkeypatch.setattr("sys.argv", [*base, "--proposal", "job_offer", "--apply", "--definition", DEFINITION])
    with pytest.raises(SystemExit) as caught:
        taxonomy_apply_cli.main()
    assert caught.value.code == 2


def test_backend_activation_allowlist_and_gate(setup):
    _, _, _, service = setup
    with pytest.raises(PermissionError):
        RealGmailBackend(service).ensure_approved_label("Offre", approved_names=frozenset({"Offre"}))
    with pytest.raises(PermissionError):
        RealGmailBackend(service, allow_label_writes=True).ensure_approved_label("Other", approved_names=frozenset({"Offre"}))
    assert service.labels_api.calls == []


@pytest.mark.anyio
async def test_future_schema_prompt_mapping_and_triage_use_activated_category(setup, monkeypatch):
    db, path, label_map, service = setup
    apply(setup)
    taxonomy = load_taxonomy(path)
    mapping = load_label_mapping(label_map, taxonomy)
    assert mapping.category_labels["JOB_OFFER"] == "Offre"
    assert mapping.category_labels["FINANCE"] == "Billing"
    assert mapping.review_label == "Review" and mapping.processed_label == "Processed"
    text = json.dumps({"results": [{
        "index": 0, "category": "JOB_OFFER", "confidence": 0.95,
        "subtype_hint": None, "needs_reply": True, "importance": "normal",
        "deadline": None, "short_reason": "Concrete offer",
    }]})
    generate = AsyncMock(return_value=response(text))
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
    # A new classifier loads current configuration without reloading Python modules.
    monkeypatch.setattr("gmail_agent.gemini_classifier.load_taxonomy", lambda: taxonomy)
    provider = GeminiBatchClassifier(client)
    monkeypatch.setattr("real_gmail_mcp.backend.load_label_mapping", lambda: mapping)
    backend = RealGmailBackend(service, allow_label_writes=True)
    async with GmailMCPClient(create_server(SearchStore(1))) as mcp:
        report = await triage_search("", provider, mcp, db, apply=True, label_writes=True,
                                     label_backend=backend, taxonomy=taxonomy, mapping=mapping)
    assert report.outcomes[0].action_label == "Offre"
    assert report.outcomes[0].processed is True
    request = generate.await_args.kwargs
    assert "JOB_OFFER" in request["config"].response_json_schema["properties"]["results"]["items"]["properties"]["category"]["enum"]
    assert DEFINITION in request["contents"]
    assert db.records()[-1]["taxonomy_version"] == "1.1.0"
    assert db.records()[-1]["prompt_version"] == CLASSIFIER_PROMPT_VERSION
    assert service.messages_api.calls[-2][1]["body"]["addLabelIds"] == [db.activation_records()[0]["gmail_label_id"]]


def test_activation_migration_preserves_existing_data(setup):
    db, _, _, _ = setup
    before = db.records()
    proposals = db.proposals()
    with db.conn:
        db.conn.execute("DROP TABLE taxonomy_activations")
    with HistoryDB(db.path) as reopened:
        assert reopened.records() == before
        assert reopened.proposals() == proposals
        assert reopened.activation_records() == []


def test_conflicting_case_user_label_does_not_create(setup):
    _, path, _, service = setup
    before = path.read_bytes()
    service.labels_api.items.append({"id": "lower-offre", "name": "offre", "type": "user"})
    with pytest.raises(activation.ActivationError):
        apply(setup)
    assert path.read_bytes() == before
    assert service.labels_api.creates == []


def test_active_proposal_with_changed_category_fails_closed(setup):
    _, path, _, service = setup
    apply(setup)
    data = json.loads(path.read_bytes())
    data["categories"][-1]["gmail_label"] = "Different Label"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="duplicates"):
        apply(setup)
    assert len(service.labels_api.creates) == 1


def test_busy_taxonomy_lock_prevents_activation_without_waiting(setup):
    _, path, _, service = setup
    with activation._activation_lock(path):
        with pytest.raises(activation.ActivationError, match="in progress"):
            apply(setup)
    assert service.labels_api.calls == []


def test_cli_apply_uses_both_gates_and_fake_backend(setup, monkeypatch, capsys):
    db, path, label_map, service = setup
    constructions = []

    def factory(**kwargs):
        constructions.append(kwargs)
        return RealGmailBackend(service, **kwargs)

    monkeypatch.setattr(taxonomy_apply_cli, "RealGmailBackend", factory)
    monkeypatch.setenv("GMAIL_LABEL_WRITES", "1")
    monkeypatch.setattr("sys.argv", [
        "apply", "--db", str(db.path), "--taxonomy", str(path), "--label-map", str(label_map),
        "--proposal", "job_offer", "--definition", DEFINITION, "--apply",
    ])
    taxonomy_apply_cli.main()
    assert json.loads(capsys.readouterr().out)["status"] == "active"
    assert constructions == [{"allow_label_writes": True}]
    assert len(service.labels_api.creates) == 1
