"""夹具级集成测试：加载 harvest_window.json，验证撤回过程与历史时点回放。"""

import unittest
from datetime import datetime

from farm.contracts import ParcelDecision
from farm.loader import load_fixture
from farm.models import WorkOrderState
from farm.service import HarvestService

FIXTURE = "fixtures/harvest_window.json"


def ts(v: str) -> datetime:
    return datetime.fromisoformat(v)


class FixtureIntegrationTests(unittest.TestCase):
    def test_late_input_withdraws_claimed_release(self):
        ledger, rules, _ = load_fixture(FIXTURE)
        order = next(iter(ledger.orders.values()))
        self.assertEqual(order.parcel_id, "field-a")
        self.assertIs(order.state, WorkOrderState.AWAITING_RECONFIRM)
        self.assertEqual(order.confirmed_decision_id, "field-a-D2")

        chain = ledger.decision_chain("field-a")
        self.assertEqual([d.version for d in chain], [1, 2, 3])
        self.assertIs(chain[1].state, ParcelDecision.READY)
        self.assertEqual(chain[1].released_by, "fixture-qa")
        self.assertIs(chain[2].state, ParcelDecision.PAUSED)
        self.assertIsNone(chain[2].released_by)
        self.assertEqual(chain[2].supersedes_id, "field-a-D2")

    def test_mixed_sample_scope_using_fixture(self):
        ledger, rules, _ = load_fixture(FIXTURE)
        svc = HarvestService(ledger, rules)
        # field-c 物候成熟但不在混合样覆盖范围 → 暂停
        self.assertIs(
            svc.view("field-c", ts("2026-09-17T06:00:00+08:00")).state,
            ParcelDecision.PAUSED,
        )
        self.assertIsNone(
            ledger.effective_sample(
                "field-c", ts("2026-09-17T06:00:00+08:00"),
                ts("2026-09-17T06:00:00+08:00"),
            )
        )

    def test_bitemporal_history_replay_field_a_was_ready(self):
        # 迟到记录 09-16 08:30 才录入：在 09-15 评估时它不可见，field-a 本应可采
        ledger, rules, _ = load_fixture(FIXTURE, materialize_orders=False)
        svc = HarvestService(ledger, rules)
        past = svc.view("field-a", ts("2026-09-15T12:00:00+08:00"))
        self.assertIs(past.state, ParcelDecision.READY)
        now = svc.view("field-a", ts("2026-09-17T06:00:00+08:00"))
        self.assertIs(now.state, ParcelDecision.PAUSED)

    def test_heavy_rain_field_b_waits_for_drydown(self):
        ledger, rules, _ = load_fixture(FIXTURE, materialize_orders=False)
        svc = HarvestService(ledger, rules)
        view = svc.view("field-b", ts("2026-09-16T12:00:00+08:00"))
        self.assertIs(view.state, ParcelDecision.WAIT)
        self.assertTrue(
            view.window_start >= ts("2026-09-18T04:00:00+08:00")
        )


if __name__ == "__main__":
    unittest.main()
