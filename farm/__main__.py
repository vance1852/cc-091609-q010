"""命令行：按夹具回放并输出地块决策说明。

用法::

    python -m farm [fixtures/harvest_window.json]
"""

import sys
from pathlib import Path

from .contracts import ParcelDecision, WorkOrderState
from .engine import HarvestLedger
from .loader import load_fixture

STATE_LABEL = {
    ParcelDecision.READY: "可采",
    ParcelDecision.WAIT: "等待",
    ParcelDecision.PAUSED: "暂停",
}

WO_LABEL = {
    WorkOrderState.CLAIMED: "已领取",
    WorkOrderState.PAUSED: "暂停待重新确认",
    WorkOrderState.STARTED: "已开工（决定锁定）",
    WorkOrderState.COMPLETED: "已完成",
}


def _fmt(clock):
    return clock.strftime("%Y-%m-%d %H:%M") if clock else "—"


def build_report(path):
    fx = load_fixture(path)
    ledger = HarvestLedger(fx["parcels"], fx["provenance"]).replay(fx)
    as_of = fx["as_of"]
    view = ledger.view(as_of)

    lines = []
    lines.append(f"作物：{fx['crop']}    评估时刻：{_fmt(as_of)}")
    lines.append("=" * 72)

    provenance = {p.parcel_id: p for p in fx["provenance"]}
    for parcel in fx["parcels"]:
        ev = view[parcel]
        prov = provenance.get(parcel)
        lines.append(f"【{parcel}】结论：{STATE_LABEL[ev.state].upper()}（{ev.state.value}）")
        if prov:
            lines.append(f"  种源：{prov.cultivar} / {prov.seed_source}")
        lines.append(f"  建议窗口：{_fmt(ev.window_start)} ~ {_fmt(ev.window_end)}")
        lines.append(f"  数据版本：v{ev.version}（评估时刻与本地块相关的可见事实数）")
        gate = ev.gates
        lines.append(
            "  门禁："
            f"物候={'成熟' if gate.get('phenology', {}).get('mature') else '未成熟'}；"
            f"安全间隔={'未通过' if gate.get('phi', {}).get('blocked') else '通过'}；"
            f"检测={'已覆盖(' + str(gate.get('sample', {}).get('sample_id')) + ')' if gate.get('sample', {}).get('covered') else '未覆盖'}"
        )
        ignored = gate.get("sample", {}).get("mixed_samples_ignored") or []
        if ignored:
            lines.append(f"  混合样 {', '.join(ignored)} 不含本地块，不扩展其合格范围")
        fc = gate.get("forecast_id")
        if fc:
            lines.append(f"  采用降雨预报：{fc}（只能收缩窗口，不绕过质量否决）")
        wo = gate.get("work_order") or {}
        if wo.get("id"):
            lines.append(
                f"  作业单 {wo['id']}：{WO_LABEL[WorkOrderState(wo['state'])]}"
                f"（领取依据 v{wo.get('basis_version')}）"
            )
        lines.append("  依据数据：" + (", ".join(ev.basis_observation_ids) or "无"))
        lines.append("  理由：")
        for reason in ev.reasons or ["全部门禁通过"]:
            lines.append(f"    - {reason}")
        lines.append("")

    # ---- 审计链：完整保留一次放行被迟到记录撤回的过程 ----------------
    lines.append("-" * 72)
    lines.append("决策审计链（只追加，被撤回的放行原样保留）")
    lines.append("-" * 72)
    for d in ledger.audit_trail():
        rows = ledger.parcel_decisions(d.parcel_id)
        idx = rows.index(d)
        successor = rows[idx + 1] if idx + 1 < len(rows) else None
        if successor is not None and successor.withdrawn_observation_id:
            marker = f" ← 被 {successor.decision_id} 撤回（迟到记录）"
        elif successor is not None:
            marker = f" ← 被 {successor.decision_id} 取代"
        else:
            marker = ""
        window = f"窗口 {_fmt(d.window_start)}~{_fmt(d.window_end)}"
        withdrawn = (
            f"｜撤回触发记录 {d.withdrawn_observation_id}（{d.withdrawn_by}）"
            if d.withdrawn_observation_id
            else ""
        )
        lines.append(
            f"{_fmt(d.decided_at)}  {d.decision_id}  [{d.parcel_id}] "
            f"{STATE_LABEL[d.state]} v{d.version} 依据数据版本 v{d.basis_version}  "
            f"放行 {d.released_by or '—'}{withdrawn}  {window}{marker}"
        )
        for reason in d.reasons:
            lines.append(f"      · {reason}")

    lines.append("")
    lines.append("-" * 72)
    lines.append("并发变更与重新确认")
    lines.append("-" * 72)
    for note in ledger.reconfirmations:
        head = (
            f"作业单 {note['work_order_id']}（{note['parcel_id']}）："
            f"{_fmt(note.get('paused_at'))} 数据版本 v{note['from_version']} → "
            f"v{note['to_version']}，自动暂停"
        )
        if "reconfirmed_at" in note:
            head = (
                f"作业单 {note['work_order_id']}（{note['parcel_id']}）："
                f"v{note['from_version']} → v{note['to_version']}，"
                f"{note['reconfirmed_by']} 于 {_fmt(note['reconfirmed_at'])} 现场重新确认"
            )
        else:
            head += "，等待现场重新确认"
        lines.append(head)

    # ---- 双时态对照：迟到施用录入前后的 field-a -----------------------
    lines.append("")
    lines.append("-" * 72)
    lines.append("双时态对照：field-a 在迟到施用记录录入前后")
    lines.append("-" * 72)
    late_app = max(
        (a for a in fx["applications"] if a.recorded_at > a.occurred_at),
        key=lambda a: a.recorded_at,
        default=None,
    )
    if late_app is not None:
        from datetime import timedelta

        reconfirmed = [
            n.get("reconfirmed_at")
            for n in ledger.reconfirmations
            if n.get("reconfirmed_at")
            and n.get("reconfirmed_at") < late_app.recorded_at
        ]
        anchor = max(reconfirmed) if reconfirmed else late_app.recorded_at - timedelta(hours=1)
        before = anchor + timedelta(minutes=10)
        after = late_app.recorded_at + timedelta(minutes=30)
        ev0 = ledger.evaluate("field-a", before)
        ev1 = ledger.evaluate("field-a", after)
        lines.append(
            f"{_fmt(before)}（现场已重新确认、施用记录尚未补录）"
            f"可见数据 v{ev0.version} → {STATE_LABEL[ev0.state]}"
        )
        lines.append(
            f"{_fmt(after)}（{late_app.application_id} 已于 "
            f"{_fmt(late_app.recorded_at)} 补录）可见数据 v{ev1.version} → "
            f"{STATE_LABEL[ev1.state]}：{ev1.reasons[0]}"
        )
        lines.append("迟到记录改变了之后的判断，但补录前作出的放行决定仍原样保留在审计链中。")

    return "\n".join(lines)


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    path = Path(argv[0]) if argv else Path(__file__).resolve().parent.parent / "fixtures" / "harvest_window.json"
    print(build_report(path))


if __name__ == "__main__":
    main()
