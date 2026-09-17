"""决策引擎规则测试。"""

import unittest
from datetime import datetime, timedelta
from pathlib import Path

from farm.contracts import (
    FieldObservation,
    HarvestDecision,
    InputApplication,
    ParcelDecision,
    RainfallForecast,
    SampleResult,
    WindowProposal,
    WorkOrderState,
)
from farm.engine import DRYING_HOURS, RAIN_THRESHOLD_MM, HarvestLedger
from farm.loader import load_fixture

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "harvest_window.json"
TZ = "+08:00"


def dt(text):
    return datetime.fromisoformat(text)


def make_ledger(parcels=("p-a", "p-b", "p-c")):
    return HarvestLedger(parcels)


class FixtureReplayTests(unittest.TestCase):
    def setUp(self):
        self.fx = load_fixture(FIXTURE)
        self.ledger = make_ledger(self.fx["parcels"]).replay(self.fx)
        self.as_of = self.fx["as_of"]

    def test_parcel_level_decisions(self):
        view = self.ledger.view(self.as_of)
        self.assertIs(view["field-a"].state, ParcelDecision.PAUSED)  # 安全间隔
        self.assertIs(view["field-b"].state, ParcelDecision.WAIT)    # 雨后脱水
        self.assertIs(view["field-c"].state, ParcelDecision.READY)   # 可采

    def test_phi_block_is_quality_veto_with_exact_clear_time(self):
        ev = self.ledger.evaluate("field-a", self.as_of)
        self.assertTrue(ev.gates["phi"]["blocked"])
        clear = dt(f"2026-09-24T16:00:00{TZ}")  # 09-10 16:00 + 336h
        self.assertIn(clear.strftime("%Y-%m-%d %H:%M"), ev.reasons[0])

    def test_mixed_sample_does_not_extend_scope(self):
        ev_c = self.ledger.evaluate("field-c", self.as_of)
        self.assertEqual(ev_c.covering_sample_id, "smp-c-01")  # 只认本地块单样
        self.assertIn("smp-mix-01", ev_c.gates["sample"]["mixed_samples_ignored"])

    def test_forecast_shrinks_window_but_cannot_lift_quality_veto(self):
        ev_a = self.ledger.evaluate("field-a", self.as_of)
        # 雨在 09-18，窗口终点被收缩到 06:00
        self.assertEqual(ev_a.window_end, dt(f"2026-09-18T06:00:00{TZ}"))
        # 但 field-a 仍然因安全间隔暂停，天气不能放行
        self.assertIs(ev_a.state, ParcelDecision.PAUSED)

    def test_withdrawn_release_is_preserved_in_audit_trail(self):
        trail = self.ledger.audit_trail()
        first, second = self.ledger.parcel_decisions("field-a")
        self.assertIs(first.state, ParcelDecision.READY)   # 原放行原样保留
        self.assertIs(second.state, ParcelDecision.PAUSED)  # 撤回新版本
        self.assertEqual(second.supersedes_id, first.decision_id)
        self.assertEqual(second.withdrawn_observation_id, "obs-a-input-late")
        self.assertIn(first, trail)  # 没有被删除或改写

    def test_concurrent_change_pauses_unstarted_work_order(self):
        wo = self.ledger.latest_work_order("field-a", self.as_of)
        self.assertIs(wo.state, WorkOrderState.PAUSED)
        self.assertEqual(wo.basis_version, 7)  # 09:10 重新确认时锚定 v7
        notes = [n for n in self.ledger.reconfirmations if "paused_at" in n]
        self.assertTrue(notes)  # 09:40 补录施用再次触发暂停

    def test_bitemporal_visibility(self):
        # 补录前：质量通过、作业单已重新确认 -> 可采
        before = self.ledger.evaluate("field-a", dt(f"2026-09-16T09:20:00{TZ}"))
        self.assertIs(before.state, ParcelDecision.READY)
        # 补录后：安全间隔否决 -> 暂停
        after = self.ledger.evaluate("field-a", dt(f"2026-09-16T10:10:00{TZ}"))
        self.assertIs(after.state, ParcelDecision.PAUSED)
        self.assertFalse(before.gates["phi"]["blocked"])
        self.assertTrue(after.gates["phi"]["blocked"])

    def test_drying_wait_after_sudden_rain(self):
        ev_b = self.ledger.evaluate("field-b", self.as_of)
        # 暴雨 04:00–08:00 + 24h 脱水 => 09-17 08:00 前等待
        self.assertTrue(any("脱水" in r for r in ev_b.reasons))
        later = self.ledger.evaluate("field-b", dt(f"2026-09-17T09:00:00{TZ}"))
        self.assertIs(later.state, ParcelDecision.READY)


