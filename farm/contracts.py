"""双时态观察、质量放行与采收作业。"""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum


class ParcelDecision(StrEnum):
    READY = "ready"
    WAIT = "wait"
    PAUSED = "paused"


@dataclass(frozen=True)
class FieldObservation:
    observation_id: str
    parcel_id: str
    kind: str
    value: str
    occurred_at: datetime
    recorded_at: datetime
    valid_until: datetime | None = None


@dataclass(frozen=True)
class SampleResult:
    sample_id: str
    parcel_ids: tuple[str, ...]
    sampled_at: datetime
    valid_until: datetime
    result_code: str
    mixed_sample: bool


@dataclass(frozen=True)
class HarvestDecision:
    decision_id: str
    parcel_id: str
    state: ParcelDecision
    observation_ids: tuple[str, ...]
    proposed_by: str
    released_by: str | None
    version: int
    decided_at: datetime
    supersedes_id: str | None = None
