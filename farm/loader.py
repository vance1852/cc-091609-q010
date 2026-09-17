"""夹具加载：把 JSON 事件流装入双时态台账。

兼容 fixtures/harvest_window.json 的精简格式（仅含 observations / workOrders），
也支持 products、samples、forecasts、provenance、rules 等扩展字段。

每条观察同时保留 occurredAt（发生时间）与 recordedAt（录入时间）；
缺省 recordedAt 时视为及时录入（等于发生时间）。
"""

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from .contracts import FieldObservation, SampleResult
from .ledger import Ledger
from .models import (
    InputProduct,
    Parcel,
    Provenance,
    RainHour,
    RainfallForecast,
    Rules,
    WorkOrder,
    WorkOrderState,
)

_DEFAULT_PHI_HOURS = 168  # 未指明投入品时的保守安全间隔（7 天）


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def load_fixture(
    path: str | Path, *, materialize_orders: bool = True
) -> tuple[Ledger, Rules, dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rules = _load_rules(data.get("rules", {}))
    ledger = Ledger()

    crop = data.get("crop", "黄芩")

    for prov in data.get("provenance", []):
        ledger.register_provenance(
            Provenance(
                provenance_id=prov["id"],
                cultivar=prov.get("cultivar", ""),
                seed_batch=prov.get("seedBatch", ""),
            )
        )

    for i, raw in enumerate(data.get("parcels", [])):
        if isinstance(raw, str):
            ledger.register_parcel(Parcel(parcel_id=raw, crop=crop, provenance_id=None))
        else:
            ledger.register_parcel(
                Parcel(
                    parcel_id=raw["id"],
                    crop=raw.get("crop", crop),
                    provenance_id=raw.get("provenanceId"),
                    area_mu=raw.get("areaMu"),
                )
            )

    for prod in data.get("products", []):
        ledger.register_product(
            InputProduct(
                code=prod["code"],
                name=prod.get("name", prod["code"]),
                phi_hours=prod.get("phiHours", _DEFAULT_PHI_HOURS),
            )
        )

    for i, raw in enumerate(data.get("observations", [])):
        occurred = _parse(raw["occurredAt"])
        recorded = _parse(raw["recordedAt"]) if raw.get("recordedAt") else occurred
        kind = raw["kind"]
        value = raw.get("value")
        if value is None:
            if kind in ("flowering-stage", "phenology"):
                value = rules.mature_stage
            elif kind in ("heavy-rain", "rainfall"):
                value = str(rules.heavy_rain_mm)
            else:
                value = "GENERIC-INPUT"
        obs = FieldObservation(
            observation_id=raw.get("id") or f"OBS-{i + 1:03d}",
            parcel_id=raw["parcel"],
            kind=kind,
            value=str(value),
            occurred_at=occurred,
            recorded_at=recorded,
            valid_until=_parse(raw["validUntil"]) if raw.get("validUntil") else None,
        )
        ledger.add_observation(obs)

    for i, raw in enumerate(data.get("samples", [])):
        ledger.add_sample(
            SampleResult(
                sample_id=raw.get("id") or f"SMP-{i + 1:03d}",
                parcel_ids=tuple(raw["parcels"]),
                sampled_at=_parse(raw["sampledAt"]),
                valid_until=_parse(raw["validUntil"]),
                result_code=raw.get("result", "pass"),
                mixed_sample=raw.get("mixed", len(raw["parcels"]) > 1),
            )
        )

    for raw in data.get("forecasts", []):
        ledger.publish_forecast(
            RainfallForecast(
                issued_at=_parse(raw["issuedAt"]),
                hours=tuple(
                    RainHour(at=_parse(h["at"]), mm=float(h["mm"]))
                    for h in raw.get("hours", [])
                ),
                parcel_ids=tuple(raw["parcelIds"]) if raw.get("parcelIds") else None,
            )
        )

    if materialize_orders:
        _materialize_work_orders(ledger, rules, data.get("workOrders", []))
        _reconcile_orders(ledger, rules)

    return ledger, rules, data


def _reconcile_orders(ledger: Ledger, rules: Rules) -> None:
    """作业单重建后，以台账最新录入时点复算，处理迟到资料导致的并发变更。"""
    from .service import HarvestService

    latest = _now(ledger)
    HarvestService(ledger, rules).sync_all(latest)


def _materialize_work_orders(ledger: Ledger, rules: Rules, raw_orders: list) -> None:
    """重建夹具中的作业单。

    夹具只给出地块与状态（如 claimed），其含义是现场在迟到资料暴露**之前**
    已凭当时的有效放行领取。因此放行决策以该地块最早一条补录记录的
    ``recorded_at`` 前一分钟为评估时点复算，保证双时态一致。
    """
    from .service import HarvestService

    svc = HarvestService(ledger, rules)
    for i, raw in enumerate(raw_orders):
        parcel_id = raw["parcel"]
        order_id = raw.get("id") or f"WO-{parcel_id.upper()}-{i + 1:02d}"
        state = raw.get("state", "claimed")

        if raw.get("claimedAt"):
            claim_at = _parse(raw["claimedAt"])
        else:
            late = [
                o.recorded_at
                for o in ledger._observations  # noqa: SLF001
                if o.parcel_id == parcel_id and o.recorded_at > o.occurred_at
            ]
            claim_at = (min(late) - timedelta(minutes=1)) if late else _now(ledger)

        svc.propose_window(parcel_id, raw.get("proposedBy", "fixture-tech"), claim_at)
        svc.release(parcel_id, raw.get("releasedBy", "fixture-qa"), claim_at)
        svc.create_order(order_id, parcel_id, claim_at)
        if state == "released":
            continue
        svc.claim_order(order_id, claim_at)
        if state in ("in_progress", "completed"):
            start_at = _parse(raw["startedAt"]) if raw.get("startedAt") else claim_at
            svc.start_order(order_id, start_at)
        if state == "completed":
            end_at = _parse(raw["completedAt"]) if raw.get("completedAt") else start_at
            svc.complete_order(order_id, end_at)
        if state == "awaiting_reconfirm":
            order = ledger.orders[order_id]
            ledger.update_order(
                replace(order, state=WorkOrderState.AWAITING_RECONFIRM)
            )


def _now(ledger: Ledger) -> datetime:
    times = [o.recorded_at for o in ledger._observations]  # noqa: SLF001
    times += [f.issued_at for f in ledger._forecasts]
    return max(times) if times else datetime.now().astimezone()


def _load_rules(raw: dict) -> Rules:
    mapping = {
        "matureStage": "mature_stage",
        "heavyRainMm": "heavy_rain_mm",
        "rainBandMm": "rain_band_mm",
        "drydownHours": "drydown_hours",
        "preRainLeadHours": "pre_rain_lead_hours",
    }
    kwargs = {mapping[k]: v for k, v in raw.items() if k in mapping}
    return Rules(**kwargs)
