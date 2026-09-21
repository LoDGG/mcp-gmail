import logging

import pytest

from fake_gmail_mcp.server import create_server
from fake_gmail_mcp.store import FakeEmailStore
from gmail_agent.agent import AgentError, SafetyLimitError, run_agent
from gmail_agent.core import ModelResponse, ToolCall
from gmail_agent.mcp_client import GmailMCPClient


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeLLMProvider:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.seen = []

    async def complete(self, messages, tools):
        self.seen.append((messages, tools))
        return next(self.responses)


def request(name, **arguments):
    return ModelResponse(tool_calls=[ToolCall(name, arguments)])


@pytest.mark.anyio
async def test_successful_cycle_search_get_label_and_final(caplog):
    store = FakeEmailStore()
    fake = FakeLLMProvider([
        request("search_emails", query="invoice"),
        request("get_email", message_id="msg-invoice"),
        request("apply_label", message_id="msg-invoice", label="TO_REVIEW"),
        ModelResponse("Invoice labeled for review."),
    ])
    async with GmailMCPClient(create_server(store)) as mcp:
        with caplog.at_level(logging.INFO, logger="gmail_agent.agent"):
            answer = await run_agent("Find and label the invoice", fake, mcp)
    assert answer == "Invoice labeled for review."
    assert store.get_email("msg-invoice")["labels"] == ["INBOX", "TO_REVIEW"]
    assert fake.seen[1][0][-1].tool_result.content["messages"][0]["id"] == "msg-invoice"
    assert fake.seen[2][0][-1].tool_result.content["body"]
    assert fake.seen[3][0][-1].tool_result.content["added"] is True
    assert len(fake.seen) == 4
    assert "model completion" in caplog.text
    assert "invoice" not in caplog.text.lower().split("arguments=")[-1]


@pytest.mark.anyio
async def test_unauthorized_tool_rejected_before_call():
    fake = FakeLLMProvider([request("delete_email", message_id="msg-invoice")])
    async with GmailMCPClient(create_server(FakeEmailStore())) as mcp:
        with pytest.raises(AgentError, match="Unauthorized"):
            await run_agent("Delete", fake, mcp)


@pytest.mark.anyio
async def test_invalid_arguments_rejected_before_call():
    fake = FakeLLMProvider([request("get_email", wrong="id")])
    async with GmailMCPClient(create_server(FakeEmailStore())) as mcp:
        with pytest.raises(AgentError, match="Invalid arguments"):
            await run_agent("Get", fake, mcp)


@pytest.mark.anyio
async def test_max_tool_calls_enforced():
    fake = FakeLLMProvider([ModelResponse(tool_calls=[ToolCall("search_emails", {}) for _ in range(4)])])
    async with GmailMCPClient(create_server(FakeEmailStore())) as mcp:
        with pytest.raises(SafetyLimitError, match="Maximum tool calls"):
            await run_agent("Keep searching", fake, mcp)
    assert len(fake.seen) == 1


@pytest.mark.anyio
async def test_max_iterations_enforced():
    fake = FakeLLMProvider([
        request("search_emails"), request("get_email", message_id="msg-action"),
        request("get_thread", thread_id="thread-action"), request("search_emails"),
    ])
    async with GmailMCPClient(create_server(FakeEmailStore())) as mcp:
        with pytest.raises(SafetyLimitError):
            await run_agent("Keep going", fake, mcp)
    assert len(fake.seen) == 4
