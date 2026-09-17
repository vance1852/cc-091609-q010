"""采收窗口决策引擎。

双时态原则
==========
* 发生时间 (occurred_at / sampled_at / issued_at) 描述田间事实；
* 录入时间 (recorded_at) 描述系统何时获知事实；
* 评估时刻 ``t`` 只能使用 ``recorded_at <= t`` 的资料（迟到资料对当时不可见）；
* 决策不可变：放行被撤回时 *追加* 一条 ``PAUSED`` 新版本（supersedes），
  旧版本永久保留；作业一旦 STARTED，迟到记录不能抹去已执行的决定。

地块级门禁（质量否决优先于农艺判断）
====================================
1. 质量否决 -> PAUSED：安全间隔未结束，或没有在有效期内、范围精确覆盖
   本地块的合格检测（混合样只代表所列地块，不自动扩展）；
2. 并发变更 -> PAUSED：作业单领取后数据版本变化，必须现场重新确认；
3. 农艺窗口 -> WAIT/READY：物候成熟、雨后脱水、逐时预报只能 *收缩*
   窗口，不能推翻质量结论；窗口关闭即 WAIT。
"""

from dataclasses import dataclass, field
from datetime import timedelta

from .contracts import (
    FieldObservation,
    HarvestDecision,
    InputApplication,
    ParcelDecision,
    RainfallForecast,
    SampleResult,
    WindowProposal,
    WorkOrder,
    WorkOrderState,
)

RAIN_THRESHOLD_MM = 5.0  # 逐时雨量达到该值视为不可作业
DRYING_HOURS = 24       # 暴雨后地块脱水等待时长


@dataclass
class Evaluation:
    """某地块在时刻 ``at`` 的评估结果（不写账本）。"""

    parcel_id: str
    at: object
    state: ParcelDecision
    version: int
    reasons: list[str] = field(default_factory=list)
    basis_observation_ids: list[str] = field(default_factory=list)
    window_start: object = None
    window_end: object = None
    covering_sample_id: str | None = None
    blocking_application_id: str | None = None
    gates: dict = field(default_factory=dict)


