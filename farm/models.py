"""平台使用的领域模型：种源、地块、投入品、降雨预报、作业单与解释结构。

``contracts`` 中的双时态观察与决策契约保持不变，本模块只做扩展。
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from .contracts import HarvestDecision, ParcelDecision


class WorkOrderState(StrEnum):
    RELEASED = "released"          # 质量已放行，现场尚未领取
    CLAIMED = "claimed"           # 现场已领取，作业未开始
    IN_PROGRESS = "in_progress"   # 作业已开始
    COMPLETED = "completed"       # 作业已执行完毕（历史不可抹除）
    AWAITING_RECONFIRM = "awaiting_reconfirm"  # 领取后情况变化，等待重新确认
    CANCELLED = "cancelled"       # 未领取即失效


@dataclass(frozen=True)
class Provenance:
    provenance_id: str
    cultivar: str
    seed_batch: str


@dataclass(frozen=True)
class Parcel:
    parcel_id: str
    crop: str
    provenance_id: str | None
    area_mu: float | None = None


@dataclass(frozen=True)
class InputProduct:
    code: str
    name: str
    phi_hours: int  # 安全间隔（收获前禁止时间，小时）


@dataclass(frozen=True)
class RainHour:
    at: datetime
    mm: float


@dataclass(frozen=True)
class RainfallForecast:
    """一次逐时降雨预报发版；新版本会缩短窗口，但不覆盖质量否决。"""

    issued_at: datetime
    hours: tuple[RainHour, ...]
    parcel_ids: tuple[str, ...] | None = None  # None 表示覆盖基地全部地块

    @property
    def ref(self) -> str:
        return f"forecast@{self.issued_at.isoformat()}"


@dataclass(frozen=True)
class Rules:
    mature_stage: str = "fruit-mature"      # 达到该物候期才进入采收窗口
    heavy_rain_mm: float = 25.0             # 单次降雨达到该量级判为暴雨
    rain_band_mm: float = 8.0               # 逐时预报达到该雨强需避让
    drydown_hours: int = 48                 # 暴雨后沥水时间
    pre_rain_lead_hours: int = 6            # 降雨带到来前预留作业时间


@dataclass(frozen=True)
class WorkOrder:
    order_id: str
    parcel_id: str
    state: WorkOrderState
    claimed_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    confirmed_decision_id: str | None = None  # 领取/确认时锁定的决策版本


@dataclass(frozen=True)
class Reason:
    code: str
    blocking: bool
    message: str
    ref: str | None = None


@dataclass(frozen=True)
class Evidence:
    """决策所采用的一条数据版本。"""

    ref: str
    kind: str                 # observation | sample | forecast
    occurred_at: datetime
    recorded_at: datetime
    title: str


@dataclass(frozen=True)
class Recommendation:
    parcel_id: str
    state: ParcelDecision
    window_start: datetime | None
    window_end: datetime | None
    reasons: tuple[Reason, ...]
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class DecisionNote:
    """决策附带的窗口与理由快照（HarvestDecision 契约之外的台账注解）。"""

    window_start: datetime | None
    window_end: datetime | None
    reasons: tuple[Reason, ...]


@dataclass(frozen=True)
class Advisory:
    """作业已执行后才暴露的问题：只能追加警示/隔离建议，不能改写历史。"""

    advisory_id: str
    parcel_id: str
    order_id: str
    kind: str
    message: str
    observation_refs: tuple[str, ...]
    created_at: datetime


@dataclass(frozen=True)
class ParcelView:
    parcel: Parcel
    state: ParcelDecision
    as_of: datetime
    knowledge_cutoff: datetime
    recommendation: Recommendation
    work_order: WorkOrder | None
    window_start: datetime | None
    window_end: datetime | None
    decision_chain: tuple[HarvestDecision, ...]
    advisories: tuple[Advisory, ...]
    notes: dict[str, DecisionNote] = field(default_factory=dict)


class DomainError(Exception):
    """领域规则冲突。"""


class QualityVeto(DomainError):
    """质量否决：安全间隔或检测状态不支持放行。"""

    def __init__(self, parcel_id: str, reasons: tuple[Reason, ...]):
        self.parcel_id = parcel_id
        self.reasons = reasons
        super().__init__(
            f"{parcel_id} 放行被否决：" + "；".join(r.message for r in reasons)
        )


class NotReady(DomainError):
    """地块当前不处于可采状态（物候、沥水或天气窗口等非质量原因）。"""

    def __init__(self, parcel_id: str, reasons: tuple[Reason, ...]):
        self.parcel_id = parcel_id
        self.reasons = reasons
        super().__init__(
            f"{parcel_id} 当前不可采：" + "；".join(r.message for r in reasons)
        )