class LateRecordVsStartedWorkTests(unittest.TestCase):
    def test_started_decision_is_not_erased_by_late_application(self):
        ledger = make_ledger(("p-a",))
        ledger.ingest_observation(
            FieldObservation("o1", "p-a", "flowering-stage", "果熟期",
                             dt("2026-09-12T09:00:00+08:00"),
                             dt("2026-09-12T09:30:00+08:00"))
        )
        ledger.ingest_sample(
            SampleResult("s1", ("p-a",), dt("2026-09-13T10:00:00+08:00"),
                         dt("2026-09-20T10:00:00+08:00"), "pass", False,
                         dt("2026-09-14T09:00:00+08:00"))
        )
        ledger.ingest_proposal(
            WindowProposal("w1", "p-a", dt("2026-09-15T08:00:00+08:00"),
                           dt("2026-09-18T18:00:00+08:00"), "技术员",
                           dt("2026-09-14T15:00:00+08:00"), 0, ())
        )
        release = ledger.release("p-a", "质检", dt("2026-09-15T08:00:00+08:00"))
        self.assertIs(release.state, ParcelDecision.READY)

        ledger.claim("p-a", dt("2026-09-15T08:05:00+08:00"), "wo-1")
        ledger.start_work_order("wo-1", dt("2026-09-15T08:30:00+08:00"))

        # 作业已开始后，迟到的施用记录到达
        ledger.ingest_application(
            InputApplication("app-late", "p-a", "多菌灵", 336,
                             dt("2026-09-10T16:00:00+08:00"),
                             dt("2026-09-16T09:00:00+08:00"))
        )
        wo = ledger.latest_work_order("p-a", dt("2026-09-16T12:00:00+08:00"))
        self.assertIs(wo.state, WorkOrderState.STARTED)  # 不被暂停/抹去
        # 原放行决定保留；新评估如实反映质量否决
        self.assertIs(ledger.latest_decision("p-a").state, ParcelDecision.READY)
        ev = ledger.evaluate("p-a", dt("2026-09-16T12:00:00+08:00"))
        self.assertIs(ev.state, ParcelDecision.PAUSED)


class SampleScopeAndValidityTests(unittest.TestCase):
    def test_mixed_sample_covers_only_listed_parcels(self):
        ledger = make_ledger(("p-a", "p-d"))
        ledger.ingest_observation(
            FieldObservation("o1", "p-d", "flowering-stage", "熟",
                             dt("2026-09-12T09:00:00+08:00"),
                             dt("2026-09-12T09:30:00+08:00"))
        )
        ledger.ingest_sample(
            SampleResult("mix", ("p-a",), dt("2026-09-13T10:00:00+08:00"),
                         dt("2026-09-20T10:00:00+08:00"), "pass", True,
                         dt("2026-09-14T09:00:00+08:00"))
        )
        ledger.ingest_proposal(
            WindowProposal("w1", "p-d", dt("2026-09-15T08:00:00+08:00"),
                           dt("2026-09-20T18:00:00+08:00"), "技术员",
                           dt("2026-09-14T15:00:00+08:00"), 0, ())
        )
        ev = ledger.evaluate("p-d", dt("2026-09-16T12:00:00+08:00"))
        self.assertFalse(ev.gates["sample"]["covered"])
        self.assertIs(ev.state, ParcelDecision.PAUSED)

    def test_expired_sample_no_longer_covers(self):
        ledger = make_ledger(("p-a",))
        ledger.ingest_sample(
            SampleResult("s1", ("p-a",), dt("2026-09-10T10:00:00+08:00"),
                         dt("2026-09-15T10:00:00+08:00"), "pass", False,
                         dt("2026-09-11T09:00:00+08:00"))
        )
        ledger.ingest_observation(
            FieldObservation("o1", "p-a", "flowering-stage", "熟",
                             dt("2026-09-10T09:00:00+08:00"),
                             dt("2026-09-10T09:30:00+08:00"))
        )
        ledger.ingest_proposal(
            WindowProposal("w1", "p-a", dt("2026-09-12T08:00:00+08:00"),
                           dt("2026-09-20T18:00:00+08:00"), "技术员",
                           dt("2026-09-11T15:00:00+08:00"), 0, ())
        )
        ev = ledger.evaluate("p-a", dt("2026-09-16T12:00:00+08:00"))
        self.assertFalse(ev.gates["sample"]["covered"])
        self.assertIs(ev.state, ParcelDecision.PAUSED)

    def test_failed_result_code_does_not_cover(self):
        ledger = make_ledger(("p-a",))
        ledger.ingest_sample(
            SampleResult("s1", ("p-a",), dt("2026-09-13T10:00:00+08:00"),
                         dt("2026-09-20T10:00:00+08:00"), "fail", False,
                         dt("2026-09-14T09:00:00+08:00"))
        )
        ev = ledger.evaluate("p-a", dt("2026-09-16T12:00:00+08:00"))
        self.assertFalse(ev.gates["sample"]["covered"])


