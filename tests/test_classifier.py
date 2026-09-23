"""Batch classifier tests; all Gmail and model responses are local."""

import json

import pytest
from google.genai import types

from fake_gmail_mcp.server import create_server
from gmail_agent.classifier import (
    BATCH_SIZE, CATEGORIES, BatchResponse, ClassificationError, Usage,
    _compact, classify_search, parse_results,
)
from gmail_agent.gemini_classifier import GeminiBatchClassifier
from gmail_agent.mcp_client import GmailMCPClient


@pytest.fixture
def anyio_backend():
    return "asyncio"


class SearchOnlyStore:
    def __init__(self, count=10):
        self.count = count
        self.searches = []

    def search_emails(self, query):
        self.searches.append(query)
        return [
            {"id": f"m-{index}", "thread_id": f"t-{index}", "sender": "a@example.test",
             "subject": f"Subject {index}", "labels": ["INBOX"], "date": "today",
             "snippet": "s" * 1000, "irrelevant": "do not send"}
            for index in range(self.count)
        ]

    def get_email(self, message_id):
        raise AssertionError("Full email fetch is forbidden")

    def get_thread(self, thread_id):
        raise AssertionError("Thread fetch is forbidden")

    def apply_label(self, message_id, label):
        raise AssertionError("Gmail mutation is forbidden")


class FakeBatchProvider:
    def __init__(self):
        self.batches = []

    async def classify(self, emails):
        self.batches.append(emails)
        results = [
            {"index": email["index"], "category": CATEGORIES[email["index"] % len(CATEGORIES)],
             "confidence": 0.8, "needs_reply": False, "importance": "normal", "deadline": None, "subtype_hint": None, "short_reason": "Brief reason"}
            for email in emails
        ]
        return BatchResponse(json.dumps({"results": results}), Usage("fake-model", 12, 8, 20))


@pytest.mark.anyio
async def test_ten_emails_one_batch_all_categories_and_no_mutation(monkeypatch):
    monkeypatch.setenv("GMAIL_LABEL_WRITES", "1")
    store = SearchOnlyStore()
    provider = FakeBatchProvider()
    async with GmailMCPClient(create_server(store)) as mcp:
        report = await classify_search("recent", provider, mcp)
    assert BATCH_SIZE == 10
    assert store.searches == ["recent"]
    assert len(provider.batches) == 1
    assert len(provider.batches[0]) == 10
    assert set(provider.batches[0][0]) == {"index", "sender", "subject"}
    assert all("m-" not in json.dumps(email) for email in provider.batches[0])
    assert [result["category"] for result in report.results] == list(CATEGORIES[:10])
    assert [result["message_id"] for result in report.results] == [f"m-{index}" for index in range(10)]
    assert report.results[0]["review_metadata"] == {
        "sender": "a@example.test", "subject": "Subject 0",
    }
    assert report.request_count == 1
    assert report.batch_sizes == [10]
    assert report.usage == [Usage("fake-model", 12, 8, 20)]


@pytest.mark.anyio
async def test_batch_size_splits_requests_and_tracks_usage():
    provider = FakeBatchProvider()
    async with GmailMCPClient(create_server(SearchOnlyStore())) as mcp:
        report = await classify_search("", provider, mcp, batch_size=4, max_emails=9, snippet_chars=15)
    assert [len(batch) for batch in provider.batches] == [4, 4, 1]
    assert report.batch_sizes == [4, 4, 1]
    assert report.request_count == 3
    assert all("snippet" not in email for batch in provider.batches for email in batch)


def test_compact_real_metadata_truncates_snippet_and_omits_irrelevant_fields():
    compact = _compact({
        "id": "m-0", "sender": "sender", "subject": "subject", "date": "today",
        "snippet": "s" * 1000, "body": "do not send", "labels": ["INBOX"],
    }, 15)
    assert compact == {"sender": "sender", "subject": "subject", "date": "today", "snippet": "s" * 15}


