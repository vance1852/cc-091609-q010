"""端到端场景回放：复现“放行被迟到施用记录撤回、再重新确认”的完整过程。

运行：python -m farm.demo
"""

from datetime import datetime

from .contracts import FieldObservation
from .loader import load_fixture
from .models import (
    NotReady,
    QualityVeto,
    RainHour,
    RainfallForecast,
)
from .service import HarvestService
from .views import render_text

FIXTURE = "fixtures/harvest_window.json"


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def run() -> str:
    ledger, rules, _ = load_fixture(FIXTURE, materialize_orders=False)
    svc = HarvestService(ledger, rules)
    out: list[str] = []
    log = out.append

    # 为演示“已执行决定不可抹除”：field-b 在迟到记录暴露前已完成采收，
    # 另有一条施用记录在完工后才补录（脚本内追加，夹具之外）。
    late_input_b = FieldObservation(
        observation_id="OBS-LATE-B",
        parcel_id="field-b",
        kind="field-input",
        value="FUNG-A",
        occurred_at=ts("2026-09-14T10:00:00+08:00"),
        recorded_at=ts("2026-09-16T09:00:00+08:00"),
    )

    # ── 第一幕：09-15 中午，迟到施用尚未录入 ──────────────────────────────
    t1 = ts("2026-09-15T12:00:00+08:00")
    log("=" * 78)
    log(f"【1】{t1.isoformat()}  补录施用与暴雨均未发生/未录入")
    log("=" * 78)

    for pid in ("field-a", "field-b", "field-c"):
        view = svc.view(pid, t1)
        log(render_text(view))
        log("")

    # 技术员提出窗口 → 质量放行 field-a → 现场领取
    svc.propose_window("field-a", "tech-chen", t1)
    svc.release("field-a", "qa-li", t1)
    order_a = svc.create_order("WO-A-01", "field-a", t1)
    order_a = svc.claim_order("WO-A-01", t1)
    log(f"→ field-a 作业单 {order_a.order_id} 已{order_a.state}，锁定 {order_a.confirmed_decision_id}")

    # 混合样 SMP-MIX1 只覆盖 field-a/field-b，field-c 不得因此获样
    assert ledger.effective_sample("field-c", t1, t1) is None
    log("→ 混合样范围校验：SMP-MIX1 不覆盖 field-c，field-c 维持暂停")

    # field-b 当日完成采收（决定已执行）
    svc.propose_window("field-b", "tech-chen", t1)
    svc.release("field-b", "qa-li", t1)
    svc.create_order("WO-B-01", "field-b", t1)
    svc.claim_order("WO-B-01", t1)
    svc.start_order("WO-B-01", ts("2026-09-15T14:00:00+08:00"))
    svc.complete_order("WO-B-01", ts("2026-09-15T17:00:00+08:00"))
    log("→ field-b WO-B-01 已于 09-15 17:00 完工")
    log("")

    # ── 第二幕：09-16 清晨暴雨；08:30 补录暴露 ───────────────────────────
    ledger.add_observation(late_input_b)

    t2 = ts("2026-09-16T09:30:00+08:00")
    log("=" * 78)
    log(f"【2】{t2.isoformat()}  暴雨已记录，field-a 施用记录补录暴露")
    log("=" * 78)
    changes_a = svc.sync_parcel("field-a", ts("2026-09-16T08:30:00+08:00"))
    log(f"→ field-a 并发处理：{changes_a}（已领取作业挂起，放行追加撤回版本）")
    changes_b = svc.sync_parcel("field-b", ts("2026-09-16T09:00:00+08:00"))
    log(f"→ field-b 并发处理：{changes_b}（作业已完工，仅追加隔离警示，历史不改写）")
    log("")

    for pid in ("field-a", "field-b", "field-c"):
        log(render_text(svc.view(pid, t2)))
        log("")

    # 质量试图立即重新放行 field-a：安全间隔未结束，否决成立
    try:
        svc.release("field-a", "qa-li", t2)
    except QualityVeto as exc:
        log(f"→ 质量否决被执行：{exc}")
    log("")

    # ── 第三幕：09-17 早晨新版预报，窗口缩短但不绕过质量 ───────────────────
    t3 = ts("2026-09-17T07:00:00+08:00")
    log("=" * 78)
    log(f"【3】{t3.isoformat()}  新版逐时预报发布（09-17 18:00 雨带）")
    log("=" * 78)
    log(render_text(svc.view("field-a", t3)))
    log("")
    try:
        svc.reconfirm_order("WO-A-01", "qa-li", t3)
    except QualityVeto as exc:
        log(f"→ 预报不能绕过安全间隔，重新确认被否决：{exc}")
    log("")

    # ── 第四幕：09-17 16:30 间隔结束，但雨带前撤时限已过 ──────────────────
    t4 = ts("2026-09-17T16:30:00+08:00")
    log("=" * 78)
    log(f"【4】{t4.isoformat()}  安全间隔刚结束，雨带 18:00 到达（需提前 6h）")
    log("=" * 78)
    view_a = svc.view("field-a", t4)
    log(f"→ field-a 状态={view_a.state.value}：质量阻断已解除，但预报把窗口压缩到关闭")
    try:
        svc.reconfirm_order("WO-A-01", "qa-li", t4)
    except QualityVeto as exc:
        log(f"→ 重新确认被质量否决：{exc}")
    except NotReady as exc:
        log(f"→ 重新确认不通过（非质量原因，等待窗口）：{exc}")
    log("")

    # ── 第五幕：09-18 中午新预报无雨，质量重新确认，作业恢复 ───────────────
    ledger.publish_forecast(
        RainfallForecast(
            issued_at=ts("2026-09-18T12:00:00+08:00"),
            hours=tuple(
                RainHour(at=ts(f"2026-09-18T{h:02d}:00:00+08:00"), mm=0.0)
                for h in (13, 18, 23)
            ),
        )
    )
    t5 = ts("2026-09-18T13:00:00+08:00")
    log("=" * 78)
    log(f"【5】{t5.isoformat()}  雨过天晴且新预报无雨，质量重新确认")
    log("=" * 78)
    order_a, decision = svc.reconfirm_order("WO-A-01", "qa-li", t5)
    log(f"→ 重新确认成功：新版本 {decision.decision_id}，作业单恢复 {order_a.state}")
    svc.start_order("WO-A-01", t5)
    svc.complete_order("WO-A-01", ts("2026-09-18T16:00:00+08:00"))
    log("→ WO-A-01 开工并完工；被撤回的放行版本仍完整保留在版本链中")
    log("")
    log(render_text(svc.view("field-a", t5)))

    return "\n".join(out)


if __name__ == "__main__":
    print(run())
