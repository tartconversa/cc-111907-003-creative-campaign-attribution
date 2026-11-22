"""测试用可控合成资料生成器（与仓库 inputs 同契约，独立构造）。"""

from __future__ import annotations

import json
from pathlib import Path

SCHEMA = {
    "regions": {"definition": "", "columns": {
        "region": "", "baseline_customers": "", "group": ""}},
    "campaigns": {"definition": "", "columns": {
        "campaign": "", "channel": "", "exclusive_family": "", "start": "", "end": "",
        "max_budget_fen": ""}},
    "creatives": {"definition": "", "columns": {
        "creative": "", "campaign": "", "revision": "", "live": ""}},
    "touches": {"definition": "", "columns": {
        "id": "", "person": "", "region": "", "creative": "", "occurred": "", "source": ""}},
    "orders": {"definition": "", "columns": {
        "id": "", "person": "", "region": "", "ordered": "", "received": "", "revenue": "",
        "cost": "", "refund": ""}},
    "contracts": {"definition": "", "columns": {
        "id": "", "campaign": "", "region": "", "amount_fen": "", "status": "",
        "cancellable": ""}},
    "observation_policy": {"definition": "", "columns": {
        "id": "", "maturity_days": "", "confidence": "", "currency": ""}},
}


def _orders_for(region: str, prefix: str, start_day: int, revenue: int,
                cost: int, refund: int, count: int = 4) -> list[dict]:
    rows = []
    for index in range(count):
        day = start_day + index
        rows.append({
            "id": f"{prefix}{day:02d}",
            "person": f"P-{prefix}-{day}",
            "region": region,
            "ordered": f"2026-01-{day:02d}T10:00:00Z",
            "received": f"2026-01-{day + 1:02d}T00:00:00Z",
            "revenue": str(revenue),
            "cost": str(cost),
            "refund": str(refund),
        })
    return rows


def make_inputs(
    root: Path,
    *,
    treatment_regions=("R1", "R2", "R3", "R4"),
    control_regions=("R5", "R6", "R7", "R8"),
    treatment_revenue=500,
    control_revenue=300,
    treatment_cost=100,
    control_cost=100,
    treatment_refund=0,
    control_refund=0,
    baselines=None,
    treatment_paid=True,
    control_paid=False,
    include_other_paid=False,
    other_paid_region="R1",
    extra_orders=None,
    order_count=4,
    order_start_day=2,
) -> Path:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "schema.json").write_text(json.dumps(SCHEMA), encoding="utf-8")
    regions = []
    for region in list(treatment_regions) + list(control_regions):
        # 相似层按地区内编号交错划分，保证每层同时含处理组和对照组。
        index = int(region[1:]) if region[1:].isdigit() else 0
        stratum = "A" if index % 2 == 1 else "B"
        baseline = (baselines or {}).get(region, 1000)
        regions.append({"region": region, "baseline_customers": baseline, "group": stratum})
    campaigns = [
        {"campaign": "ad", "channel": "video", "exclusive_family": "f1",
         "start": "2026-01-01T00:00:00Z", "end": "2026-02-01T00:00:00Z",
         "max_budget_fen": 200000},
        {"campaign": "other", "channel": "search", "exclusive_family": "f1",
         "start": "2026-01-01T00:00:00Z", "end": "2026-02-01T00:00:00Z",
         "max_budget_fen": 100000},
    ]
    creatives = [
        {"creative": "A1", "campaign": "ad", "revision": 1, "live": "2026-01-01T00:00:00Z"},
        {"creative": "O1", "campaign": "other", "revision": 1, "live": "2026-01-01T00:00:00Z"},
    ]
    touches = []
    counter = 0
    for region in treatment_regions:
        if treatment_paid:
            touches.append({
                "id": f"T{counter:03d}", "person": f"u-{region}", "region": region,
                "creative": "A1", "occurred": "2026-01-02T00:00:00Z", "source": "paid",
            })
            counter += 1
        touches.append({
            "id": f"T{counter:03d}", "person": f"u-{region}", "region": region,
            "creative": "A1", "occurred": "2026-01-03T00:00:00Z", "source": "organic",
        })
        counter += 1
    for region in control_regions:
        if control_paid:
            touches.append({
                "id": f"T{counter:03d}", "person": f"u-{region}", "region": region,
                "creative": "A1", "occurred": "2026-01-02T00:00:00Z", "source": "paid",
            })
            counter += 1
        touches.append({
            "id": f"T{counter:03d}", "person": f"u-{region}", "region": region,
            "creative": "A1", "occurred": "2026-01-03T00:00:00Z", "source": "organic",
        })
        counter += 1
    if include_other_paid:
        touches.append({
            "id": f"T{counter:03d}", "person": f"u-{other_paid_region}", "region": other_paid_region,
            "creative": "O1", "occurred": "2026-01-04T00:00:00Z", "source": "paid",
        })
        counter += 1

    orders = []
    day = order_start_day
    for region in treatment_regions:
        orders.extend(_orders_for(region, f"{region}-", day, treatment_revenue,
                                  treatment_cost, treatment_refund, order_count))
        day += 1
    for region in control_regions:
        orders.extend(_orders_for(region, f"{region}-", day, control_revenue,
                                  control_cost, control_refund, order_count))
        day += 1
    if extra_orders:
        orders.extend(extra_orders)
    contracts = [
        {"id": "C-sign", "campaign": "ad", "region": "R1", "amount_fen": 50000,
         "status": "signed", "cancellable": False},
        {"id": "C-exec", "campaign": "other", "region": "R8", "amount_fen": 20000,
         "status": "executed", "cancellable": False},
        {"id": "C-cancel", "campaign": "ad", "region": "R2", "amount_fen": 99999,
         "status": "signed", "cancellable": True},
    ]
    observations = [
        {"id": "default", "maturity_days": 7, "confidence": 0.95, "currency": "CNY"},
    ]
    for name, rows in (
        ("regions", regions), ("campaigns", campaigns), ("creatives", creatives),
        ("touches", touches), ("orders", orders), ("contracts", contracts),
        ("observation_policy", observations),
    ):
        (root / f"{name}.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    return root
