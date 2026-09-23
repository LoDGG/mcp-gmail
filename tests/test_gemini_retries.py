"""Bounded Gemini retries with fake SDK responses, Gmail, and sleep."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.genai import errors, types

from fake_gmail_mcp.server import create_server
from gmail_agent.classifier import ClassificationError, classify_search
from gmail_agent.gemini_classifier import GeminiBatchClassifier
from gmail_agent.history import HistoryDB
from gmail_agent.mcp_client import GmailMCPClient
from gmail_agent import triage_cli
from test_triage import Labels, SearchStore


@pytest.fixture
def anyio_backend():
    return "asyncio"


def api_error(code, message="Temporary provider failure"):
    cls = errors.ClientError if code < 500 else errors.ServerError
    return cls(code, {"error": {"code": code, "message": message}})


def response(text=None):
    if text is None:
        text = json.dumps({"results": [{
            "index": 0, "category": "FINANCE", "confidence": 0.95,
            "needs_reply": False, "importance": "normal", "deadline": None,
            "short_reason": "Brief",
        }]})
    return types.GenerateContentResponse(candidates=[
        types.Candidate(content=types.Content(role="model", parts=[
            types.Part.from_text(text=text),
        ])),
    ])


def provider_for(outcomes):
    generate = AsyncMock(side_effect=outcomes)
    sleep = AsyncMock()
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
    return GeminiBatchClassifier(client, sleep=sleep), generate, sleep


@pytest.mark.anyio
@pytest.mark.parametrize("code", [None, 429, 500, 502, 503, 504])
async def test_success_counts_requests_and_retries(code):
    outcomes = [response()] if code is None else [api_error(code), response()]
    provider, generate, sleep = provider_for(outcomes)
    async with GmailMCPClient(create_server(SearchStore(1))) as mcp:
        report = await classify_search("", provider, mcp)
    attempts = 1 if code is None else 2
    assert report.request_count == provider.request_count == generate.await_count == attempts
    assert provider.retry_count == attempts - 1
    assert [call.args[0] for call in sleep.await_args_list] == ([] if code is None else [20])
    for call in generate.await_args_list:
        assert call.kwargs["config"].http_options.retry_options.attempts == 1
    if code is not None:
        assert generate.await_args_list[0] == generate.await_args_list[1]


@pytest.mark.anyio
async def test_repeated_503_stops_after_three_attempts():
    failure = api_error(503)
    provider, generate, sleep = provider_for([failure] * 4)
    with pytest.raises(errors.ServerError) as caught:
        await provider.classify([])
    assert caught.value is failure
    assert generate.await_count == provider.request_count == 3
    assert provider.retry_count == 2
    assert [call.args[0] for call in sleep.await_args_list] == [20, 60]


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [
    api_error(400, "Invalid request"),
    api_error(400, "API key not valid"),
    api_error(401, "Unauthenticated"),
    api_error(403, "Permission denied"),
    api_error(408), api_error(501),
    ValueError("Application validation failed"),
    RuntimeError("503 is only text, not a provider status"),
])
async def test_non_transient_errors_are_not_retried(failure):
    provider, generate, sleep = provider_for([failure])
    with pytest.raises(type(failure)) as caught:
        await provider.classify([])
    assert caught.value is failure
    assert generate.await_count == provider.request_count == 1
    assert provider.retry_count == 0
    sleep.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("text", ["not json", '{"results":[]}', '{"results":[{"index":0}]}'])
async def test_malformed_or_invalid_output_is_not_retried(text):
    provider, generate, sleep = provider_for([response(text)])
    async with GmailMCPClient(create_server(SearchStore(1))) as mcp:
        with pytest.raises(ClassificationError):
            await classify_search("", provider, mcp)
    assert generate.await_count == provider.request_count == 1
    assert provider.retry_count == 0
    sleep.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("exhausted", [False, True])
async def test_triage_cli_counters_and_no_message_mutation_on_exhaustion(
    exhausted, tmp_path, monkeypatch, capsys,
):
    outcomes = [api_error(503)] * 3 if exhausted else [response()]
    provider, generate, sleep = provider_for(outcomes)
    labels = Labels()
    store = SearchStore(1)
    db_path = tmp_path / "history.sqlite"
    monkeypatch.setenv("GMAIL_BACKEND", "real")
    monkeypatch.setenv("GMAIL_LABEL_WRITES", "1")
    monkeypatch.setattr("sys.argv", ["triage", "--apply", "--db", str(db_path)])
    monkeypatch.setattr(triage_cli, "GeminiBatchClassifier", lambda: provider)
    monkeypatch.setattr(triage_cli, "RealGmailBackend", lambda **kwargs: labels)
    monkeypatch.setattr(triage_cli, "create_real_server", lambda *args, **kwargs: create_server(store))
    if exhausted:
        # The uncaught exception makes asyncio.run(main()) exit unsuccessfully.
        with pytest.RaisesGroup(errors.ServerError, flatten_subgroups=True):
            await triage_cli.main()
        assert labels.calls == []
        assert labels.applied == {}
        assert labels.creates == []
        with HistoryDB(db_path) as db:
            assert db.records() == []
        assert "Gemini requests=3 retries=2" in capsys.readouterr().out
        assert generate.await_count == 3
        assert [call.args[0] for call in sleep.await_args_list] == [20, 60]
    else:
        await triage_cli.main()
        assert labels.calls == [("m-0", "Billing"), ("m-0", "Processed")]
        assert "Gemini requests=1 retries=0" in capsys.readouterr().out
        sleep.assert_not_awaited()
