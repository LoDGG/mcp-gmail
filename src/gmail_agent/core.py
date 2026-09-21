"""Small provider-neutral conversation contract."""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]
    id: str | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class ToolResult:
    call: ToolCall
    content: dict[str, Any]
    is_error: bool = False


@dataclass(frozen=True)
class Message:
    role: str  # user, assistant, or tool
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_result: ToolResult | None = None


@dataclass(frozen=True)
class ModelResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


class LLMProvider(Protocol):
    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> ModelResponse: ...
