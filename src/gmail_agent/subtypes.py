"""Validation for non-binding model subtype hints."""

import re

SUBTYPE_HINT_SCHEMA = {
    "type": ["string", "null"],
    "maxLength": 40,
    "pattern": "^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$",
}


def validate_subtype_hint(hint: str | None, category: str) -> None:
    if hint is None:
        return
    if (
        not isinstance(hint, str)
        or len(hint) > 40
        or re.fullmatch(SUBTYPE_HINT_SCHEMA["pattern"], hint) is None
        or hint == category.lower()
    ):
        raise ValueError("Invalid subtype_hint")
