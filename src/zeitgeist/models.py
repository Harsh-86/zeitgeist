"""Domain models flowing through the pipeline as JSON Kafka messages."""

import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class GdeltEvent:
    """One parsed GDELT 2.0 event row (the `raw.events` payload)."""

    event_id: str
    occurred_on: str
    actor1_code: str | None
    actor1_name: str | None
    actor2_code: str | None
    actor2_name: str | None
    event_code: str
    event_root_code: str
    quad_class: int
    goldstein: float | None
    num_mentions: int
    avg_tone: float | None
    geo_name: str | None
    geo_lat: float | None
    geo_lon: float | None
    observed_at: str
    source_url: str | None

    def to_json(self) -> bytes:
        return json.dumps(asdict(self)).encode()

    @classmethod
    def from_json(cls, raw: bytes) -> "GdeltEvent":
        return cls(**json.loads(raw))


@dataclass(frozen=True)
class Claim:
    """A typed (subject, relation, object) claim (the `extracted.claims` payload)."""

    subject: str
    relation: str
    object: str | None
    event_id: str
    event_code: str
    quad_class: int
    goldstein: float | None
    tone: float | None
    num_mentions: int
    occurred_on: str
    observed_at: str
    geo_name: str | None
    geo_lat: float | None
    geo_lon: float | None
    source_url: str | None
    confidence: float
    tier: str = "rules"
    detail: str | None = None

    def to_json(self) -> bytes:
        return json.dumps(asdict(self)).encode()

    @classmethod
    def from_json(cls, raw: bytes) -> "Claim":
        data = json.loads(raw)
        defaults = {"tier": "rules", "detail": None}
        return cls(**{**defaults, **data})
