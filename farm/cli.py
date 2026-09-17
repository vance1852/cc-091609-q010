"""命令行入口：查看地块状态与回放演示场景。

用法：
    python -m farm.cli view   [--as-of 2026-09-17T08:00:00+08:00] [field-a ...]
    python -m farm.cli demo
"""

import argparse
from datetime import datetime

from .loader import load_fixture
from .service import HarvestService
from .views import render_text

DEFAULT_FIXTURE = "fixtures/harvest_window.json"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="黄芩采收窗口决策平台")
    sub = parser.add_subparsers(dest="cmd", required=True)

    view_p = sub.add_parser("view", help="按地块查看 可采/等待/暂停 与理由")
    view_p.add_argument("parcels", nargs="*", help="地块编号，缺省为全部地块")
    view_p.add_argument("--fixture", default=DEFAULT_FIXTURE)
    view_p.add_argument("--as-of", dest="as_of", default=None,
                        help="评估时点（ISO8601），缺省取夹具中最后录入时间")

    demo_p = sub.add_parser("demo", help="回放放行被迟到记录撤回的完整过程")
    demo_p.add_argument("--fixture", default=DEFAULT_FIXTURE)

    args = parser.parse_args(argv)

    if args.cmd == "demo":
        from .demo import run
        print(run())
        return

    ledger, rules, data = load_fixture(args.fixture)
    svc = HarvestService(ledger, rules)
    if args.as_of:
        as_of = datetime.fromisoformat(args.as_of)
    else:
        times = [o.recorded_at for o in ledger._observations]  # noqa: SLF001
        times += [f.issued_at for f in ledger._forecasts]
        as_of = max(times) if times else datetime.now().astimezone()

    parcel_ids = args.parcels or list(ledger.parcels)
    for pid in parcel_ids:
        print(render_text(svc.view(pid, as_of)))
        print()


if __name__ == "__main__":
    main()
