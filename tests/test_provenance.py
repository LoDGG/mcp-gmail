"""Explicit provenance and provider-independent learning; all APIs are fake."""

from dataclasses import replace
import sqlite3

import pytest

from fake_gmail_mcp.server import create_server
from gmail_agent import classify_cli, taxonomy_stats
from gmail_agent.classifier import (
    BatchResponse, CLASSIFIER_PROMPT_VERSION, CLASSIFIER_TAXONOMY,
    ClassificationError, classify_search,
)
from gmail_agent.history import HistoryDB, ground_truth
from gmail_agent.mcp_client import GmailMCPClient
from gmail_agent.provenance import PredictionProvenance
from gmail_agent.taxonomy import load_taxonomy
from gmail_agent.taxonomy_analysis import calculate_stats
from gmail_agent.triage import triage_search
from test_gemini_retries import provider_for, response
from test_history import prediction
from test_triage import Model, SearchStore


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("configured_model", [None, "test-gemini-model-version"])
async def test_gemini_configured_model_taxonomy_and_prompt_stored(tmp_path, monkeypatch, configured_model):
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    if configured_model:
        monkeypatch.setenv("GEMINI_MODEL", configured_model)
    provider, generate, sleep = provider_for([response()])
    path = tmp_path / "history.sqlite"
    with HistoryDB(path) as db:
        async with GmailMCPClient(create_server(SearchStore(1))) as mcp:
            await triage_search("", provider, mcp, db)
        row = db.records()[0]
        run = dict(db.conn.execute("SELECT * FROM classification_runs").fetchone())
    model = configured_model or "gemini-3.6-flash"
    expected = {
        "provider_id": "gemini", "model_id": model,
        "taxonomy_version": load_taxonomy().version, "prompt_version": CLASSIFIER_PROMPT_VERSION,
    }
    assert CLASSIFIER_PROMPT_VERSION == "classifier-v2"
    for key, value in expected.items():
        assert row[key] == run[key] == value
    assert row["run_id"] == run["id"]
    assert generate.await_args.kwargs["model"] == model
    sleep.assert_not_awaited()


@pytest.mark.anyio
async def test_classify_cli_save_preserves_provenance(tmp_path, monkeypatch):
    path = tmp_path / "history.sqlite"
    monkeypatch.setenv("GMAIL_BACKEND", "fake")
    monkeypatch.setenv("GEMINI_MODEL", "configured-cli-model")
    provider, _, _ = provider_for([response()])
    monkeypatch.setattr(classify_cli, "GeminiBatchClassifier", lambda: provider)
    monkeypatch.setattr(classify_cli, "create_configured_server", lambda **kwargs: create_server(SearchStore(1)))
    monkeypatch.setattr("sys.argv", ["classify", "--save", "--db", str(path)])
    await classify_cli.main()
    with HistoryDB(path) as db:
        assert db.records()[0]["model_id"] == "configured-cli-model"
        assert db.records()[0]["prompt_version"] == CLASSIFIER_PROMPT_VERSION


@pytest.mark.anyio
async def test_multiple_batches_share_one_run_provenance(tmp_path):
    class VersionedModel(Model):
        async def classify(self, emails):
            result = await super().classify(emails)
            return BatchResponse(result.text, result.usage, provenance=PredictionProvenance(
                "local", "local-model-v2", CLASSIFIER_TAXONOMY.version, "local-classifier-v1",
            ))

    provider = VersionedModel()
    with HistoryDB(tmp_path / "history.sqlite") as db:
        async with GmailMCPClient(create_server(SearchStore(3))) as mcp:
            report = await classify_search("", provider, mcp, batch_size=1)
        db.save_run(report.results, CLASSIFIER_TAXONOMY, provenance=report.provenance)
        assert len(provider.batches) == 3
        assert db.conn.execute("SELECT COUNT(*) FROM classification_runs").fetchone()[0] == 1
        assert {row["model_id"] for row in db.records()} == {"local-model-v2"}
        columns = {row[1] for row in db.conn.execute("PRAGMA table_info(classifications)")}
        assert not {"provider_id", "model_id", "prompt_version"} & columns


@pytest.mark.anyio
async def test_mixed_batch_provenance_cannot_be_silently_saved():
    class ChangingModel(Model):
        async def classify(self, emails):
            result = await super().classify(emails)
            return BatchResponse(result.text, result.usage, provenance=PredictionProvenance(
                "local", f"model-{len(self.batches)}", CLASSIFIER_TAXONOMY.version, "classifier-v1",
            ))

    async with GmailMCPClient(create_server(SearchStore(2))) as mcp:
        with pytest.raises(ClassificationError, match="provenance changed"):
            await classify_search("", ChangingModel(), mcp, batch_size=1)


