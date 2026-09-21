"""Gemini mapping for the provider-neutral contract."""

import os

from google import genai
from google.genai import types

from gmail_agent.core import Message, ModelResponse, ToolCall, ToolSpec


class GeminiProvider:
    def __init__(self, client: genai.Client | None = None) -> None:
        self.model = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
        if client is None:
            key = os.environ.get("GEMINI_API_KEY")
            if not key:
                raise ValueError("GEMINI_API_KEY is required")
            client = genai.Client(api_key=key)
        self.client = client

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> ModelResponse:
        contents = []
        for message in messages:
            if message.role == "user":
                contents.append(types.Content(role="user", parts=[types.Part.from_text(text=message.text)]))
            elif message.role == "assistant":
                parts = ([types.Part.from_text(text=message.text)] if message.text else [])
                for call in message.tool_calls:
                    part = types.Part.from_function_call(name=call.name, args=call.arguments)
                    part.function_call.id = call.id
                    if "thought_signature" in call.provider_metadata:
                        part.thought_signature = call.provider_metadata["thought_signature"]
                    parts.append(part)
                contents.append(types.Content(role="model", parts=parts))
            elif message.role == "tool" and message.tool_result:
                result = message.tool_result
                payload = {"error": result.content.get("error", "Tool failed")} if result.is_error else {"output": result.content}
                part = types.Part.from_function_response(name=result.call.name, response=payload)
                part.function_response.id = result.call.id
                contents.append(types.Content(role="user", parts=[part]))
            else:
                raise ValueError("Invalid conversation message")
        declarations = [types.FunctionDeclaration(name=tool.name, description=tool.description, parameters_json_schema=tool.input_schema) for tool in tools]
        config = types.GenerateContentConfig(
            tools=[types.Tool(function_declarations=declarations)] if declarations else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        response = await self.client.aio.models.generate_content(model=self.model, contents=contents, config=config)
        if not response.candidates or not response.candidates[0].content:
            raise ValueError("Gemini returned no candidate content")
        parts = response.candidates[0].content.parts or []
        text = "".join(part.text or "" for part in parts if part.text)
        calls = [
            ToolCall(
                part.function_call.name,
                dict(part.function_call.args or {}),
                part.function_call.id,
                {"thought_signature": part.thought_signature} if part.thought_signature is not None else {},
            )
            for part in parts if part.function_call
        ]
        return ModelResponse(text, calls)
