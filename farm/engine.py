"""地块级采收窗口评估。

评估是纯函数：给定台账、地块与时点，只采用 ``recorded_at <= as_of`` 的资料，
输出 可采(ready) / 等待(wait) / 暂停(paused) 及完整理由与证据版本。
"""

from datetime import datetime, timedelta

from .contracts import ParcelDecision, SampleResult
from .ledger import Ledger
from .models import Evidence, Reason, Recommendation, Rules

DEFAULT_INPUT_CODE = "GENERIC-INPUT"


def evaluate(
    ledger: Ledger, parcel_id: str, as_of: datetime, rules: Rules | None = None
) -> Recommendation:
    rules = rules or Rules()
    reasons: list[Reason] = []
    evidence: list[Evidence] = []
    blocking = False

    # ---- 1. 物候 -------------------------------------------------------
    phen = ledger.latest_phenology(parcel_id, as_of)
    if phen is None:
        reasons.append(
            Reason("phenology-missing", False, "尚无物候观察，无法判断成熟度", None)
        )
    else:
        evidence.append(
            Evidence(
                phen.observation_id,
                "observation",
                phen.occurred_at,
                phen.recorded_at,
                f"物候观察：{phen.value}",
            )
        )
        if phen.value != rules.mature_stage:
            reasons.append(
                Reason(
                    "not-mature",
                    False,
                    f"当前物候期 {phen.value}，未达 {rules.mature_stage}",
                    phen.observation_id,
                )
            )

    # ---- 2. 投入品安全间隔（质量否决项）---------------------------------
    window_start = as_of
    phi_end: datetime | None = None
    for app in ledger.applications(parcel_id, as_of):
        product_code = app.value or DEFAULT_INPUT_CODE
        product = ledger.products.get(product_code)
        if product is None:
            # 未登记投入品：安全间隔未知，按质量阻断处理，需质量人员核定
            blocking = True
            reasons.append(
                Reason(
                    "input-unregistered",
                    True,
                    f"投入品 {product_code} 未登记，安全间隔无法确认，需质量核定",
                    app.observation_id,
                )
            )
            evidence.append(
                Evidence(
                    app.observation_id,
                    "observation",
                    app.occurred_at,
                    app.recorded_at,
                    f"投入品 {product_code}（未登记）"
                    + ("【补录】" if app.recorded_at > app.occurred_at else ""),
                )
            )
            continue
        phi_hours = product.phi_hours
        end = app.occurred_at + timedelta(hours=phi_hours)
        evidence.append(
            Evidence(
                app.observation_id,
                "observation",
                app.occurred_at,
                app.recorded_at,
                f"投入品 {product_code}（安全间隔 {phi_hours}h）"
                + ("【补录】" if app.recorded_at > app.occurred_at else ""),
            )
        )
        if end > as_of:
            blocking = True
            reasons.append(
                Reason(
                    "phi-active",
                    True,
                    f"{product_code} 安全间隔至 {end.isoformat()} 结束",
                    app.observation_id,
                )
            )
        phi_end = end if phi_end is None else max(phi_end, end)
    if phi_end and phi_end > window_start:
        window_start = phi_end

    # ---- 3. 抽样检测（只代表列明地块与有效期；混合样不自动扩展）------------
    sample = _latest_known_sample(ledger, parcel_id, as_of)
    if sample is None:
        blocking = True
        reasons.append(
            Reason("sample-missing", True, "无在有效期内的抽样检测结果，质量不予放行")
        )
    else:
        evidence.append(
            Evidence(
                sample.sample_id,
                "sample",
                sample.sampled_at,
                sample.sampled_at,
                ("混合样" if sample.mixed_sample else "单地块样")
                + f"：{sample.result_code}，有效期至 {sample.valid_until.isoformat()}"
                + f"（覆盖地块 {','.join(sample.parcel_ids)}）",
            )
        )
        if sample.result_code == "fail":
            blocking = True
            reasons.append(
                Reason(
                    "sample-failed",
                    True,
                    f"检测不合格（{sample.sample_id}）",
                    sample.sample_id,
                )
            )
        elif sample.result_code == "pending":
            reasons.append(
                Reason(
                    "sample-pending",
                    False,
                    f"样品 {sample.sample_id} 结果未出",
                    sample.sample_id,
                )
            )
        elif as_of >= sample.valid_until:
            blocking = True
            reasons.append(
                Reason(
                    "sample-expired",
                    True,
                    f"检测结果已于 {sample.valid_until.isoformat()} 过期，需重新抽样",
                    sample.sample_id,
                )
            )

    # ---- 4. 已发生暴雨后的沥水期 ----------------------------------------
    dry_end: datetime | None = None
    for rain in ledger.rain_events(parcel_id, as_of):
        try:
            mm = float(rain.value)
        except ValueError:
            mm = rules.heavy_rain_mm
        evidence.append(
            Evidence(
                rain.observation_id,
                "observation",
                rain.occurred_at,
                rain.recorded_at,
                f"降雨记录：{mm}mm",
            )
        )
        if mm >= rules.heavy_rain_mm and rain.occurred_at <= as_of:
            end = rain.occurred_at + timedelta(hours=rules.drydown_hours)
            dry_end = end if dry_end is None else max(dry_end, end)
    if dry_end and dry_end > as_of:
        reasons.append(
            Reason(
                "rain-drydown",
                False,
                f"暴雨后需沥水至 {dry_end.isoformat()}",
            )
        )
        if dry_end > window_start:
            window_start = dry_end

    # ---- 5. 最新一版逐时降雨预报（只能缩短窗口）--------------------------
    forecast = ledger.latest_forecast(parcel_id, as_of)
    window_end: datetime | None = None
    if forecast is not None:
        future_band = [
            h
            for h in forecast.hours
            if h.at > as_of and h.mm >= rules.rain_band_mm
        ]
        evidence.append(
            Evidence(
                forecast.ref,
                "forecast",
                forecast.issued_at,
                forecast.issued_at,
                f"逐时降雨预报（{len(forecast.hours)} 个时次"
                + (f"，未来 {len(future_band)} 个时次达雨强 {rules.rain_band_mm}mm/h）" if future_band else "，未来无显著雨带）"),
            )
        )
        if future_band:
            band_start = min(h.at for h in future_band)
            window_end = band_start - timedelta(hours=rules.pre_rain_lead_hours)
            reasons.append(
                Reason(
                    "rain-forecast-band",
                    False,
                    f"预报雨带 {band_start.isoformat()} 到来，需提前 "
                    f"{rules.pre_rain_lead_hours}h 结束作业",
                    forecast.ref,
                )
            )

    # ---- 6. 汇总（质量阻断永远优先于天气/物候）----------------------------
    if blocking:
        state = ParcelDecision.PAUSED
    elif window_start > as_of:
        state = ParcelDecision.WAIT
    elif window_end is not None and window_end <= as_of:
        state = ParcelDecision.WAIT
        reasons.append(
            Reason("window-closed", False, "预报更新后当前已无可作业时段，等待下一版预报")
        )
    elif any(r.code in ("not-mature", "phenology-missing", "sample-pending") for r in reasons):
        state = ParcelDecision.WAIT
    else:
        state = ParcelDecision.READY

    if not blocking and window_end is not None and window_start > as_of and window_end <= window_start:
        # 窗口被降雨压缩到与沥水/成熟时间冲突：等待，而不是放宽任何质量约束
        state = ParcelDecision.WAIT
        reasons.append(
            Reason("window-closed", False, "可行开始时间已晚于雨带前撤离时限，本窗口关闭")
        )

    return Recommendation(
        parcel_id=parcel_id,
        state=state,
        window_start=window_start if window_start > as_of else as_of,
        window_end=window_end,
        reasons=tuple(reasons),
        evidence=tuple(evidence),
    )


def _latest_known_sample(
    ledger: Ledger, parcel_id: str, as_of: datetime
) -> SampleResult | None:
    known = [
        s
        for s in ledger._samples  # noqa: SLF001 - 引擎与台账同包
        if parcel_id in s.parcel_ids and s.sampled_at <= as_of
    ]
    if not known:
        return None
    known.sort(key=lambda s: s.sampled_at)
    return known[-1]


def signature(rec: Recommendation) -> tuple:
    """用于并发变更检测的决策指纹。"""
    return (
        str(rec.state),
        rec.window_start.isoformat() if rec.window_start else None,
        rec.window_end.isoformat() if rec.window_end else None,
        tuple(sorted((r.code, r.ref or "") for r in rec.reasons if r.blocking)),
        tuple(sorted(e.ref for e in rec.evidence)),
    )