def test_historical_migration_keeps_unknown_metadata_and_old_reviews(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with HistoryDB(path) as db:
        db.save_run([prediction(subtype_hint="subscription_invoice")], run_key="legacy-run")
        row = db.records()[0]
        db.review(row["id"], "corrected", category="ADMIN",
                  subtype_action="corrected", corrected_subtype="account_notice")
        before = db.records()[0]
        run_before = dict(db.conn.execute("SELECT * FROM classification_runs").fetchone())
    with sqlite3.connect(path) as conn:
        for name in ("provider_id", "model_id", "prompt_version"):
            conn.execute(f"ALTER TABLE classification_runs DROP COLUMN {name}")
    for _ in range(2):
        with HistoryDB(path) as db:
            assert db.records()[0] == before
            assert dict(db.conn.execute("SELECT * FROM classification_runs").fetchone()) == run_before
            assert ground_truth(db.records()[0])["category"] == "ADMIN"
            assert db.records()[0]["corrected_subtype_hint"] == "account_notice"
            for name in ("provider_id", "model_id", "prompt_version"):
                assert db.records()[0][name] is None
            assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_generic_storage_and_stats_separate_all_provenance_dimensions(tmp_path, monkeypatch, capsys):
    taxonomy = load_taxonomy()
    newer_taxonomy = replace(taxonomy, version="test-taxonomy-v2")
    variants = [
        PredictionProvenance("gemini", "model-v1", taxonomy.version, "classifier-v1"),
        PredictionProvenance("gemini", "model-v2", taxonomy.version, "classifier-v1"),
        PredictionProvenance("gemini", "model-v2", taxonomy.version, "classifier-v2"),
        PredictionProvenance("gemini", "model-v2", newer_taxonomy.version, "classifier-v2"),
        PredictionProvenance("local", "model-v2", newer_taxonomy.version, "classifier-v2"),
        PredictionProvenance("openai", "other-model", newer_taxonomy.version, "other-prompt-v1"),
    ]
    path = tmp_path / "history.sqlite"
    with HistoryDB(path) as db:
        for index, provenance in enumerate(variants):
            config = taxonomy if provenance.taxonomy_version == taxonomy.version else newer_taxonomy
            db.save_run([prediction(str(index), subtype_hint="subscription_invoice")],
                        config, provenance=provenance)
        first, second, third, *_ = db.records()
        db.review(first["id"], "corrected", category="ADMIN", subtype_action="rejected")
        db.review(second["id"], "accepted", subtype_action="accepted")
        db.review(third["id"], "accepted", subtype_action="corrected", corrected_subtype="invoice_notice")
        db.save_run([prediction("unknown")])
        groups = calculate_stats(db.records())["performance_by_provenance"]
    assert len(groups) == 7
    by_key = {(g["provider_id"], g["model_id"], g["taxonomy_version"], g["prompt_version"]): g for g in groups}
    for index, provenance in enumerate(variants):
        group = by_key[(provenance.provider_id, provenance.model_id, provenance.taxonomy_version, provenance.prompt_version)]
        assert group["prediction_count"] == 1
        assert group["reviewed_count"] == int(index < 3)
        assert group["category_correction_rate"] == (1.0 if index == 0 else 0.0 if index < 3 else None)
        assert group["subtype_acceptance_rate"] == (1.0 if index == 1 else 0.0 if index < 3 else None)
        assert group["subtype_correction_rate"] == (1.0 if index == 2 else 0.0 if index < 3 else None)
        assert group["subtype_rejection_rate"] == (1.0 if index == 0 else 0.0 if index < 3 else None)
    assert by_key[(None, None, taxonomy.version, None)]["prediction_count"] == 1
    monkeypatch.setattr("sys.argv", ["stats", "--db", str(path)])
    taxonomy_stats.main()
    output = capsys.readouterr().out
    assert "performance_by_provenance" in output
    assert "All provenance groups combined" in output


def test_provider_change_preserves_shared_human_learning(tmp_path):
    taxonomy = load_taxonomy()
    with HistoryDB(tmp_path / "history.sqlite") as db:
        db.save_run([prediction("same-message", subtype_hint="subscription_invoice")],
                    provenance=PredictionProvenance("gemini", "old-model", taxonomy.version, "classifier-v1"))
        old = db.records()[0]
        db.review(old["id"], "corrected", category="ADMIN",
                  subtype_action="corrected", corrected_subtype="account_notice")
        reviewed_before = db.records()[0]
        truth_before = ground_truth(reviewed_before)
        approved_before = calculate_stats(db.records(), subtype_min_messages=1)["human_approved_subtype_candidates"]
        db.save_run([prediction("same-message", "PROFESSIONAL", subtype_hint="recruiter")],
                    provenance=PredictionProvenance("local", "new-model", taxonomy.version, "classifier-v2"))
        old, new = db.records()
        assert old == reviewed_before
        assert ground_truth(old) == truth_before
        assert ground_truth(new) is None  # A new observation does not fabricate a review.
        assert old["message_id"] == new["message_id"]
        stats = calculate_stats(db.records(), subtype_min_messages=1)
        assert stats["human_approved_subtype_candidates"] == approved_before == [
            {"category": "ADMIN", "subtype_hint": "account_notice", "distinct_messages": 1},
        ]
        assert len(stats["performance_by_provenance"]) == 2


def test_run_key_cannot_relabel_existing_provenance_or_reviews(tmp_path):
    taxonomy = load_taxonomy()
    first = PredictionProvenance("local", "model-v1", taxonomy.version, "classifier-v1")
    with HistoryDB(tmp_path / "history.sqlite") as db:
        db.save_run([prediction()], run_key="same-run", provenance=first)
        db.review(db.records()[0]["id"], "accepted")
        before = db.records()
        db.save_run([prediction()], run_key="same-run", provenance=first)
        with pytest.raises(ValueError, match="different provenance"):
            db.save_run([prediction("new")], run_key="same-run", provenance=replace(first, model_id="model-v2"))
        with pytest.raises(ValueError, match="taxonomy version"):
            db.save_run([prediction("new")], provenance=replace(first, taxonomy_version="wrong-version"))
        assert db.records() == before
        assert db.conn.execute("SELECT COUNT(*) FROM classification_runs").fetchone()[0] == 1