def test_result_validation_rejects_malformed_incomplete_and_wrong_indices():
    good = {"index": 0, "category": "UNCERTAIN", "confidence": 0.5,
            "needs_reply": False, "importance": "normal", "deadline": None, "subtype_hint": None, "short_reason": "Not enough context"}
    assert parse_results(json.dumps({"results": [good]}), ["m-0"]) == [
        {"message_id": "m-0", **{key: value for key, value in good.items() if key != "index"}}
    ]
    for text in [
        "not json", json.dumps({"results": [{**good, "category": "WRONG"}]}),
        json.dumps({"results": [{key: value for key, value in good.items() if key != "needs_reply"}]}),
        json.dumps({"results": [{**good, "index": 1}]}),
        json.dumps({"results": [good, good]}),
        json.dumps({"results": [{**good, "confidence": 1.5}]}),
    ]:
        with pytest.raises(ClassificationError):
            parse_results(text, ["m-0"])


def test_all_taxonomy_categories_are_valid_classification_results():
    for category in CATEGORIES:
        item = {"index": 0, "category": category, "confidence": 0.5,
                "needs_reply": False, "importance": "normal", "deadline": None, "subtype_hint": None, "short_reason": "Brief"}
        assert parse_results(json.dumps({"results": [item]}), ["m-0"])[0]["category"] == category


def test_reordered_indices_remap_to_original_message_ids():
    base = {"category": "UNCERTAIN", "confidence": 0.5, "needs_reply": False, "importance": "normal",
            "deadline": None, "subtype_hint": None, "short_reason": "Brief"}
    results = [{"index": 2, **base}, {"index": 0, **base}, {"index": 1, **base}]
    mapped = parse_results(json.dumps({"results": results}), ["opaque-A", "opaque-B", "opaque-C"])
    assert [item["message_id"] for item in mapped] == ["opaque-A", "opaque-B", "opaque-C"]
    assert all("index" not in item for item in mapped)


@pytest.mark.parametrize("indices", [[0, 0, 2], [0, 2], [0, 1, 3]])
def test_duplicate_missing_or_unknown_index_rejected(indices):
    results = [{"index": index, "category": "UNCERTAIN", "confidence": 0.5,
                "needs_reply": False, "importance": "normal", "deadline": None, "subtype_hint": None, "short_reason": "Brief"} for index in indices]
    with pytest.raises(ClassificationError, match="indices"):
        parse_results(json.dumps({"results": results}), ["opaque-A", "opaque-B", "opaque-C"])


class CapturingModels:
    def __init__(self):
        self.requests = []

    async def generate_content(self, **kwargs):
        self.requests.append(kwargs)
        return types.GenerateContentResponse(
            candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part.from_text(text='{"results":[]}')]))],
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=15, candidates_token_count=7, total_token_count=25,
            ),
        )


@pytest.mark.anyio
async def test_gemini_structured_output_and_usage_mapping_without_network():
    client = type("Client", (), {})()
    client.aio = type("Async", (), {})()
    client.aio.models = CapturingModels()
    response = await GeminiBatchClassifier(client).classify([{"index": 0, "subject": "Test"}])
    request = client.aio.models.requests[0]
    assert len(client.aio.models.requests) == 1
    assert request["config"].response_mime_type == "application/json"
    assert request["config"].response_json_schema["properties"]["results"]["type"] == "array"
    assert request["config"].tools is None
    assert request["config"].automatic_function_calling.disable is True
    assert request["config"].thinking_config.thinking_level == types.ThinkingLevel.MINIMAL
    assert "subtype_hint" in request["contents"]
    assert "lowercase snake_case" in request["contents"]
    assert "maximum 40 characters" in request["contents"]
    assert "not a Gmail label" in request["contents"]
    assert "subtype_hint" in request["config"].response_json_schema["properties"]["results"]["items"]["required"]
    assert "message_id" not in request["contents"]
    assert response.usage == Usage("gemini-3.6-flash", 15, 7, 25)
