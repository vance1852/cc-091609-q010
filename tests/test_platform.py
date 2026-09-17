"""平台行为测试：双时态、质量否决、混合样范围、并发变更与不可抹除历史。"""

import unittest
from datetime import datetime, timedelta

from farm.contracts import (
    FieldObservation,
    HarvestDecision,
    ParcelDecision,
    SampleResult,
)
from farm.engine import evaluate
from farm.ledger import Ledger
from farm.models import (
    InputProduct,
    Parcel,
    QualityVeto,
    RainHour,
    RainfallForecast,
    Rules,
    WorkOrderState,
)
from farm.service import HarvestService


def ts(v: str) -> datetime:
    return datetime.fromisoformat(v)


class BaseCase(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = Ledger()
        self.ledger.register_parcel(Parcel("field-a", "黄芩", None))
        self.ledger.register_parcel(Parcel("field-b", "黄芩", None))
        self.ledger.register_product(InputProduct("FUNG-A", "多菌灵", 168))
        self.rules = Rules()
        self.svc = HarvestService(self.ledger, self.rules)

    def add_phenology(self, parcel="field-a", at="2026-09-12T09:00:00+08:00",
                      value="fruit-mature", recorded=None):
        self.ledger.add_observation(FieldObservation(
            f"PH-{parcel}", parcel, "flowering-stage", value,
            ts(at), ts(recorded or at),
        ))

    def add_sample(self, sid="SMP-1", parcels=("field-a",), result="pass",
                   sampled="2026-09-14T10:00:00+08:00",
                   valid="2026-09-21T10:00:00+08:00", mixed=False):
        self.ledger.add_sample(SampleResult(
            sid, tuple(parcels), ts(sampled), ts(valid), result, mixed,
        ))

    def ready_release_claim(self, parcel="field-a", order="WO-1",
                            at="2026-09-15T12:00:00+08:00"):
        self.svc.propose_window(parcel, "tech", ts(at))
        self.svc.release(parcel, "qa", ts(at))
        self.svc.create_order(order, parcel, ts(at))
        self.svc.claim_order(order, ts(at))


class BitemporalTests(BaseCase):
    def test_late_observation_invisible_before_recorded_time(self):
        self.add_phenology()
        self.add_sample()
        self.ledger.add_observation(FieldObservation(
            "IN-LATE", "field-a", "field-input", "FUNG-A",
            occurred_at=ts("2026-09-14T10:00:00+08:00"),
            recorded_at=ts("2026-09-16T08:30:00+08:00"),
        ))
        before = evaluate(self.ledger, "field-a", ts("2026-09-15T12:00:00+08:00"))
        after = evaluate(self.ledger, "field-a", ts("2026-09-16T09:00:00+08:00"))
        self.assertIs(before.state, ParcelDecision.READY)
        self.assertIs(after.state, ParcelDecision.PAUSED)
        self.assertIn("phi-active", [r.code for r in after.reasons])

    def test_immature_parcel_waits(self):
        self.add_phenology(value="full-flowering")
        self.add_sample()
        rec = evaluate(self.ledger, "field-a", ts("2026-09-15T09:00:00+08:00"))
        self.assertIs(rec.state, ParcelDecision.WAIT)


class QualityGateTests(BaseCase):
    def test_release_blocked_by_phi(self):
        self.add_phenology()
        self.add_sample()
        self.ledger.add_observation(FieldObservation(
            "IN-1", "field-a", "field-input", "FUNG-A",
            ts("2026-09-15T10:00:00+08:00"), ts("2026-09-15T10:05:00+08:00"),
        ))
        with self.assertRaises(QualityVeto):
            self.svc.release("field-a", "qa", ts("2026-09-15T12:00:00+08:00"))

    def test_release_blocked_without_sample(self):
        self.add_phenology()
        with self.assertRaises(QualityVeto):
            self.svc.release("field-a", "qa", ts("2026-09-15T12:00:00+08:00"))

    def test_expired_sample_blocks(self):
        self.add_phenology()
        self.add_sample(valid="2026-09-16T10:00:00+08:00")
        rec = evaluate(self.ledger, "field-a", ts("2026-09-17T08:00:00+08:00"))
        self.assertIs(rec.state, ParcelDecision.PAUSED)

    def test_failed_sample_blocks(self):
        self.add_phenology()
        self.add_sample(result="fail")
        rec = evaluate(self.ledger, "field-a", ts("2026-09-15T12:00:00+08:00"))
        self.assertIs(rec.state, ParcelDecision.PAUSED)

    def test_mixed_sample_does_not_extend_scope(self):
        self.add_phenology("field-a")
        self.add_phenology("field-b")
        self.add_sample("MIX", ("field-a", "field-b"), mixed=True)
        # field-a/b 可被混合样覆盖；field-c 无样
        self.ledger.register_parcel(Parcel("field-c", "黄芩", None))
        self.ledger.add_observation(FieldObservation(
            "PH-c", "field-c", "flowering-stage", "fruit-mature",
            ts("2026-09-12T09:00:00+08:00"), ts("2026-09-12T09:00:00+08:00"),
        ))
        self.assertIs(evaluate(self.ledger, "field-a", ts("2026-09-15T12:00:00+08:00")).state,
                      ParcelDecision.READY)
        self.assertIs(evaluate(self.ledger, "field-c", ts("2026-09-15T12:00:00+08:00")).state,
                      ParcelDecision.PAUSED)
        self.assertIsNone(self.ledger.effective_sample(
            "field-c", ts("2026-09-15T12:00:00+08:00"), ts("2026-09-15T12:00:00+08:00")))

    def test_forecast_shrinks_window_but_cannot_override_quality(self):
        self.add_phenology()
        self.add_sample()
        self.ledger.add_observation(FieldObservation(
            "IN-1", "field-a", "field-input", "FUNG-A",
            ts("2026-09-15T10:00:00+08:00"), ts("2026-09-15T10:05:00+08:00"),
        ))
        self.ledger.publish_forecast(RainfallForecast(
            issued_at=ts("2026-09-15T11:00:00+08:00"),
            hours=(RainHour(ts("2026-09-20T10:00:00+08:00"), 0.0),),
        ))
        rec = evaluate(self.ledger, "field-a", ts("2026-09-15T12:00:00+08:00"))
        # 即使预报给出大片无雨时段，安全间隔仍然暂停
        self.assertIs(rec.state, ParcelDecision.PAUSED)

    def test_forecast_shortens_ready_window(self):
        self.add_phenology()
        self.add_sample()
        self.ledger.publish_forecast(RainfallForecast(
            issued_at=ts("2026-09-17T06:00:00+08:00"),
            hours=(RainHour(ts("2026-09-17T18:00:00+08:00"), 12.0),),
        ))
        rec = evaluate(self.ledger, "field-a", ts("2026-09-17T07:00:00+08:00"))
        self.assertIs(rec.state, ParcelDecision.READY)
        self.assertEqual(rec.window_end, ts("2026-09-17T12:00:00+08:00"))

    def test_heavy_rain_drydown_waits(self):
        self.add_phenology("field-b")
        self.add_sample("SMP-B", ("field-b",))
        self.ledger.add_observation(FieldObservation(
            "RAIN-B", "field-b", "heavy-rain", "42",
            ts("2026-09-16T04:00:00+08:00"), ts("2026-09-16T04:05:00+08:00"),
        ))
        rec = evaluate(self.ledger, "field-b", ts("2026-09-16T12:00:00+08:00"))
        self.assertIs(rec.state, ParcelDecision.WAIT)
        self.assertIn("rain-drydown", [r.code for r in rec.reasons])


class WorkflowTests(BaseCase):
    def test_full_release_claim_withdrawal_reconfirm_chain(self):
        self.add_phenology()
        self.add_sample(valid="2026-09-24T10:00:00+08:00")
        t1 = ts("2026-09-15T12:00:00+08:00")
        self.ready_release_claim(at="2026-09-15T12:00:00+08:00")

        chain1 = self.ledger.decision_chain("field-a")
        self.assertEqual([d.version for d in chain1], [1, 2])
        self.assertIsNotNone(chain1[-1].released_by)

        # 迟到施用记录暴露，已领取作业挂起并追加 PAUSED 撤回版本
        self.ledger.add_observation(FieldObservation(
            "IN-LATE", "field-a", "field-input", "FUNG-A",
            ts("2026-09-14T10:00:00+08:00"), ts("2026-09-16T08:30:00+08:00"),
        ))
        result = self.svc.sync_parcel("field-a", ts("2026-09-16T09:00:00+08:00"))
        self.assertEqual(result["reconfirm"], ["WO-1"])
        order = self.ledger.orders["WO-1"]
        self.assertIs(order.state, WorkOrderState.AWAITING_RECONFIRM)

        chain2 = self.ledger.decision_chain("field-a")
        self.assertEqual(len(chain2), 3)
        withdrawn = chain2[-1]
        self.assertIs(withdrawn.state, ParcelDecision.PAUSED)
        self.assertIsNone(withdrawn.released_by)
        self.assertEqual(withdrawn.supersedes_id, chain1[-1].decision_id)

        # 旧放行原样保留：不可抹除
        self.assertIs(chain2[1].state, ParcelDecision.READY)
        self.assertEqual(chain2[1].released_by, "qa")
        self.assertEqual(chain2[1].decision_id, "field-a-D2")

        # 间隔内重新确认被质量否决
        with self.assertRaises(QualityVeto):
            self.svc.reconfirm_order("WO-1", "qa", ts("2026-09-16T10:00:00+08:00"))

        # 间隔结束（施用 09-14 10:00 +168h = 09-21 10:00）后重新确认成功
        t_ok = ts("2026-09-21T11:00:00+08:00")
        order, decision = self.svc.reconfirm_order("WO-1", "qa", t_ok)
        self.assertIs(order.state, WorkOrderState.CLAIMED)
        self.assertEqual(order.confirmed_decision_id, decision.decision_id)
        self.assertEqual(decision.version, 4)
        self.assertEqual(decision.supersedes_id, "field-a-D3")

    def test_late_input_pauses_unclaimed_and_cancels_order(self):
        self.add_phenology()
        self.add_sample()
        t1 = ts("2026-09-15T12:00:00+08:00")
        self.svc.propose_window("field-a", "tech", t1)
        self.svc.release("field-a", "qa", t1)
        self.svc.create_order("WO-9", "field-a", t1)  # 未领取

        self.ledger.add_observation(FieldObservation(
            "IN-LATE", "field-a", "field-input", "FUNG-A",
            ts("2026-09-14T10:00:00+08:00"), ts("2026-09-16T08:30:00+08:00"),
        ))
        result = self.svc.sync_parcel("field-a", ts("2026-09-16T09:00:00+08:00"))
        self.assertEqual(result["withdrawn"], ["WO-9"])
        self.assertIs(self.ledger.orders["WO-9"].state, WorkOrderState.CANCELLED)

    def test_started_order_keeps_history_and_gets_advisory(self):
        self.add_phenology()
        self.add_sample()
        t1 = ts("2026-09-15T12:00:00+08:00")
        self.ready_release_claim(order="WO-RUN", at="2026-09-15T12:00:00+08:00")
        self.svc.start_order("WO-RUN", ts("2026-09-15T13:00:00+08:00"))

        self.ledger.add_observation(FieldObservation(
            "IN-LATE", "field-a", "field-input", "FUNG-A",
            ts("2026-09-14T10:00:00+08:00"), ts("2026-09-16T08:30:00+08:00"),
        ))
        result = self.svc.sync_parcel("field-a", ts("2026-09-16T09:00:00+08:00"))
        self.assertEqual(len(result["advisories"]), 1)
        # 作业未被改写为暂停/取消
        self.assertIs(self.ledger.orders["WO-RUN"].state, WorkOrderState.IN_PROGRESS)
        # 旧放行版本依然是 READY 且保留
        chain = self.ledger.decision_chain("field-a")
        self.assertEqual(
            [(d.version, str(d.state)) for d in chain],
            [(1, "ready"), (2, "ready")],
        )
        adv = self.ledger.advisories[0]
        self.assertEqual(adv.kind, "quarantine-recheck")
        self.assertIn("IN-LATE", adv.observation_refs)

    def test_concurrent_forecast_update_requires_reconfirm(self):
        self.add_phenology()
        self.add_sample()
        self.ready_release_claim(at="2026-09-15T12:00:00+08:00")
        self.ledger.publish_forecast(RainfallForecast(
            issued_at=ts("2026-09-15T13:00:00+08:00"),
            hours=(RainHour(ts("2026-09-15T20:00:00+08:00"), 15.0),),
        ))
        result = self.svc.sync_parcel("field-a", ts("2026-09-15T13:05:00+08:00"))
        # 非阻断性变更：挂起等待重新确认，但不追加撤回版本
        self.assertEqual(result["reconfirm"], ["WO-1"])
        self.assertEqual(len(self.ledger.decision_chain("field-a")), 2)
        with self.assertRaises(Exception):
            self.svc.start_order("WO-1", ts("2026-09-15T14:00:00+08:00"))

    def test_cannot_claim_after_version_changed(self):
        self.add_phenology()
        self.add_sample()
        t1 = ts("2026-09-15T12:00:00+08:00")
        self.svc.propose_window("field-a", "tech", t1)
        self.svc.release("field-a", "qa", t1)
        self.svc.create_order("WO-X", "field-a", t1)
        # 无实质变化的重放行版本也会使旧单失效
        self.svc.release("field-a", "qa", t1)
        with self.assertRaises(Exception):
            self.svc.claim_order("WO-X", t1)

    def test_evidence_lists_data_versions_used(self):
        self.add_phenology()
        self.add_sample()
        rec = evaluate(self.ledger, "field-a", ts("2026-09-15T12:00:00+08:00"))
        refs = {e.ref for e in rec.evidence}
        self.assertIn("PH-field-a", refs)
        self.assertIn("SMP-1", refs)


if __name__ == "__main__":
    unittest.main()
