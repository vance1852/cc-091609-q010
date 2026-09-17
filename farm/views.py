"""按地块组装可读视图：状态、窗口、理由、数据版本、决策链、警示。"""

from datetime import datetime

from .contracts import ParcelDecision
from .engine import evaluate
from .ledger import Ledger
from .models import ParcelView, Rules, WorkOrderState


def build_view(
    ledger: Ledger, parcel_id: str, as_of: datetime, rules: Rules | None = None
) -> ParcelView:
    rules = rules or Rules()
    rec = evaluate(ledger, parcel_id, as_of, rules)
    orders = ledger.orders_for(parcel_id)
    active = next(
        (o for o in reversed(orders) if o.state != WorkOrderState.CANCELLED),
        orders[-1] if orders else None,
    )
    chain = ledger.decision_chain(parcel_id)

    # 作业领取后若决策版本已推进，真实状态照实显示（暂停/等待），
    # 由作业单状态上的“待重新确认”提示补充流程含义
    state = rec.state
    if active is not None and active.state in (
        WorkOrderState.IN_PROGRESS,
        WorkOrderState.COMPLETED,
    ):
        # 已开工的作业不被新资料抹除，但阻断项仍需显式提示
        state = rec.state

    known_times = ledger.known_times(parcel_id, as_of) + [as_of]
    return ParcelView(
        parcel=ledger.parcels[parcel_id],
        state=state,
        as_of=as_of,
        knowledge_cutoff=max(known_times),
        recommendation=rec,
        work_order=active,
        window_start=rec.window_start,
        window_end=rec.window_end,
        decision_chain=tuple(chain),
        advisories=tuple(ledger.advisories_for(parcel_id)),
        notes=dict(ledger.notes),
    )


def render_text(view: ParcelView) -> str:
    labels = {
        ParcelDecision.READY: "可采",
        ParcelDecision.WAIT: "等待",
        ParcelDecision.PAUSED: "暂停",
    }
    suffix = ""
    if view.work_order and view.work_order.state == WorkOrderState.AWAITING_RECONFIRM:
        suffix = "（作业待重新确认）"
    lines = [
        f"地块 {view.parcel.parcel_id}（{view.parcel.crop}） @ {view.as_of.isoformat()}",
        f"状态：{labels[view.state]}{suffix}",
    ]
    ws, we = view.window_start, view.window_end
    if we is not None and ws is not None and we <= ws:
        lines.append(f"建议窗口：已关闭（最早可行 {ws.isoformat()} 已晚于雨带前撤离时限 {we.isoformat()}）")
    else:
        lines.append(
            f"建议窗口：{ws.isoformat() if ws else '—'} → "
            f"{we.isoformat() if we else '（无雨带上限）'}"
        )
    lines.append(f"知识截止：recorded_at ≤ {view.knowledge_cutoff.isoformat()}")
    if view.work_order:
        o = view.work_order
        suffix = ""
        if o.state == WorkOrderState.COMPLETED and view.state != ParcelDecision.READY:
            suffix = "（历史已完工不可抹除；当前资料评估为非可采，见下方警示）"
        elif o.state == WorkOrderState.AWAITING_RECONFIRM:
            suffix = "（领取后数据变更，必须重新确认方可继续）"
        lines.append(
            f"作业单 {o.order_id}：{o.state}{suffix}"
            + (f"，锁定版本 {o.confirmed_decision_id}" if o.confirmed_decision_id else "")
        )
    lines.append("理由：")
    for r in view.recommendation.reasons:
        lines.append(f"  - [{'阻断' if r.blocking else '提示'}] {r.message}（{r.code}）")
    lines.append("采用的数据版本：")
    for e in view.recommendation.evidence:
        lines.append(
            f"  - {e.ref} [{e.kind}] 发生 {e.occurred_at.isoformat()} / "
            f"录入 {e.recorded_at.isoformat()} — {e.title}"
        )
    lines.append("决策版本链：")
    for d in view.decision_chain:
        note = view.notes.get(d.decision_id)
        lines.append(
            f"  - v{d.version} {d.decision_id} {d.state} "
            f"提案人={d.proposed_by} 放行={d.released_by or '—'} "
            f"时间={d.decided_at.isoformat()} 取代={d.supersedes_id or '—'}"
        )
        if note and note.reasons:
            for r in note.reasons:
                lines.append(f"      · {r.message}")
    if view.advisories:
        lines.append("历史作业警示（不可抹除，仅追加）：")
        for a in view.advisories:
            lines.append(f"  - {a.advisory_id} [{a.kind}] {a.message}")
    return "\n".join(lines)
