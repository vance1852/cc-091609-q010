"""把 JSON 夹具装载为领域对象。"""

import json
from datetime import datetime
from pathlib import Path

from .contracts import (
    FieldObservation,
    InputApplication,
    Provenance,
    RainfallForecast,
    SampleResult,
    WindowProposal,
    WorkOrder,
    WorkOrderState,
)

_PHI_DEFAULT_HOURS = 336  # 未显式标注时按多菌灵登记安全间隔 14 天计


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def load_fixture(path: str | Path) -> dict:
    """读取夹具并返回领域对象集合。"""

    data = json.loads(Path(path).read_text(encoding="utf-8"))

    provenance = [
        Provenance(
            provenance_id=item["provenanceId"],
            parcel_id=item["parcel"],
            cultivar=item["cultivar"],
            seed_source=item["seedSource"],
        )
        for item in data.get("provenance", [])
    ]

    observations: list[FieldObservation] = []
    applications: list[InputApplication] = []
    for index, item in enumerate(data.get("observations", []), start=1):
        occurred = _dt(item["occurredAt"])
        recorded = _dt(item.get("recordedAt")) or occurred
        obs_id = item.get("observationId") or f"obs-{index}"
        if item["kind"] == "field-input":
            applications.append(
                InputApplication(
                    application_id=obs_id,
                    parcel_id=item["parcel"],
                    product=item.get("product", "未登记投入品"),
                    phi_hours=int(item.get("phiHours", _PHI_DEFAULT_HOURS)),
                    occurred_at=occurred,
                    recorded_at=recorded,
                )
            )
        observations.append(
            FieldObservation(
                observation_id=obs_id,
                parcel_id=item["parcel"],
                kind=item["kind"],
                value=item.get("value", ""),
                occurred_at=occurred,
                recorded_at=recorded,
                valid_until=_dt(item.get("validUntil")),
                ended_at=_dt(item.get("endedAt")),
            )
        )

    samples = [
        SampleResult(
            sample_id=item["sampleId"],
            parcel_ids=tuple(item["parcels"]),
            sampled_at=_dt(item["sampledAt"]),
            valid_until=_dt(item["validUntil"]),
            result_code=item["resultCode"],
            mixed_sample=bool(item.get("mixed", False)),
            recorded_at=_dt(item.get("recordedAt")),
        )
        for item in data.get("samples", [])
    ]

    forecasts = [
        RainfallForecast(
            forecast_id=item["forecastId"],
            parcel_ids=tuple(item.get("parcels", [])),
            issued_at=_dt(item["issuedAt"]),
            hours=tuple((_dt(clock), float(mm)) for clock, mm in item["hours"]),
        )
        for item in data.get("rainfallForecasts", [])
    ]

    proposals = [
        WindowProposal(
            proposal_id=item["proposalId"],
            parcel_id=item["parcel"],
            window_start=_dt(item["windowStart"]),
            window_end=_dt(item["windowEnd"]),
            proposed_by=item["proposedBy"],
            proposed_at=_dt(item["proposedAt"]),
            basis_version=0,
            observation_ids=tuple(item.get("observationIds", ())),
        )
        for item in data.get("proposals", [])
    ]

    work_orders = [
        WorkOrder(
            work_order_id=item.get("workOrderId", f"wo-{item['parcel']}"),
            parcel_id=item["parcel"],
            decision_id=item.get("decisionId", ""),
            state=WorkOrderState(item["state"]),
            claimed_at=_dt(item.get("claimedAt", data["asOf"])),
            basis_version=int(item.get("basisVersion", 0)),
            effective_at=_dt(item.get("claimedAt", data["asOf"])),
            started_at=_dt(item.get("startedAt")),
            completed_at=_dt(item.get("completedAt")),
        )
        for item in data.get("workOrders", [])
    ]

    return {
        "crop": data["crop"],
        "as_of": _dt(data["asOf"]),
        "parcels": tuple(data["parcels"]),
        "provenance": provenance,
        "observations": observations,
        "applications": applications,
        "samples": samples,
        "forecasts": forecasts,
        "proposals": proposals,
        "work_orders": work_orders,
        "timeline": tuple(
            {
                "type": event["type"],
                "parcel": event["parcel"],
                "by": event.get("by", ""),
                "at": _dt(event["at"]),
                "decision_id": event.get("decisionId"),
                "due_to_observation": event.get("dueToObservation"),
            }
            for event in data.get("timeline", [])
        ),
    }
