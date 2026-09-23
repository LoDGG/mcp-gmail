"""Explicit, provider-neutral classification provenance."""

from dataclasses import dataclass


@dataclass(frozen=True)
class PredictionProvenance:
    provider_id: str | None = None
    model_id: str | None = None
    taxonomy_version: str | None = None
    prompt_version: str | None = None

    def __post_init__(self) -> None:
        for value in (self.provider_id, self.model_id, self.taxonomy_version, self.prompt_version):
            if value is not None and (not isinstance(value, str) or not value.strip() or value != value.strip()):
                raise ValueError("Provenance identifiers must be nonempty strings or null")
