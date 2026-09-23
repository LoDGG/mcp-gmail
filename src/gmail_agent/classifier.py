"""Separate, dry-run batch classification workflow."""

import json
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from jsonschema import ValidationError, validate

from gmail_agent.core import ToolCall
from gmail_agent.subtypes import SUBTYPE_HINT_SCHEMA, validate_subtype_hint
from gmail_agent.mcp_client import GmailMCPClient
from gmail_agent.taxonomy import IMPORTANCE_VALUES, load_taxonomy

BATCH_SIZE = 10
SNIPPET_CHARS = 200
CATEGORIES = load_taxonomy().active_names

CLASSIFICATION_SCHEMA = {
    "type": "object",
    "properties": {"results": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "index": {"type": "integer", "minimum": 0},
                "category": {"type": "string", "enum": list(CATEGORIES)},
                "subtype_hint": SUBTYPE_HINT_SCHEMA,
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "needs_reply": {"type": "boolean"},
                "importance": {"type": "string", "enum": list(IMPORTANCE_VALUES)},
                "deadline": {"type": ["string", "null"]},
                "short_reason": {"type": "string"},
            },
            "required": ["index", "category", "subtype_hint", "confidence", "needs_reply", "importance", "deadline", "short_reason"],
            "additionalProperties": False,
        },
    }},
    "required": ["results"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Usage:
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    thinking_tokens: int | None = None


@dataclass(frozen=True)
class BatchResponse:
    text: str
    usage: Usage
    request_count: int = 1


class BatchClassifierProvider(Protocol):
    async def classify(self, emails: list[dict[str, str | int]]) -> BatchResponse: ...


@dataclass(frozen=True)
class BatchReport:
    results: list[dict[str, Any]]
    usage: list[Usage]
    batch_sizes: list[int]
    request_count: int
    emails_found: int = 0


class ClassificationError(ValueError):
    pass


def parse_results(text: str, expected_ids: list[str]) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(text)
        validate(parsed, CLASSIFICATION_SCHEMA)
        for item in parsed["results"]:
            validate_subtype_hint(item["subtype_hint"], item["category"])
    except (ValueError, ValidationError) as exc:
        raise ClassificationError("Invalid classification response") from exc
    results = parsed["results"]
    indices = [item["index"] for item in results]
    if len(indices) != len(expected_ids) or set(indices) != set(range(len(expected_ids))):
        raise ClassificationError("Classification indices do not match the batch")
    if any(len(item["short_reason"]) > 120 or (item["deadline"] is not None and len(item["deadline"]) > 80) for item in results):
        raise ClassificationError("Classification explanation is too long")
    by_index = {item["index"]: item for item in results}
    return [
        {"message_id": message_id, **{key: value for key, value in by_index[index].items() if key != "index"}}
        for index, message_id in enumerate(expected_ids)
    ]


def _compact(message: dict[str, Any], snippet_chars: int) -> dict[str, str]:
    compact = {"sender": message.get("sender", ""), "subject": message.get("subject", "")}
    for key in ("date", "snippet"):
        value = message.get(key)
        if isinstance(value, str) and value:
            compact[key] = value[:snippet_chars] if key == "snippet" else value
    return compact


async def classify_search(
    query: str, provider: BatchClassifierProvider, mcp: GmailMCPClient,
    *, batch_size: int = BATCH_SIZE, max_emails: int = 20, snippet_chars: int = SNIPPET_CHARS,
    before_classify: Callable[[], None] | None = None,
) -> BatchReport:
    if batch_size < 1 or max_emails < 1 or snippet_chars < 1:
        raise ValueError("batch_size, max_emails, and snippet_chars must be positive")
    search = await mcp.call(ToolCall("search_emails", {"query": query}))
    if search.is_error:
        raise ClassificationError("Email search failed")
    messages = search.content.get("messages")
    if not isinstance(messages, list):
        raise ClassificationError("Email search returned no message list")
    emails = messages[:max_emails]
    if emails and before_classify is not None:
        before_classify()
    all_results: list[dict[str, Any]] = []
    usage: list[Usage] = []
    batch_sizes: list[int] = []
    request_count = 0
    for start in range(0, len(emails), batch_size):
        batch = emails[start:start + batch_size]
        model_batch = [{"index": index, **_compact(email, snippet_chars)} for index, email in enumerate(batch)]
        response = await provider.classify(model_batch)
        results = parse_results(response.text, [email["id"] for email in batch])
        for result, email in zip(results, batch):
            # Review context comes from Gmail search metadata, never model output.
            result["review_metadata"] = {
                key: email[key] for key in ("date", "sender", "subject")
                if isinstance(email.get(key), str)
            }
        all_results.extend(results)
        request_count += response.request_count
        usage.append(response.usage)
        batch_sizes.append(len(batch))
    return BatchReport(all_results, usage, batch_sizes, request_count, len(messages))
