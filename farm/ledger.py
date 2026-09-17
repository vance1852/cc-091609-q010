"""双时态台账：观察按 occurred_at / recorded_at 双轴保存，决策只追加、不改写。"""

from datetime import datetime

from .contracts import FieldObservation, HarvestDecision, SampleResult
from .models import (
    Advisory,
    InputProduct,
    Parcel,
    Provenance,
    RainfallForecast,
    WorkOrder,
)


class Ledger:
    """保存全部地块数据的双时态存储器。

    所有查询都接受 ``as_of``：只有 ``recorded_at <= as_of`` 的资料才可见，
    因此可以复算历史时点“当时知道什么”。
    """

    def __init__(self) -> None:
        self.parcels: dict[str, Parcel] = {}
        self.provenance: dict[str, Provenance] = {}
        self.products: dict[str, InputProduct] = {}
        self._observations: list[FieldObservation] = []
        self._samples: list[SampleResult] = []
        self._forecasts: list[RainfallForecast] = []
        self._decisions: dict[str, list[HarvestDecision]] = {}
        self.notes: dict[str, "object"] = {}
        self.orders: dict[str, WorkOrder] = {}
        self.advisories: list[Advisory] = []

    # -- 注册 -------------------------------------------------------------

    def register_provenance(self, provenance: Provenance) -> None:
        self.provenance[provenance.provenance_id] = provenance

    def register_parcel(self, parcel: Parcel) -> None:
        self.parcels[parcel.parcel_id] = parcel

    def register_product(self, product: InputProduct) -> None:
        self.products[product.code] = product

    # -- 观察录入（append-only） ------------------------------------------

    def add_observation(self, observation: FieldObservation) -> None:
        self._observations.append(observation)

    def add_sample(self, sample: SampleResult) -> None:
        self._samples.append(sample)

    def publish_forecast(self, forecast: RainfallForecast) -> None:
        self._forecasts.append(forecast)
        self._forecasts.sort(key=lambda f: f.issued_at)

    def append_decision(self, decision: HarvestDecision, note: object) -> HarvestDecision:
        chain = self._decisions.setdefault(decision.parcel_id, [])
        chain.append(decision)
        self.notes[decision.decision_id] = note
        return decision

    def update_order(self, order: WorkOrder) -> None:
        self.orders[order.order_id] = order

    def add_advisory(self, advisory: Advisory) -> None:
        self.advisories.append(advisory)

    # -- 双时态查询 --------------------------------------------------------

    def observations(
        self, parcel_id: str, as_of: datetime, kinds: tuple[str, ...] | None = None
    ) -> list[FieldObservation]:
        rows = [
            o
            for o in self._observations
            if o.parcel_id == parcel_id
            and o.recorded_at <= as_of
            and (kinds is None or o.kind in kinds)
        ]
        return sorted(rows, key=lambda o: (o.occurred_at, o.recorded_at))

    def latest_phenology(
        self, parcel_id: str, as_of: datetime
    ) -> FieldObservation | None:
        rows = self.observations(parcel_id, as_of, ("flowering-stage", "phenology"))
        return rows[-1] if rows else None

    def applications(self, parcel_id: str, as_of: datetime) -> list[FieldObservation]:
        return self.observations(parcel_id, as_of, ("field-input",))

    def rain_events(self, parcel_id: str, as_of: datetime) -> list[FieldObservation]:
        return self.observations(parcel_id, as_of, ("heavy-rain", "rainfall"))

    def effective_sample(
        self, parcel_id: str, at: datetime, as_of: datetime
    ) -> SampleResult | None:
        """返回该时点对该地块有效的抽样结果。

        混合样只对 ``parcel_ids`` 中列明的地块有效，不会自动扩展到其他地块。
        """
        candidates = [
            s
            for s in self._samples
            if parcel_id in s.parcel_ids
            and s.sampled_at <= at < s.valid_until
            and s.sampled_at <= as_of
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda s: s.sampled_at)
        return candidates[-1]

    def latest_forecast(
        self, parcel_id: str, as_of: datetime
    ) -> RainfallForecast | None:
        issued = [f.issued_at for f in self._forecasts if f.issued_at <= as_of]
        if not issued:
            return None
        cutoff = issued[-1]
        for forecast in reversed(self._forecasts):
            if forecast.issued_at != cutoff:
                    continue
            if forecast.parcel_ids is None or parcel_id in forecast.parcel_ids:
                return forecast
        return None

    # -- 决策链 ------------------------------------------------------------

    def decision_chain(self, parcel_id: str) -> list[HarvestDecision]:
        return list(self._decisions.get(parcel_id, []))

    def current_decision(self, parcel_id: str) -> HarvestDecision | None:
        chain = self._decisions.get(parcel_id)
        return chain[-1] if chain else None

    def next_version(self, parcel_id: str) -> int:
        chain = self._decisions.get(parcel_id, [])
        return len(chain) + 1

    def orders_for(self, parcel_id: str) -> list[WorkOrder]:
        return sorted(
            (o for o in self.orders.values() if o.parcel_id == parcel_id),
            key=lambda o: o.order_id,
        )

    def advisories_for(self, parcel_id: str) -> list[Advisory]:
        return [a for a in self.advisories if a.parcel_id == parcel_id]

    def known_times(self, parcel_id: str, as_of: datetime) -> list[datetime]:
        """该地块在 as_of 前所有资料的录入/发布时间（用于展示知识截止）。"""
        times = [o.recorded_at for o in self.observations(parcel_id, as_of)]
        forecast = self.latest_forecast(parcel_id, as_of)
        if forecast is not None:
            times.append(forecast.issued_at)
        for s in self._samples:
            if parcel_id in s.parcel_ids and s.sampled_at <= as_of:
                times.append(s.sampled_at)
        return times