class ReconfirmFlowTests(unittest.TestCase):
    def _ready_parcel_with_claim(self):
        ledger = make_ledger(("p-a",))
        ledger.ingest_observation(
            FieldObservation("o1", "p-a", "flowering-stage", "熟",
                             dt("2026-09-12T09:00:00+08:00"),
                             dt("2026-09-12T09:30:00+08:00"))
        )
        ledger.ingest_sample(
            SampleResult("s1", ("p-a",), dt("2026-09-13T10:00:00+08:00"),
                         dt("2026-09-20T10:00:00+08:00"), "pass", False,
                         dt("2026-09-14T09:00:00+08:00"))
        )
        ledger.ingest_proposal(
            WindowProposal("w1", "p-a", dt("2026-09-15T08:00:00+08:00"),
                           dt("2026-09-25T18:00:00+08:00"), "技术员",
                           dt("2026-09-14T15:00:00+08:00"), 0, ())
        )
        ledger.release("p-a", "质检", dt("2026-09-14T16:00:00+08:00"))
        ledger.claim("p-a", dt("2026-09-15T08:00:00+08:00"), "wo-1")
        return ledger

    def test_forecast_update_pauses_and_successful_reconfirm_resumes(self):
        ledger = self._ready_parcel_with_claim()
        ledger.ingest_forecast(
            RainfallForecast(
                "fc1", (), dt("2026-09-15T20:00:00+08:00"),
                ((dt("2026-09-16T03:00:00+08:00"), 12.0),),
            )
        )
        wo = ledger.latest_work_order("p-a", dt("2026-09-16T08:00:00+08:00"))
        self.assertIs(wo.state, WorkOrderState.PAUSED)

        ledger.reconfirm("p-a", "质检/班组", dt("2026-09-16T08:30:00+08:00"))
        wo = ledger.latest_work_order("p-a", dt("2026-09-16T09:00:00+08:00"))
        self.assertIs(wo.state, WorkOrderState.CLAIMED)
        ev = ledger.evaluate("p-a", dt("2026-09-16T09:00:00+08:00"))
        self.assertIs(ev.state, ParcelDecision.READY)

    def test_reconfirm_fails_when_quality_blocks(self):
        ledger = self._ready_parcel_with_claim()
        ledger.ingest_application(
            InputApplication("app-late", "p-a", "多菌灵", 336,
                             dt("2026-09-10T16:00:00+08:00"),
                             dt("2026-09-16T09:00:00+08:00"))
        )
        with self.assertRaises(ValueError):
            ledger.reconfirm("p-a", "质检", dt("2026-09-16T09:30:00+08:00"))
        wo = ledger.latest_work_order("p-a", dt("2026-09-16T10:00:00+08:00"))
        self.assertIs(wo.state, WorkOrderState.PAUSED)  # 维持暂停

    def test_only_paused_work_order_can_be_reconfirmed(self):
        ledger = self._ready_parcel_with_claim()
        with self.assertRaises(ValueError):
            ledger.reconfirm("p-a", "质检", dt("2026-09-16T09:00:00+08:00"))


class RainBandTests(unittest.TestCase):
    def test_bands_merge_adjacent_wet_hours(self):
        fx = RainfallForecast(
            "f", (), dt("2026-09-16T09:00:00+08:00"),
            (
                (dt("2026-09-18T06:00:00+08:00"), 8.0),
                (dt("2026-09-18T07:00:00+08:00"), 12.0),
                (dt("2026-09-18T08:00:00+08:00"), 10.0),
                (dt("2026-09-18T12:00:00+08:00"), 6.0),
            ),
        )
        bands = HarvestLedger._rain_bands(fx)
        self.assertEqual(
            bands,
            [
                (dt("2026-09-18T06:00:00+08:00"), dt("2026-09-18T09:00:00+08:00")),
                (dt("2026-09-18T12:00:00+08:00"), dt("2026-09-18T13:00:00+08:00")),
            ],
        )

    def test_below_threshold_hours_are_ignored(self):
        fx = RainfallForecast(
            "f", (), dt("2026-09-16T09:00:00+08:00"),
            ((dt("2026-09-17T08:00:00+08:00"), RAIN_THRESHOLD_MM - 0.1),),
        )
        self.assertEqual(HarvestLedger._rain_bands(fx), [])


if __name__ == "__main__":
    unittest.main()
