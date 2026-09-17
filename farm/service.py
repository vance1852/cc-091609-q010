"""采收窗口工作流：技术员提案 → 质量放行 → 现场领取 → 作业 → 并发变更处理。

所有决定只追加（HarvestDecision 版本链），迟到资料触发新版本，绝不覆盖旧版本。
"""

from dataclasses import replace
from datetime import datetime

from .contracts import HarvestDecision, ParcelDecision
from .engine import evaluate, signature
from .ledger import Ledger
from .models import (
    Advisory,
    DecisionNote,
    DomainError,
    NotReady,
    QualityVeto,
    Reason,
    Rules,
    WorkOrder,
    WorkOrderState,
)
from .views import build_view


class HarvestService:
    def __init__(self, ledger: Ledger, rules: Rules | None = None) -> None:
        self.ledger = ledger
        self.rules = rules or Rules()
        self._advisory_seq = 0

    # -- 内部工具 ----------------------------------------------------------

    def _append(
        self,
        parcel_id: str,
        state: ParcelDecision,
        proposed_by: str,
        at: datetime,
        released_by: str | None = None,
        rec=None,
    ) -> HarvestDecision:
        chain = self.ledger.decision_chain(parcel_id)
        version = len(chain) + 1
        decision = HarvestDecision(
            decision_id=f"{parcel_id}-D{version}",
            parcel_id=parcel_id,
            state=state,
            observation_ids=tuple(e.ref for e in (rec.evidence if rec else ())),
            proposed_by=proposed_by,
            released_by=released_by,
            version=version,
            decided_at=at,
            supersedes_id=chain[-1].decision_id if chain else None,
        )
        note = DecisionNote(
            window_start=rec.window_start if rec else None,
            window_end=rec.window_end if rec else None,
            reasons=rec.reasons if rec else (),
        )
        self.ledger.append_decision(decision, note)
        return decision

    def _require_ready(self, parcel_id: str, at: datetime):
        rec = evaluate(self.ledger, parcel_id, at, self.rules)
        if rec.state is not ParcelDecision.READY:
            blockers = tuple(r for r in rec.reasons if r.blocking)
            if blockers:
                raise QualityVeto(parcel_id, blockers)
            raise NotReady(parcel_id, rec.reasons)
        return rec

    # -- 技术员：提出窗口 ---------------------------------------------------

    def propose_window(self, parcel_id: str, proposed_by: str, at: datetime) -> HarvestDecision:
        rec = evaluate(self.ledger, parcel_id, at, self.rules)
        return self._append(parcel_id, rec.state, proposed_by, at, rec=rec)

    # -- 质量：放行 / 撤回 --------------------------------------------------

    def release(self, parcel_id: str, released_by: str, at: datetime) -> HarvestDecision:
        rec = self._require_ready(parcel_id, at)
        return self._append(
            parcel_id, ParcelDecision.READY, released_by, at,
            released_by=released_by, rec=rec,
        )

    def withdraw(
        self,
        parcel_id: str,
        withdrawn_by: str,
        at: datetime,
        rec,
        cause: str,
    ) -> HarvestDecision:
        """质量因新资料撤回放行：追加 PAUSED 版本，旧放行原样保留。

        ``cause`` 追加为阻断理由，便于审计撤回触发来源（迟到记录/并发变更）。
        """
        reasons = tuple(rec.reasons) + (
            Reason(f"withdrawn:{cause}", True, f"放行已由 {withdrawn_by} 撤回（{cause}）", None),
        )
        rec_with_cause = replace(rec, reasons=reasons)
        return self._append(
            parcel_id,
            ParcelDecision.PAUSED,
            withdrawn_by,
            at,
            released_by=None,
            rec=rec_with_cause,
        )

    # -- 作业单 -------------------------------------------------------------

    def create_order(self, order_id: str, parcel_id: str, at: datetime) -> WorkOrder:
        current = self.ledger.current_decision(parcel_id)
        if current is None or current.state is not ParcelDecision.READY or current.released_by is None:
            raise DomainError(f"{parcel_id} 尚无有效质量放行，不能开出作业单")
        order = WorkOrder(
            order_id=order_id,
            parcel_id=parcel_id,
            state=WorkOrderState.RELEASED,
            confirmed_decision_id=current.decision_id,
        )
        self.ledger.update_order(order)
        return order

    def claim_order(self, order_id: str, at: datetime) -> WorkOrder:
        order = self.ledger.orders[order_id]
        if order.state is not WorkOrderState.RELEASED:
            raise DomainError(f"作业单 {order_id} 状态 {order.state}，不可领取")
        current = self.ledger.current_decision(order.parcel_id)
        if current.decision_id != order.confirmed_decision_id:
            raise DomainError("放行版本已变化，必须先重新确认再领取")
        order = replace(order, state=WorkOrderState.CLAIMED, claimed_at=at)
        self.ledger.update_order(order)
        return order

    def reconfirm_order(
        self, order_id: str, released_by: str, at: datetime
    ) -> tuple[WorkOrder, HarvestDecision]:
        """现场领取后发生并发变更，质量重新确认当前版本。"""
        order = self.ledger.orders[order_id]
        if order.state is not WorkOrderState.AWAITING_RECONFIRM:
            raise DomainError(f"作业单 {order_id} 不在待重新确认状态")
        rec = self._require_ready(order.parcel_id, at)
        decision = self._append(
            order.parcel_id, ParcelDecision.READY, released_by, at,
            released_by=released_by, rec=rec,
        )
        order = replace(order, state=WorkOrderState.CLAIMED, confirmed_decision_id=decision.decision_id)
        self.ledger.update_order(order)
        return order, decision

    def start_order(self, order_id: str, at: datetime) -> WorkOrder:
        order = self.ledger.orders[order_id]
        if order.state is not WorkOrderState.CLAIMED:
            raise DomainError(f"作业单 {order_id} 状态 {order.state}，不能开工")
        current = self.ledger.current_decision(order.parcel_id)
        if current.decision_id != order.confirmed_decision_id:
            raise DomainError("领取后数据已变更，必须等待质量重新确认")
        order = replace(order, state=WorkOrderState.IN_PROGRESS, started_at=at)
        self.ledger.update_order(order)
        return order

    def complete_order(self, order_id: str, at: datetime) -> WorkOrder:
        order = self.ledger.orders[order_id]
        if order.state not in (WorkOrderState.IN_PROGRESS, WorkOrderState.CLAIMED):
            raise DomainError(f"作业单 {order_id} 状态 {order.state}，不能完工")
        order = replace(order, state=WorkOrderState.COMPLETED, completed_at=at)
        self.ledger.update_order(order)
        return order

    # -- 并发变更同步 --------------------------------------------------------

    def sync_parcel(self, parcel_id: str, at: datetime) -> dict:
        """录入新资料后调用：复算地块，对在途作业做不可变处理。

        - 未领取的放行单：放行撤回，作业单取消；
        - 已领取未开工：作业单挂起待重新确认；若存在质量阻断，追加 PAUSED 撤回版本；
        - 已开工/已完工：历史决定保留，追加警示（建议隔离/复检）。
        """
        rec = evaluate(self.ledger, parcel_id, at, self.rules)
        result = {"withdrawn": [], "advisories": [], "reconfirm": []}

        for order in self.ledger.orders_for(parcel_id):
            if order.state == WorkOrderState.CANCELLED:
                continue
            confirmed = self._decision_by_id(order.confirmed_decision_id)
            note = self.ledger.notes.get(order.confirmed_decision_id)
            if confirmed is not None:
                changed = self._snapshot_signature(note, confirmed) != signature(rec)
            else:
                changed = True
            if not changed:
                continue

            if order.state == WorkOrderState.RELEASED:
                # 尚未领取：直接撤回放行
                self.withdraw(parcel_id, "quality-system", at, rec, "concurrent-change")
                self.ledger.update_order(replace(order, state=WorkOrderState.CANCELLED))
                result["withdrawn"].append(order.order_id)

            elif order.state == WorkOrderState.CLAIMED:
                if rec.state == ParcelDecision.PAUSED:
                    self.withdraw(parcel_id, "quality-system", at, rec, "late-record")
                self.ledger.update_order(
                    replace(order, state=WorkOrderState.AWAITING_RECONFIRM)
                )
                result["reconfirm"].append(order.order_id)

            elif order.state == WorkOrderState.AWAITING_RECONFIRM:
                # 等待期间又有新数据：更新挂起版本（若阻断）即可，仍需重新确认
                if rec.state == ParcelDecision.PAUSED:
                    self.withdraw(parcel_id, "quality-system", at, rec, "late-record")
                result["reconfirm"].append(order.order_id)

            else:  # IN_PROGRESS / COMPLETED：已执行的决定不可暂停或抹去，只能追加警示
                if any(a.order_id == order.order_id for a in self.ledger.advisories_for(parcel_id)):
                    continue
                advisory = self._advisory(parcel_id, order, at, rec)
                result["advisories"].append(advisory.advisory_id)
        return result

    def sync_all(self, at: datetime) -> dict:
        merged = {"withdrawn": [], "advisories": [], "reconfirm": []}
        for parcel_id in self.ledger.parcels:
            for key, values in self.sync_parcel(parcel_id, at).items():
                merged[key].extend(values)
        return merged

    # -- 视图 ----------------------------------------------------------------

    def view(self, parcel_id: str, as_of: datetime):
        return build_view(self.ledger, parcel_id, as_of, self.rules)

    # -- 辅助 ----------------------------------------------------------------

    def _decision_by_id(self, decision_id: str | None):
        if decision_id is None:
            return None
        for chain in self.ledger._decisions.values():  # noqa: SLF001
            for d in chain:
                if d.decision_id == decision_id:
                    return d
        return None

    @staticmethod
    def _snapshot_signature(note, decision: HarvestDecision):
        if note is None:
            return ("state", str(decision.state))
        return (
            str(decision.state),
            note.window_start.isoformat() if note.window_start else None,
            note.window_end.isoformat() if note.window_end else None,
            tuple(sorted((r.code, r.ref or "") for r in note.reasons if r.blocking)),
            tuple(decision.observation_ids),
        )

    def _advisory(self, parcel_id: str, order: WorkOrder, at: datetime, rec) -> Advisory:
        self._advisory_seq += 1
        blocking = [r for r in rec.reasons if r.blocking]
        kind = "quarantine-recheck" if blocking else "window-changed"
        message = (
            "作业已在执行，新资料（"
            + "；".join(r.message for r in (blocking or rec.reasons))
            + "）不能撤回已执行决定，建议该批原料隔离并复检"
            if blocking
            else "作业进行中预报更新，建议在雨带前停止采收并记录实际完工时间"
        )
        advisory = Advisory(
            advisory_id=f"ADV-{self._advisory_seq:03d}",
            parcel_id=parcel_id,
            order_id=order.order_id,
            kind=kind,
            message=message,
            observation_refs=tuple(e.ref for e in rec.evidence),
            created_at=at,
        )
        self.ledger.add_advisory(advisory)
        return advisory
