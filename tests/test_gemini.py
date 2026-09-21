"""Gemini translation tests with a local SDK-shaped client; no network calls."""

import pytest
from google.genai import types

from gmail_agent.core import Message, ToolCall, ToolResult, ToolSpec
from gmail_agent.gemini import GeminiProvider


@pytest.fixture
def anyio_backend():
    return "asyncio"


class CapturingModels:
    def __init__(self):
        self.requests = []

    async def generate_content(self, **kwargs):
        self.requests.append(kwargs)
        if len(self.requests) == 1:
            part = types.Part.from_function_call(name="search_emails", args={"query": "invoice"})
            part.function_call.id = "call-123"
            part.thought_signature = b"opaque-signature-123"
        else:
            part = types.Part.from_text(text="Done")
        return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=[part]))])


class CapturingClient:
    def __init__(self):
        self.aio = type("AsyncClient", (), {})()
        self.aio.models = CapturingModels()


@pytest.mark.anyio
async def test_function_call_and_result_mapping_for_followup_request():
    client = CapturingClient()
    provider = GeminiProvider(client=client)
    tools = [ToolSpec("search_emails", "Search", {"type": "object", "properties": {"query": {"type": "string"}}})]
    prompt = Message("user", "Find invoice")
    first = await provider.complete([prompt], tools)
    assert first.tool_calls == [ToolCall("search_emails", {"query": "invoice"}, "call-123", {"thought_signature": b"opaque-signature-123"})]

    result = ToolResult(first.tool_calls[0], {"messages": [{"id": "msg-invoice"}]})
    second = await provider.complete([
        prompt,
        Message("assistant", tool_calls=first.tool_calls),
        Message("tool", tool_result=result),
    ], tools)
    assert second.text == "Done"
    contents = client.aio.models.requests[1]["contents"]
    assert [content.role for content in contents] == ["user", "model", "user"]
    assert contents[1].parts[0].function_call.name == "search_emails"
    assert contents[1].parts[0].function_call.id == "call-123"
    assert contents[1].parts[0].thought_signature == b"opaque-signature-123"
    assert contents[2].parts[0].function_response.name == "search_emails"
    assert contents[2].parts[0].function_response.id == "call-123"
    assert contents[2].parts[0].function_response.response == {"output": result.content}
    assert client.aio.models.requests[1]["config"].automatic_function_calling.disable is True


@pytest.mark.anyio
async def test_parallel_function_call_signatures_stay_with_their_parts():
    class ParallelModels(CapturingModels):
        async def generate_content(self, **kwargs):
            self.requests.append(kwargs)
            if len(self.requests) == 1:
                calls = []
                for name, call_id, signature in [
                    ("search_emails", "first", b"first-signature"),
                    ("get_email", "second", b"second-signature"),
                ]:
                    part = types.Part.from_function_call(name=name, args={})
                    part.function_call.id = call_id
                    part.thought_signature = signature
                    calls.append(part)
                return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=calls))])
            return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part.from_text(text="Done")]))])

    client = CapturingClient()
    client.aio.models = ParallelModels()
    provider = GeminiProvider(client=client)
    prompt = Message("user", "Find both")
    first = await provider.complete([prompt], [])
    await provider.complete([
        prompt,
        Message("assistant", tool_calls=first.tool_calls),
        *(Message("tool", tool_result=ToolResult(call, {})) for call in first.tool_calls),
    ], [])
    parts = client.aio.models.requests[1]["contents"][1].parts
    assert [(part.function_call.id, part.thought_signature) for part in parts] == [
        ("first", b"first-signature"),
        ("second", b"second-signature"),
    ]
