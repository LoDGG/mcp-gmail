"""Versioned local taxonomy configuration."""

import json
from dataclasses import dataclass
from pathlib import Path

from gmail_agent.gmail_labels import validate_user_label

DEFAULT_TAXONOMY_PATH = Path(__file__).resolve().parents[2] / "config" / "taxonomy.json"
IMPORTANCE_VALUES = ("low", "normal", "high")


@dataclass(frozen=True)
class Taxonomy:
    version: str
    target_primary_categories: str
    max_primary_categories: int
    categories: tuple[dict, ...]

    @property
    def active_names(self) -> tuple[str, ...]:
        return tuple(item["name"] for item in self.categories if item["active"])


def load_taxonomy(path: Path = DEFAULT_TAXONOMY_PATH) -> Taxonomy:
    return taxonomy_from_data(json.loads(Path(path).read_text(encoding="utf-8")))


def taxonomy_from_data(data: dict) -> Taxonomy:
    categories = tuple(data["categories"])
    required = {"name", "definition", "positive_guidance", "negative_guidance", "active"}
    if not all(required <= item.keys() for item in categories):
        raise ValueError("Taxonomy category is incomplete")
    for category in categories:
        if "gmail_label" in category:
            validate_user_label(category["gmail_label"])
    names = [item["name"] for item in categories]
    if len(names) != len(set(names)):
        raise ValueError("Taxonomy category names must be unique")
    taxonomy = Taxonomy(data["version"], data["target_primary_categories"], data["max_primary_categories"], categories)
    if len(taxonomy.active_names) > taxonomy.max_primary_categories or "UNCERTAIN" not in taxonomy.active_names:
        raise ValueError("Taxonomy active categories exceed cap or omit UNCERTAIN")
    return taxonomy