class HarvestLedger:
    """只追加的双时态账本：事实、决策、作业单状态均以追加表达。"""

    def __init__(self, parcels, provenance=()):
        self.parcels = tuple(parcels)
        self.provenance = tuple(provenance)
        self.observations: list[FieldObservation] = []
        self.applications: list[InputApplication] = []
        self.samples: list[SampleResult] = []
        self.forecasts: list[RainfallForecast] = []
        self.proposals: list[WindowProposal] = []
        self.decisions: list[HarvestDecision] = []
        self.work_orders: list[WorkOrder] = []
        self.reconfirmations: list[dict] = []

    # ------------------------------------------------------------- 事实录入

    def ingest_observation(self, obs: FieldObservation):
        self.observations.append(obs)
        self._bump((obs.parcel_id,), obs.recorded_at)

    def ingest_application(self, app: InputApplication):
        self.applications.append(app)
        # 迟到施用：暂停尚未开始的作业，但不能撤回已执行的决定。
        self._bump((app.parcel_id,), app.recorded_at)

    def ingest_sample(self, sample: SampleResult):
        self.samples.append(sample)
        self._bump(tuple(sample.parcel_ids), sample.recorded_at or sample.sampled_at)

    def ingest_forecast(self, forecast: RainfallForecast):
        self.forecasts.append(forecast)
        affected = tuple(forecast.parcel_ids) or self.parcels
        self._bump(tuple(affected), forecast.issued_at)

    def ingest_proposal(self, proposal: WindowProposal):
        self.proposals.append(proposal)
        self._bump((proposal.parcel_id,), proposal.proposed_at)

    def _bump(self, affected, at):
        self._reconcile_claims(set(affected), at)

    def parcel_version(self, parcel, at):
        """数据版本：截至 ``at`` 与本地块相关、且已录入可见的事实条数。

        物候/施用/提案按地块计数；检测按其代表范围计数（混合样对所列每个
        地块计一次，对范围外地块不计）；全局预报对所有地块计数。
        """
        return (
            sum(
                1
                for o in self.observations
                if o.parcel_id == parcel and o.recorded_at <= at
            )
            + sum(
                1
                for a in self.applications
                if a.parcel_id == parcel and a.recorded_at <= at
            )
            + sum(
                1
                for s in self.samples
                if parcel in s.parcel_ids and (s.recorded_at or s.sampled_at) <= at
            )
            + sum(
                1
                for f in self.forecasts
                if f.issued_at <= at and (not f.parcel_ids or parcel in f.parcel_ids)
            )
            + sum(
                1
                for p in self.proposals
                if p.parcel_id == parcel and p.proposed_at <= at
            )
        )

    def _reconcile_claims(self, affected, at):
        """领取后发生并发变更：未开工的作业单转入 PAUSED，等待重新确认。"""
        for parcel in affected:
            wo = self.latest_work_order(parcel, at)
            if wo is None or wo.state != WorkOrderState.CLAIMED:
                continue  # 无作业单，或已 STARTED/COMPLETED：决定不被迟到资料抹去
            if wo.basis_version >= self.parcel_version(parcel, at):
                continue
            self.work_orders.append(
                WorkOrder(
                    work_order_id=wo.work_order_id,
                    parcel_id=wo.parcel_id,
                    decision_id=wo.decision_id,
                    state=WorkOrderState.PAUSED,
                    claimed_at=wo.claimed_at,
                    basis_version=wo.basis_version,
                    effective_at=at,
                )
            )
            self.reconfirmations.append(
                {
                    "work_order_id": wo.work_order_id,
                    "parcel_id": wo.parcel_id,
                    "from_version": wo.basis_version,
                    "to_version": self.parcel_version(parcel, at),
                    "paused_at": at,
                }
            )

    # ------------------------------------------------------------- 决策写入

    def parcel_decisions(self, parcel):
        return [d for d in self.decisions if d.parcel_id == parcel]

    def latest_decision(self, parcel):
        rows = self.parcel_decisions(parcel)
        return rows[-1] if rows else None

    def release(self, parcel, by, at, decision_id=None):
        """质量人员放行：评估为否决时如实留下 PAUSED 记录。"""
        ev = self.evaluate(parcel, at)
        previous = self.latest_decision(parcel)
        decision = HarvestDecision(
            decision_id=decision_id
            or f"dec-{parcel}-{len(self.parcel_decisions(parcel)) + 1}",
            parcel_id=parcel,
            state=ev.state,
            observation_ids=tuple(ev.basis_observation_ids),
            proposed_by=self._proposer(parcel),
            released_by=by,
            version=len(self.parcel_decisions(parcel)) + 1,
            decided_at=at,
            supersedes_id=previous.decision_id if previous else None,
            window_start=ev.window_start,
            window_end=ev.window_end,
            basis_version=ev.version,
            reasons=tuple(ev.reasons),
        )
        self.decisions.append(decision)
        return decision

    def withdraw(self, parcel, by, at, due_to_observation_id, decision_id=None):
        """质量撤回：追加 PAUSED 新版本；被撤回的放行原样保留在审计链中。"""
        ev = self.evaluate(parcel, at)
        previous = self.latest_decision(parcel)
        decision = HarvestDecision(
            decision_id=decision_id
            or f"dec-{parcel}-{len(self.parcel_decisions(parcel)) + 1}",
            parcel_id=parcel,
            state=ParcelDecision.PAUSED,
            observation_ids=tuple(
                dict.fromkeys((due_to_observation_id, *ev.basis_observation_ids))
            ),
            proposed_by=previous.proposed_by if previous else self._proposer(parcel),
            released_by=by,
            version=len(self.parcel_decisions(parcel)) + 1,
            decided_at=at,
            supersedes_id=previous.decision_id if previous else None,
            window_start=ev.window_start,
            window_end=ev.window_end,
            basis_version=ev.version,
            reasons=tuple(
                [f"放行被撤回：迟到记录 {due_to_observation_id} 触发质量复核", *ev.reasons]
            ),
            withdrawn_observation_id=due_to_observation_id,
            withdrawn_by=by,
        )
        self.decisions.append(decision)
        return decision

    def _proposer(self, parcel):
        rows = [p for p in self.proposals if p.parcel_id == parcel]
        return sorted(rows, key=lambda p: p.proposed_at)[-1].proposed_by if rows else ""

    # ------------------------------------------------------------- 作业单

    def claim(self, parcel, at, work_order_id):
        decision = self.latest_decision(parcel)
        wo = WorkOrder(
            work_order_id=work_order_id,
            parcel_id=parcel,
            decision_id=decision.decision_id if decision else "",
            state=WorkOrderState.CLAIMED,
            claimed_at=at,
            basis_version=self.parcel_version(parcel, at),
            effective_at=at,
        )
        self.work_orders.append(wo)
        return wo

    def _transition(self, work_order_id, state, at):
        latest = next(
            w for w in reversed(self.work_orders) if w.work_order_id == work_order_id
        )
        self.work_orders.append(
            WorkOrder(
                work_order_id=latest.work_order_id,
                parcel_id=latest.parcel_id,
                decision_id=latest.decision_id,
                state=state,
                claimed_at=latest.claimed_at,
                basis_version=latest.basis_version,
                effective_at=at,
                started_at=at if state == WorkOrderState.STARTED else latest.started_at,
                completed_at=at
                if state == WorkOrderState.COMPLETED
                else latest.completed_at,
            )
        )
        return self.work_orders[-1]

    def start_work_order(self, work_order_id, at):
        return self._transition(work_order_id, WorkOrderState.STARTED, at)

    def complete_work_order(self, work_order_id, at):
        return self._transition(work_order_id, WorkOrderState.COMPLETED, at)

    def reconfirm(self, parcel, by, at):
        """现场重新确认：按当前数据版本重评；通过则恢复作业单，不另立放行决定。

        原放行（或其后的最新决定）继续有效，作业单从 PAUSED 恢复为 CLAIMED，
        依据版本锚定到当前版本；未通过则维持暂停。
        """
        wo = self.latest_work_order(parcel, at)
        if wo is None or wo.state != WorkOrderState.PAUSED:
            raise ValueError("只有 PAUSED 的作业单可以重新确认")
        # 重评时忽略作业单自身状态，只回答“当前证据能否继续作业”。
        ev = self.evaluate(parcel, at, include_work_order=False)
        if ev.state != ParcelDecision.READY:
            raise ValueError("重新确认未通过质量/农艺门禁，作业继续暂停")
        decision = self.latest_decision(parcel)
        self.work_orders.append(
            WorkOrder(
                work_order_id=wo.work_order_id,
                parcel_id=parcel,
                decision_id=decision.decision_id if decision else wo.decision_id,
                state=WorkOrderState.CLAIMED,
                claimed_at=wo.claimed_at,
                basis_version=ev.version,
                effective_at=at,
            )
        )
        self.reconfirmations.append(
            {
                "work_order_id": wo.work_order_id,
                "parcel_id": parcel,
                "from_version": wo.basis_version,
                "to_version": ev.version,
                "reconfirmed_by": by,
                "reconfirmed_at": at,
            }
        )
        return self.work_orders[-1]

    def latest_work_orders(self, at=None):
        """每个地块在时刻 ``at`` 生效的最新作业单状态（未来状态不可见）。"""
        latest = {}
        for wo in self.work_orders:
            if at is not None and wo.effective_at is not None and wo.effective_at > at:
                continue
            latest[wo.parcel_id] = wo
        return latest

    def latest_work_order(self, parcel, at=None):
        return self.latest_work_orders(at).get(parcel)

    # ------------------------------------------------------------- 可见集

    @staticmethod
    def _visible(items, at, key):
        return [item for item in items if key(item) <= at]

    def _visible_proposals(self, parcel, at):
        return sorted(
            (
                p
                for p in self.proposals
                if p.parcel_id == parcel and p.proposed_at <= at
            ),
            key=lambda p: p.proposed_at,
        )

    def _latest_forecast(self, parcel, at):
        rows = [
            f
            for f in self.forecasts
            if f.issued_at <= at and (not f.parcel_ids or parcel in f.parcel_ids)
        ]
        return sorted(rows, key=lambda f: f.issued_at)[-1] if rows else None

    # ------------------------------------------------------------- 评估

    def evaluate(self, parcel, at, include_work_order=True):
        """按时刻 ``at`` *可见* 的数据版本评估单个地块。"""
        reasons: list[str] = []
        basis: list[str] = []

        observations = self._visible(self.observations, at, lambda o: o.recorded_at)
        applications = self._visible(self.applications, at, lambda a: a.recorded_at)
        samples = self._visible(
            self.samples, at, lambda s: s.recorded_at or s.sampled_at
        )
        forecasts = self._visible(self.forecasts, at, lambda f: f.issued_at)
        proposals = self._visible_proposals(parcel, at)
        # 数据版本 = 截至 at 与本地块相关、且已录入可见的事实条数。
        version = self.parcel_version(parcel, at)

        ev = Evaluation(
            parcel_id=parcel, at=at, state=ParcelDecision.READY, version=version
        )

        # ---- 物候 ------------------------------------------------------
        phenology = [
            o
            for o in observations
            if o.parcel_id == parcel and o.kind == "flowering-stage"
        ]
        mature = bool(phenology)
        if phenology:
            basis.append(phenology[-1].observation_id)
        ev.gates["phenology"] = {"mature": mature}
        if not mature:
            reasons.append("物候未达到果熟期采收成熟标志")

        # ---- 质量门禁 1：安全间隔 --------------------------------------
        phi_blocks = []
        for app in applications:
            if app.parcel_id != parcel:
                continue
            clear_at = app.occurred_at + timedelta(hours=app.phi_hours)
            if clear_at > at:
                phi_blocks.append((app, clear_at))
        if phi_blocks:
            app, clear_at = phi_blocks[-1]
            ev.blocking_application_id = app.application_id
            basis.append(app.application_id)
            tag = "补录的迟到施用记录" if app.recorded_at > app.occurred_at else "施用记录"
            reasons.append(
                f"{tag}：{app.product} 安全间隔 {app.phi_hours}h 未结束，"
                f"须等到 {clear_at:%Y-%m-%d %H:%M}"
            )
        ev.gates["phi"] = {
            "blocked": bool(phi_blocks),
            "blocking_application_id": ev.blocking_application_id,
        }

        # ---- 质量门禁 2：检测范围与有效期（混合样不扩展） ---------------
        covering = [
            s
            for s in samples
            if parcel in s.parcel_ids
            and s.sampled_at <= at <= s.valid_until
            and s.result_code == "pass"
        ]
        ignored_mixed = [
            s.sample_id
            for s in samples
            if s.mixed_sample and parcel not in s.parcel_ids
        ]
        if covering:
            # 优先引用本地块专属样；混合样只作兜底（两者范围都合法覆盖）。
            dedicated = [s for s in covering if not s.mixed_sample]
            sample = dedicated[-1] if dedicated else covering[-1]
            ev.covering_sample_id = sample.sample_id
            basis.append(sample.sample_id)
        else:
            reasons.append("无在有效期内、范围精确覆盖本地块的合格检测结果")
        ev.gates["sample"] = {
            "covered": bool(covering),
            "sample_id": ev.covering_sample_id,
            "mixed_samples_ignored": ignored_mixed,
        }
        quality_blocked = bool(phi_blocks) or not covering

        # ---- 农艺窗口：提案 + 雨后脱水 + 预报收缩 -----------------------
        wait_agronomy = not mature
        win_start = win_end = None
        proposal = proposals[-1] if proposals else None
        if proposal is None:
            reasons.append("技术员尚未提出采收窗口")
            wait_agronomy = True
        else:
            win_start, win_end = proposal.window_start, proposal.window_end
            basis.append(proposal.proposal_id)

            rain_obs = [
                o
                for o in observations
                if o.parcel_id == parcel
                and o.kind == "heavy-rain"
                and o.occurred_at <= at
            ]
            dry_after = None
            if rain_obs:
                last_rain = rain_obs[-1]
                basis.append(last_rain.observation_id)
                rain_end = last_rain.ended_at or last_rain.occurred_at
                dry_after = rain_end + timedelta(hours=DRYING_HOURS)
                if dry_after > at:
                    wait_agronomy = True
                    reasons.append(
                        f"突发暴雨（{last_rain.occurred_at:%m-%d %H:%M}，"
                        f"{last_rain.value or '雨量记录'}）后需脱水 {DRYING_HOURS}h，"
                        f"{dry_after:%m-%d %H:%M} 前不宜采收"
                    )
            ev.gates["rainfall_observed"] = {
                "dry_after": dry_after.isoformat() if dry_after else None
            }

            forecast = self._latest_forecast(parcel, at)
            ev.gates["forecast_id"] = forecast.forecast_id if forecast else None
            if forecast:
                original = (win_start, win_end)
                win_start, win_end = self._shrink_window(
                    win_start, win_end, at, forecast
                )
                if (win_start, win_end) != original:
                    reasons.append(
                        f"预报 {forecast.forecast_id}（{forecast.issued_at:%m-%d %H:%M} "
                        f"发布）把建议窗口收缩为 "
                        f"{win_start:%m-%d %H:%M}–{win_end:%m-%d %H:%M}；"
                        f"预报只缩短窗口，不改变质量结论"
                    )

            if not (win_start <= at <= win_end):
                wait_agronomy = True
                if at < win_start:
                    reasons.append(f"采收窗口 {win_start:%m-%d %H:%M} 才开启")
                else:
                    reasons.append("建议窗口已被预报关闭，等待技术员重新评估")

        ev.window_start, ev.window_end = win_start, win_end

        # ---- 并发变更：领取（或被自动暂停）后版本落后 -------------------
        wo = self.latest_work_order(parcel, at) if include_work_order else None
        stale = bool(
            wo
            and wo.state in (WorkOrderState.CLAIMED, WorkOrderState.PAUSED)
            and wo.basis_version < version
        )
        if wo and wo.state == WorkOrderState.PAUSED:
            reasons.append(
                f"作业单 {wo.work_order_id} 领取后发生并发变更，已暂停，"
                f"须按数据版本 v{version} 现场重新确认后才能开工"
            )
        elif stale:
            reasons.append(
                f"作业单 {wo.work_order_id} 领取时依据 v{wo.basis_version}，"
                f"当前为 v{version}，须现场重新确认"
            )
        ev.gates["work_order"] = {
            "id": wo.work_order_id if wo else None,
            "state": wo.state.value if wo else None,
            "basis_version": wo.basis_version if wo else None,
            "stale_claim": stale,
        }

        # ---- 综合判定（质量否决 > 并发暂停 > 农艺等待） -----------------
        ev.reasons = reasons
        ev.basis_observation_ids = list(dict.fromkeys(basis))
        if quality_blocked:
            ev.state = ParcelDecision.PAUSED
        elif stale or (wo and wo.state == WorkOrderState.PAUSED):
            ev.state = ParcelDecision.PAUSED
        elif wait_agronomy:
            ev.state = ParcelDecision.WAIT
        else:
            ev.state = ParcelDecision.READY
        return ev

    @staticmethod
    def _rain_bands(forecast):
        """达标的逐时雨量合并为 [起, 止) 连续雨带。"""
        wet = sorted(clock for clock, mm in forecast.hours if mm >= RAIN_THRESHOLD_MM)
        if not wet:
            return []
        gaps = [b - a for a, b in zip(wet, wet[1:]) if b > a]
        step = min(gaps) if gaps else timedelta(hours=1)
        bands = []
        start = prev = wet[0]
        for clock in wet[1:]:
            if clock - prev == step:
                prev = clock
            else:
                bands.append((start, prev + step))
                start = prev = clock
        bands.append((start, prev + step))
        return bands

    def _shrink_window(self, win_start, win_end, at, forecast):
        """预报只做收缩：推过压住当前时刻的雨带，并把终点截到下一条雨带前。"""
        lower, upper = max(win_start, at), win_end
        bands = self._rain_bands(forecast)
        for band_start, band_end in bands:
            if band_start <= lower < band_end:
                lower = band_end
        for band_start, _band_end in bands:
            if lower <= band_start < upper:
                upper = band_start
                break
        return lower, upper

    # ------------------------------------------------------------- 回放/视图

    def replay(self, fixture):
        """按录入/命令时间混合排序回放，重建完整决策历史。"""
        events = []
        for obs in fixture["observations"]:
            if obs.kind == "field-input":
                continue  # 施用走 applications，避免重复计数
            events.append((obs.recorded_at, 0, "observation", obs))
        for app in fixture["applications"]:
            events.append((app.recorded_at, 0, "application", app))
        for sample in fixture["samples"]:
            events.append((sample.recorded_at or sample.sampled_at, 0, "sample", sample))
        for forecast in fixture["forecasts"]:
            events.append((forecast.issued_at, 0, "forecast", forecast))
        for proposal in fixture["proposals"]:
            events.append((proposal.proposed_at, 0, "proposal", proposal))
        for wo in fixture["work_orders"]:
            events.append((wo.claimed_at, 1, "claim", wo))
        for command in fixture["timeline"]:
            events.append((command["at"], 1, command["type"], command))

        for _, _, kind, payload in sorted(events, key=lambda e: (e[0], e[1])):
            if kind == "observation":
                self.ingest_observation(payload)
            elif kind == "application":
                self.ingest_application(payload)
            elif kind == "sample":
                self.ingest_sample(payload)
            elif kind == "forecast":
                self.ingest_forecast(payload)
            elif kind == "proposal":
                self.ingest_proposal(payload)
            elif kind == "claim":
                self.claim(payload.parcel_id, payload.claimed_at, payload.work_order_id)
            elif kind == "reconfirm":
                self.reconfirm(payload["parcel"], payload["by"], payload["at"])
            elif kind == "release":
                self.release(
                    payload["parcel"], payload["by"], payload["at"], payload["decision_id"]
                )
            elif kind == "withdraw":
                self.withdraw(
                    payload["parcel"],
                    payload["by"],
                    payload["at"],
                    payload["due_to_observation"],
                    payload["decision_id"],
                )
        return self

    def view(self, at):
        return {parcel: self.evaluate(parcel, at) for parcel in self.parcels}

    def audit_trail(self):
        """完整决策链：被撤回的放行仍在链中，可逐版解释。"""
        return sorted(self.decisions, key=lambda d: (d.decided_at, d.version))
