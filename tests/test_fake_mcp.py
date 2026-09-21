import pytest
from mcp import Client

from fake_gmail_mcp.server import create_server
from fake_gmail_mcp.store import FakeEmailStore


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    async with Client(create_server(FakeEmailStore()), raise_exceptions=True) as connected:
        yield connected


@pytest.mark.anyio
async def test_tool_discovery(client):
    tools = await client.list_tools()
    assert {tool.name for tool in tools.tools} == {
        "search_emails", "get_email", "get_thread", "apply_label"
    }


@pytest.mark.anyio
async def test_search_returns_metadata_only(client):
    result = await client.call_tool("search_emails", {"query": "INVOICE"})
    assert not result.is_error
    assert [message["id"] for message in result.structured_content["messages"]] == ["msg-invoice"]
    assert "body" not in result.structured_content["messages"][0]


@pytest.mark.anyio
async def test_search_empty_query_lists_all(client):
    result = await client.call_tool("search_emails", {})
    assert len(result.structured_content["messages"]) == 4


@pytest.mark.anyio
async def test_get_known_email(client):
    result = await client.call_tool("get_email", {"message_id": "msg-action"})
    assert result.structured_content["subject"] == "Please review the draft"
    assert "Friday" in result.structured_content["body"]


@pytest.mark.anyio
async def test_unknown_email_is_clear_error(client):
    result = await client.call_tool("get_email", {"message_id": "missing"})
    assert result.is_error
    assert "Unknown message ID: missing" in result.content[0].text


@pytest.mark.anyio
async def test_get_thread(client):
    result = await client.call_tool("get_thread", {"thread_id": "thread-action"})
    assert [message["id"] for message in result.structured_content["messages"]] == [
        "msg-action", "msg-action-followup"
    ]


@pytest.mark.anyio
async def test_apply_label_mutates_fake_message(client):
    applied = await client.call_tool("apply_label", {"message_id": "msg-invoice", "label": "TO_REVIEW"})
    assert applied.structured_content == {
        "message_id": "msg-invoice", "label": "TO_REVIEW", "added": True,
        "labels": ["INBOX", "TO_REVIEW"],
    }
    fetched = await client.call_tool("get_email", {"message_id": "msg-invoice"})
    assert fetched.structured_content["labels"] == ["INBOX", "TO_REVIEW"]
    duplicate = await client.call_tool("apply_label", {"message_id": "msg-invoice", "label": "TO_REVIEW"})
    assert duplicate.structured_content["added"] is False


@pytest.mark.anyio
async def test_apply_label_validates_inputs(client):
    for arguments, expected in [
        ({"message_id": "missing", "label": "TO_REVIEW"}, "Unknown message ID"),
        ({"message_id": "msg-invoice", "label": " "}, "label must"),
    ]:
        result = await client.call_tool("apply_label", arguments)
        assert result.is_error
        assert expected in result.content[0].text


def test_reset_and_instance_isolation():
    first = FakeEmailStore()
    second = FakeEmailStore()
    first.apply_label("msg-invoice", "TO_REVIEW")
    assert second.get_email("msg-invoice")["labels"] == ["INBOX"]
    first.reset()
    assert first.get_email("msg-invoice")["labels"] == ["INBOX"]
