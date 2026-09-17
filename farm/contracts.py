"""双时态观察、质量放行与采收作业。

每条资料同时携带 *发生时间* (``occurred_at`` / ``sampled_at`` /
``issued_at``) 与 *录入时间* (``recorded_at``)：前者描述田间事实，后者描述
系统何时知道该事实。迟到记录可以改变后续判断，却不能改写已经作出的决定。
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class ParcelDecision(StrEnum):
    """地块级采收状态。"""

    READY = "ready"      # 可采：技术员窗口在期内且质量已放行
    WAIT = "wait"        # 等待：物候/天气/窗口等农艺条件尚未满足
    PAUSED = "paused"    # 暂停：质量否决、资料失效或领取后需重新确认


class WorkOrderState(StrEnum):
    """采收作业单状态。"""

    CLAIMED = "claimed"      # 现场已领取，尚未开工——迟到记录仍可暂停
    PAUSED = "paused"        # 领取后发生并发变更，等待重新确认
    STARTED = "started"      # 已开工：决定锁定，不再被撤回
    COMPLETED = "completed"  # 已完成：历史决定永久保留


@dataclass(frozen=True)
class Provenance:
    """种源信息。"""

    provenance_id: str
    parcel_id: str
    cultivar: str
    seed_source: str


@dataclass(frozen=True)
class FieldObservation:
    """物候或天气等田间观察。"""

    observation_id: str
    parcel_id: str
    kind: str
    value: str
    occurred_at: datetime
    recorded_at: datetime
    valid_until: datetime | None = None
    ended_at: datetime | None = None


@dataclass(frozen=True)
class InputApplication:
    """投入品（农药/肥料）施用记录，携带安全间隔期（PHI，小时）。"""

    application_id: str
    parcel_id: str
    product: str
    phi_hours: int
    occurred_at: datetime
    recorded_at: datetime


@dataclass(frozen=True)
class SampleResult:
    """抽样检测结果。

    ``parcel_ids`` 是结果可以代表的 *全部且唯一* 范围：混合样只对所列地块
    生效，绝不自动扩展到其他地块；结果仅在 ``sampled_at`` 与
    ``valid_until`` 之间有效。
    """

    sample_id: str
    parcel_ids: tuple[str, ...]
    sampled_at: datetime
    valid_until: datetime
    result_code: str
    mixed_sample: bool
    recorded_at: datetime | None = None


@dataclass(frozen=True)
class RainfallForecast:
    """逐时降雨预报；``parcel_ids`` 为空表示覆盖全部地块。"""

    forecast_id: str
    parcel_ids: tuple[str, ...]
    issued_at: datetime
    hours: tuple[tuple[datetime, float], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class WindowProposal:
    """技术员提出的采收窗口。"""

    proposal_id: str
    parcel_id: str
    window_start: datetime
    window_end: datetime
    proposed_by: str
    proposed_at: datetime
    basis_version: int
    observation_ids: tuple[str, ...]


@dataclass(frozen=True)
class HarvestDecision:
    """一个不可变的决策版本；撤回 = 追加一个 supersedes 新版本，不删除旧版。"""

    decision_id: str
    parcel_id: str
    state: ParcelDecision
    observation_ids: tuple[str, ...]
    proposed_by: str
    released_by: str | None
    version: int
    decided_at: datetime
    supersedes_id: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    basis_version: int = 0
    reasons: tuple[str, ...] = ()
    withdrawn_observation_id: str | None = None
    withdrawn_by: str | None = None


@dataclass(frozen=True)
class WorkOrder:
    """采收作业单；每次状态流转追加一条带生效时间的记录。"""

    work_order_id: str
    parcel_id: str
    decision_id: str
    state: WorkOrderState
    claimed_at: datetime
    basis_version: int
    effective_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
