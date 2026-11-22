"""观察性描述与组随机化分析。

两类输出严格分开：

``describe``
    只描述触达路径、创意版本、渠道、获客批次与订单净贡献的并存关系。
    不把任何净贡献分摊给渠道，也不输出“广告带来的增量”。

``design / review``
    基于事先登记并冻结的对照分配做组随机化推断。只有通过成熟度、平衡、
    覆盖与污染稳健性守卫，且结果在预先指定口径下显著为正时，才标记
    ``auto_win_eligible``；应用不会自动替负责人做“判胜”决定。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import random
from typing import Any, Iterable

from . import stats
from .errors import DomainError
from .source import SourceData
from .timeutil import add_days, format_ts, parse_ts

ALGORITHM = "campaign_app.stratified_block.v1"

DEFAULT_THRESHOLDS = {
    "min_treatment_coverage": 0.80,
    "max_control_leakage": 0.10,
    "min_mature_share": 0.80,
    "max_baseline_smd": 0.50,
    "max_arm_ratio": 2.00,
    "min_units_per_arm": 2,
}


class DesignError(DomainError):
    pass


class ReviewError(DomainError):
    pass


# ---------------------------------------------------------------------------
# 描述性输出
# ---------------------------------------------------------------------------

def _iso_week(value: datetime) -> str:
    year, week, _ = value.isocalendar()
    return f"{year}-W{week:02d}"


def describe(data: SourceData, cutoff: datetime, *, campaign: str | None = None) -> dict[str, Any]:
    """描述性路径汇总；不进行任何收入分摊或因果归因。"""
    touches = data.touches_as_of(cutoff)
    orders = data.orders_as_of(cutoff)
    order_by_person: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for order in orders:
        order_by_person[order["person"]].append(order)

    creative_campaign = {}
    creative_revision = {}
    for creative, versions in data.creatives.items():
        live_versions = [row for row in versions if row["_live"] is not None and row["_live"] <= cutoff]
        if not live_versions:
            live_versions = versions
        chosen = sorted(live_versions, key=lambda row: (row["revision"], row["_live"]))[-1]
        creative_campaign[creative] = chosen["campaign"]
        creative_revision[creative] = chosen["revision"]

    filtered = [
        touch for touch in touches
        if creative_campaign.get(touch["creative"]) is not None
        and (campaign is None or creative_campaign.get(touch["creative"]) == campaign)
    ]

    persons = sorted({touch["person"] for touch in filtered})
    first_touch: dict[str, datetime] = {}
    channel_set: dict[str, set[str]] = defaultdict(set)
    for touch in filtered:
        current = first_touch.get(touch["person"])
        if current is None or touch["_occurred"] < current:
            first_touch[touch["person"]] = touch["_occurred"]
        campaign_name = creative_campaign[touch["creative"]]
        channel = data.campaigns[campaign_name]["channel"]
        channel_set[touch["person"]].add(channel)

    cohort_of = {person: _iso_week(first_touch[person]) for person in persons}

    persons_with_orders = [person for person in persons if order_by_person.get(person)]
    cross_channel = [person for person in persons if len(channel_set[person]) > 1]
    cross_region_persons = []
    for person in persons:
        regions = {touch["region"] for touch in filtered if touch["person"] == person}
        if len(regions) > 1:
            cross_region_persons.append(person)

    by_channel: dict[str, dict[str, Any]] = {}
    first_channel: dict[str, str] = {}
    for touch in filtered:
        if touch["_occurred"] == first_touch[touch["person"]]:
            campaign_name = creative_campaign[touch["creative"]]
            first_channel.setdefault(touch["person"], data.campaigns[campaign_name]["channel"])
    for person in persons:
        net_values = [order["_net"] for order in order_by_person.get(person, []) if order["_net"] is not None]
        channels = sorted(channel_set[person])
        for channel in channels:
            bucket = by_channel.setdefault(channel, {
                "channel": channel,
                "persons_reached": 0,
                "persons_first_touch": 0,
                "persons_with_orders": 0,
                "orders": 0,
                "net_contribution_first_touch": 0.0,
                "persons_with_orders_not_first_touch": 0,
                "unknown_net_orders": 0,
                "note": (
                    "按个人首个触达渠道归属其订单净贡献；仅用于描述人群构成，"
                    "不按渠道分摊收入，也不构成增量"
                ),
            })
            bucket["persons_reached"] += 1
            if channel == first_channel[person]:
                bucket["persons_first_touch"] += 1
                if order_by_person.get(person):
                    bucket["persons_with_orders"] += 1
                    bucket["orders"] += len(order_by_person[person])
                    bucket["net_contribution_first_touch"] += float(sum(net_values))
                bucket["unknown_net_orders"] += sum(
                    1 for order in order_by_person.get(person, []) if order["_net"] is None
                )
            elif order_by_person.get(person):
                bucket["persons_with_orders_not_first_touch"] += 1

    # 创意版本 × 渠道 × 获客批次
    by_creative: dict[tuple[str, str, str], dict[str, Any]] = {}
    for touch in filtered:
        campaign_name = creative_campaign[touch["creative"]]
        channel = data.campaigns[campaign_name]["channel"]
        revision = creative_revision[touch["creative"]]
        cohort = cohort_of[touch["person"]]
        key = (campaign_name, touch["creative"], cohort)
        bucket = by_creative.setdefault(key, {
            "campaign": campaign_name,
            "creative": touch["creative"],
            "revision": revision,
            "channel": channel,
            "cohort": cohort,
            "paid_touches": 0,
            "organic_touches": 0,
            "persons": set(),
        })
        if touch["source"] == "paid":
            bucket["paid_touches"] += 1
        else:
            bucket["organic_touches"] += 1
        bucket["persons"].add(touch["person"])

    by_creative_rows = []
    for key in sorted(by_creative):
        bucket = by_creative[key]
        people = sorted(bucket.pop("persons"))
        bucket["persons_reached"] = len(people)
        bucket["persons_with_orders"] = sum(1 for person in people if order_by_person.get(person))
        bucket["orders"] = sum(len(order_by_person.get(person, [])) for person in people)
        bucket["net_contribution"] = float(sum(
            order["_net"]
            for person in people
            for order in order_by_person.get(person, [])
            if order["_net"] is not None
        ))
        bucket["unknown_net_orders"] = sum(
            1 for person in people for order in order_by_person.get(person, []) if order["_net"] is None
        )
        by_creative_rows.append(bucket)

    # 同一用户跨渠道的重叠矩阵
    channel_names = sorted({data.campaigns[name]["channel"] for name in data.campaigns})
    overlap = {left: {right: 0 for right in channel_names} for left in channel_names}
    for person in persons:
        channels = sorted(channel_set[person])
        for left in channels:
            for right in channels:
                overlap[left][right] += 1

    all_touches = [touch for touch in data.touches_as_of(cutoff)
                   if campaign is None or creative_campaign.get(touch["creative"]) == campaign]
    duplicate_touches = sum(
        1 for touch in all_touches if touch.get("_alias_of")
    )

    return {
        "kind": "descriptive",
        "causal_claim": "none",
        "statement": (
            "以下为观察性路径描述：触达不是随机分配，自然流量、节假日促销和地区基线"
            "都可能同时影响结果。任何渠道或创意的人群净贡献合计都不是广告增量。"
        ),
        "cutoff": format_ts(cutoff),
        "campaign": campaign,
        "dimension_notes": {
            "cohort": "获客批次按个人首个触达所在 ISO 周定义",
            "creative_revision": "创意按触达时点之前最近上线的版本解释",
            "channel": "渠道来自活动定义",
        },
        "persons_reached": len(persons),
        "persons_with_orders": len(persons_with_orders),
        "cross_channel_persons": len(cross_channel),
        "cross_region_persons": len(cross_region_persons),
        "duplicate_touch_rows_ignored": duplicate_touches,
        "unknown_net_orders": sum(1 for order in orders if order["_net"] is None),
        "by_channel": [by_channel[name] for name in sorted(by_channel)],
        "by_creative": by_creative_rows,
        "channel_overlap": overlap,
        "not_incremental": True,
    }


# ---------------------------------------------------------------------------
# 单位构建
# ---------------------------------------------------------------------------

@dataclass
class UnitContext:
    unit_id: str
    stratum: str
    baseline: float
    region: str
    paid_touches: int
    organic_touches: int
    orders_known: int
    orders_mature: int
    raw_net: float | None
    unknown_net_orders: int
    contaminated: bool
    contamination: list[dict[str, Any]]


def _paid_campaign_touches(
    data: SourceData,
    campaign: str,
    cutoff: datetime,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    creative_campaign = {
        creative: versions[0]["campaign"] for creative, versions in data.creatives.items() if versions
    }
    return [
        touch for touch in data.touches_as_of(cutoff)
        if creative_campaign.get(touch["creative"]) == campaign
        and start <= touch["_occurred"] < end
    ]


def _campaigns_overlapping(
    data: SourceData,
    campaign: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    target = data.campaigns[campaign]
    rows = []
    for other_name, other in sorted(data.campaigns.items()):
        if other_name == campaign:
            continue
        try:
            other_start = parse_ts(other["start"])
            other_end = parse_ts(other["end"])
        except (KeyError, ValueError):
            continue
        if other_start < end and start < other_end:
            rows.append({
                "campaign": other_name,
                "exclusive": other.get("exclusive_family") == target.get("exclusive_family"),
                "family": other.get("exclusive_family"),
                "start": format_ts(other_start),
                "end": format_ts(other_end),
                "overlap_start": format_ts(max(other_start, start)),
                "overlap_end": format_ts(min(other_end, end)),
            })
    return rows


def _contracts_by_region(data: SourceData) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in data.contracts.values():
        if row.get("status") in {"signed", "executed"}:
            grouped[(row["campaign"], row["region"])].append(row)
    return grouped


def _workspace_plans(workspace_state: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not workspace_state:
        return []
    plans = []
    for plan in workspace_state.get("plans", {}).values():
        if plan.get("status") in {"confirmed", "executed"}:
            plans.append(plan)
    for execution in workspace_state.get("executions", {}).values():
        if execution.get("status") in {"signed", "executed"}:
            plans.append({
                "id": execution.get("id"),
                "campaign": execution.get("campaign"),
                "allocations": execution.get("allocations", []),
                "regions": execution.get("regions", []),
                "start": execution.get("start"),
                "end": execution.get("end"),
            })
    return plans


def _room_occupancy(
    data: SourceData,
    campaign: str,
    region: str,
    start: datetime,
    end: datetime,
    cutoff: datetime,
    workspace_state: dict[str, Any] | None,
) -> dict[str, list[dict[str, Any]]]:
    """返回该地区在观察窗口内的两类活动证据。

    ``interfering``
        不同活动中，同一地区确有付费触达、已签约/已执行合同或已确认方案，
        且日期重叠者。只有这类活动才真正改变该地区的结果。
    ``scheduled_elsewhere``
        同互斥组、日期重叠，却没有任何本地区执行证据的“挂名”活动。
        它们没有改变本地区结果，但这些活动自己的控制组可能被污染。
    """
    creative_campaign = {
        creative: versions[0]["campaign"] for creative, versions in data.creatives.items() if versions
    }
    paid_by_campaign: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for touch in data.touches_as_of(cutoff):
        other = creative_campaign.get(touch["creative"])
        if other and other != campaign and touch["source"] == "paid":
            paid_by_campaign[other].append(touch)
    contracts = _contracts_by_region(data)
    plans = _workspace_plans(workspace_state)

    interfering: list[dict[str, Any]] = []
    scheduled: list[dict[str, Any]] = []
    for other in _campaigns_overlapping(data, campaign, start, end):
        other_name = other["campaign"]
        overlap_start = parse_ts(other["overlap_start"])
        overlap_end = parse_ts(other["overlap_end"])
        evidence: list[str] = []
        touches = [
            touch for touch in paid_by_campaign.get(other_name, [])
            if overlap_start <= touch["_occurred"] < overlap_end and touch["region"] == region
        ]
        if touches:
            evidence.append(f"{len(touches)} 次付费触达")
        contract_rows = contracts.get((other_name, region), [])
        if contract_rows:
            evidence.append(
                f"{len(contract_rows)} 份合同（{','.join(sorted(row['id'] for row in contract_rows))}）"
            )
        plan_rows = [
            plan for plan in plans
            if plan.get("campaign") == other_name
            and region in (
                plan.get("regions")
                or [allocation.get("region") for allocation in plan.get("allocations", [])]
            )
        ]
        if plan_rows:
            evidence.append(f"{len(plan_rows)} 个已确认/执行方案")
        row = {
            "campaign": other_name,
            "exclusive": other["exclusive"],
            "family": other["family"],
            "overlap_start": other["overlap_start"],
            "overlap_end": other["overlap_end"],
            "evidence": evidence,
        }
        if evidence:
            interfering.append(row)
        elif other["exclusive"]:
            scheduled.append({
                **row,
                "evidence": ["该地区无执行证据；但同互斥组的计划可能污染对照"],
            })
    return {"interfering": interfering, "scheduled_elsewhere": scheduled}


def _home_region(person: str, touches_by_person: dict[str, list[dict[str, Any]]],
                 orders_by_person: dict[str, list[dict[str, Any]]]) -> str | None:
    """用户归属地区：最早触达的地区；没有触达时用最早已知订单的地区。"""
    person_touches = touches_by_person.get(person, [])
    if person_touches:
        first = min(person_touches, key=lambda row: (row["_occurred"], row["region"], row["id"]))
        return first["region"]
    person_orders = [order for order in orders_by_person.get(person, []) if order["_ordered"] is not None]
    if person_orders:
        first = min(person_orders, key=lambda row: (row["_ordered"], row["region"], row["id"]))
        return first["region"]
    return None


def _realized_net(
    order: dict[str, Any],
    maturity_days: int,
    refund_maturity_days: int,
    cutoff: datetime,
) -> float:
    """在截止点下已实现的订单净贡献：取消成熟后才计入订单；退款成熟后才扣除退款。"""
    revenue = float(order["_revenue"])
    cost = float(order["_cost"])
    refund = float(order["_refund"])
    ordered = order["_ordered"]
    net = revenue - cost
    if ordered is not None and add_days(ordered, refund_maturity_days) <= cutoff:
        net -= refund
    return net


def build_units(
    data: SourceData,
    design: dict[str, Any],
    cutoff: datetime,
    *,
    workspace_state: dict[str, Any] | None = None,
    exclude_units: Iterable[str] = (),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    unit_type = design["unit_type"]
    start = parse_ts(design["planned_start"])
    end = parse_ts(design["planned_end"])
    maturity = int(design["maturity_days"])
    refund_maturity = int(design.get("refund_maturity_days", maturity))
    campaign = design["campaign"]
    excluded = set(exclude_units)

    orders = data.orders_as_of(cutoff)
    touches = data.touches_as_of(cutoff)
    creative_campaign = {
        creative: versions[0]["campaign"] for creative, versions in data.creatives.items() if versions
    }
    campaign_touches = [
        touch for touch in touches
        if creative_campaign.get(touch["creative"]) == campaign
        and start <= touch["_occurred"] < end
    ]
    paid_by_region: dict[str, list[dict[str, Any]]] = defaultdict(list)
    organic_by_region: dict[str, list[dict[str, Any]]] = defaultdict(list)
    paid_by_person: dict[str, list[dict[str, Any]]] = defaultdict(list)
    organic_by_person: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for touch in campaign_touches:
        target = paid_by_person if touch["source"] == "paid" else organic_by_person
        target[touch["person"]].append(touch)
        target = paid_by_region if touch["source"] == "paid" else organic_by_region
        target[touch["region"]].append(touch)

    orders_by_region: dict[str, list[dict[str, Any]]] = defaultdict(list)
    orders_by_person: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for order in orders:
        if order["_ordered"] is None:
            continue
        if start <= order["_ordered"] < end:
            orders_by_region[order["region"]].append(order)
            orders_by_person[order["person"]].append(order)

    touches_by_person: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for touch in touches:
        touches_by_person[touch["person"]].append(touch)

    allocation = design["allocation"]["assignment"]
    units: list[dict[str, Any]] = []
    for unit_id in sorted(allocation):
        if unit_id in excluded:
            continue
        arm = allocation[unit_id]
        if unit_type == "region":
            region = unit_id
            region_row = data.regions.get(unit_id)
            if region_row is None:
                continue
            stratum = str(region_row.get("group", "unknown"))
            baseline = region_row.get("baseline_customers")
            baseline_value = float(baseline) if isinstance(baseline, (int, float)) and not isinstance(baseline, bool) else 0.0
            unit_orders = orders_by_region.get(region, [])
            paid = paid_by_region.get(region, [])
            organic = organic_by_region.get(region, [])
            person = None
        else:
            person = unit_id
            region = design["allocation"].get("unit_regions", {}).get(person)
            if region is None:
                region = _home_region(person, touches_by_person, orders_by_person)
            if region is None:
                continue
            region_row = data.regions.get(region, {})
            stratum = str(region_row.get("group", "unknown"))
            baseline_value = 1.0
            unit_orders = orders_by_person.get(person, [])
            paid = paid_by_person.get(person, [])
            organic = organic_by_person.get(person, [])

        known = [order for order in unit_orders]
        # 取消（能否成交）与部分退款有各自的成熟期：订单需先过取消成熟期，
        # 退款金额需再过退款成熟期才被视为已实现。
        mature = [
            order for order in known
            if order["_ordered"] is not None and add_days(order["_ordered"], maturity) <= cutoff
        ]
        unknown_net = sum(1 for order in known if order["_net"] is None)
        if unknown_net:
            raw_net: float | None = None
        else:
            raw_net = float(sum(_realized_net(order, maturity, refund_maturity, cutoff)
                                for order in mature))

        room = _room_occupancy(
            data, campaign, region, start, end, cutoff, workspace_state,
        )
        contamination = room["interfering"]
        units.append({
            "unit": unit_id,
            "arm": arm,
            "stratum": stratum,
            "region": region,
            "baseline": baseline_value,
            "paid_touches": len(paid),
            "organic_touches": len(organic),
            "orders_known": len(known),
            "orders_mature": len(mature),
            "orders_refund_mature": sum(
                1 for order in mature
                if order["_ordered"] is not None
                and add_days(order["_ordered"], refund_maturity) <= cutoff
            ),
            "raw_net": raw_net,
            "unknown_net_orders": unknown_net,
            "contaminated": bool(contamination),
            "contamination": contamination,
            "scheduled_elsewhere": room["scheduled_elsewhere"],
        })

    summary = {
        "eligible_units": len(design["allocation"]["assignment"]),
        "analysed_units": len(units),
        "excluded_units": sorted(excluded),
        "units_by_arm": {
            "treatment": sum(1 for unit in units if unit["arm"] == "treatment"),
            "control": sum(1 for unit in units if unit["arm"] == "control"),
        },
        "orders_known": sum(unit["orders_known"] for unit in units),
        "orders_mature": sum(unit["orders_mature"] for unit in units),
        "unknown_net_orders": sum(unit["unknown_net_orders"] for unit in units),
        "refund_mature_orders": sum(unit.get("orders_refund_mature", 0) for unit in units),
        "contaminated_units": sorted(unit["unit"] for unit in units if unit["contaminated"]),
        "allocation_fingerprint": design["allocation"]["fingerprint"],
    }
    return units, summary


# ---------------------------------------------------------------------------
# 设计登记与冻结
# ---------------------------------------------------------------------------

def _assignment_fingerprint(design_core: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(design_core, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def _eligible_units(
    data: SourceData,
    unit_type: str,
    regions: list[str] | None,
    *,
    as_of: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    if unit_type == "region":
        selected = set(regions) if regions else set(data.regions)
        unknown = selected - set(data.regions)
        if unknown:
            raise DesignError(f"未知地区: {sorted(unknown)}")
        if not selected:
            raise DesignError("没有可分配地区")
        return {
            region: {
                "stratum": str(data.regions[region].get("group", "unknown")),
                "baseline": data.regions[region].get("baseline_customers"),
            }
            for region in sorted(selected)
        }

    if unit_type == "person":
        # 用户群实验的合格名单必须由负责人在投放前登记（external_assignment）。
        # 若从投放后的触达反推名单，会把处理本身带进分组，因此这里拒绝自动生成。
        raise DesignError(
            "用户群单位必须通过 --assignment 登记投放前的合格名单与分组；"
            "不能用投放后的触达反推名单"
        )

    raise DesignError("unit_type 必须是 region 或 person")


def person_roster(
    data: SourceData,
    persons: Iterable[str],
    *,
    before: datetime,
) -> dict[str, dict[str, Any]]:
    """登记用户群名单的相似层：只看投放前的地区归属；未知时归入 all。"""
    touches_by_person: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for touch in data.touches:
        if touch["_occurred"] is not None and touch["_occurred"] < before:
            touches_by_person[touch["person"]].append(touch)
    orders_by_person: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for order in data.orders_as_of(before):
        if order["_ordered"] is not None and order["_ordered"] < before:
            orders_by_person[order["person"]].append(order)
    roster = {}
    for person in sorted(set(persons)):
        region = _home_region(person, touches_by_person, orders_by_person)
        stratum = str(data.regions.get(region, {}).get("group", "unknown")) if region else "unknown"
        roster[person] = {
            "stratum": stratum,
            "baseline": 1.0,
            "region": region,
        }
    return roster


def allocate(
    eligible: dict[str, dict[str, Any]],
    *,
    seed: int,
    salt: str,
) -> dict[str, str]:
    """按相似层做基线配对区块随机分配；所有随机源可复现。

    每个相似层内按常规客群规模排序，相邻单位配成对，再对每对随机抛硬币。
    相比整层简单随机，配对能显著降低随机失衡，同时仍是随机分配；
    若层内单位数为奇数，剩下的一个单位随机归入一组，随机源仍由 seed 决定。
    """
    strata: dict[str, list[str]] = defaultdict(list)
    for unit, info in eligible.items():
        strata[str(info["stratum"])].append(unit)
    assignment: dict[str, str] = {}
    for stratum in sorted(strata):
        units = sorted(
            strata[stratum],
            key=lambda unit: (
                float(eligible[unit].get("baseline") or 0.0)
                if isinstance(eligible[unit].get("baseline"), (int, float))
                and not isinstance(eligible[unit].get("baseline"), bool) else 0.0,
                unit,
            ),
        )
        rng = random.Random(f"{seed}|{salt}|{stratum}")
        index = 0
        while index + 1 < len(units):
            left, right = units[index], units[index + 1]
            if rng.random() < 0.5:
                assignment[left], assignment[right] = "treatment", "control"
            else:
                assignment[left], assignment[right] = "control", "treatment"
            index += 2
        if index < len(units):
            assignment[units[index]] = "treatment" if rng.random() < 0.5 else "control"
    return dict(sorted(assignment.items()))


def _validate_assignment(eligible: dict[str, dict[str, Any]], assignment: dict[str, str]) -> None:
    if set(assignment) != set(eligible):
        raise DesignError("分配必须恰好覆盖全部合格单位，不能事后增删")
    for unit, arm in assignment.items():
        if arm not in {"treatment", "control"}:
            raise DesignError(f"单位 {unit} 的分组必须是 treatment/control")
    strata: dict[str, list[str]] = defaultdict(list)
    for unit, info in eligible.items():
        strata[str(info["stratum"])].append(unit)
    for stratum, units in sorted(strata.items()):
        arms = {assignment[unit] for unit in units}
        if arms != {"treatment", "control"}:
            raise DesignError(f"相似层 {stratum} 必须同时包含处理组与对照组")
        if len(units) < 4:
            raise DesignError(f"相似层 {stratum} 至少需要 4 个单位，当前 {len(units)}")
    treated = sum(1 for arm in assignment.values() if arm == "treatment")
    control = len(assignment) - treated
    if treated < 2 or control < 2:
        raise DesignError("处理组和对照组各至少需要 2 个单位")


def create_design(
    data: SourceData,
    *,
    design_id: str,
    campaign: str,
    unit_type: str,
    planned_start: str,
    planned_end: str,
    observation_end: str,
    created_at: datetime,
    seed: int,
    allocation_basis: str,
    analysis_spec: str = "stratified_baseline",
    normalization: str = "per_1000_baseline",
    alpha: float = 0.05,
    confidence: float = 0.95,
    maturity_days: int | None = None,
    refund_maturity_days: int | None = None,
    max_looks: int = 1,
    regions: list[str] | None = None,
    budget_cap_fen: int | None = None,
    external_assignment: dict[str, str] | None = None,
    thresholds: dict[str, float] | None = None,
    notes: str = "",
    late_registration: bool = False,
) -> dict[str, Any]:
    if campaign not in data.campaigns:
        raise DesignError(f"未知活动: {campaign}")
    if analysis_spec not in stats.SPEC_LIBRARY:
        raise DesignError(f"未知分析口径: {analysis_spec}")
    if normalization not in {"total", "per_1000_baseline"}:
        raise DesignError("归一化口径必须是 total 或 per_1000_baseline")
    if unit_type == "person" and normalization == "per_1000_baseline":
        raise DesignError("用户群单位没有常规客群基数，请使用 total")
    if not (0 < alpha < 1):
        raise DesignError("alpha 必须在 (0,1) 内")
    if not (0 < confidence < 1):
        raise DesignError("confidence 必须在 (0,1) 内")
    if max_looks < 1:
        raise DesignError("max_looks 至少为 1")
    if not allocation_basis.strip():
        raise DesignError("必须登记随机分配依据，不能留空")
    start = parse_ts(planned_start, field="planned_start")
    end = parse_ts(planned_end, field="planned_end")
    obs_end = parse_ts(observation_end, field="observation_end")
    if not (start < end <= obs_end):
        raise DesignError("必须满足 planned_start < planned_end <= observation_end")
    policy = data.policy("default")
    maturity = int(policy["maturity_days"]) if maturity_days is None else int(maturity_days)
    refund_maturity = maturity if refund_maturity_days is None else int(refund_maturity_days)
    if maturity < 0 or refund_maturity < 0:
        raise DesignError("成熟期不能为负")
    if refund_maturity < maturity:
        raise DesignError("退款成熟期不能短于取消成熟期")
    if obs_end < add_days(end, refund_maturity):
        raise DesignError("观察截止点必须覆盖计划终点加退款成熟期，否则退款可能尚未实现")
    # 投放已经开始后才登记设计，不可能是“事先规定”的确认性研究。
    # 允许登记，但必须显式承认，并永久标记为非确认性。
    registered_after_exposure = created_at >= start
    if registered_after_exposure and not late_registration:
        raise DesignError(
            "计划起点早于或等于登记时间：这是投放后才登记的回顾性设计，"
            "不能声称为事先规定。如确需登记，请显式承认（--late-registration）并接受非确认性标记"
        )
    campaign_row = data.campaigns[campaign]
    cap = int(campaign_row.get("max_budget_fen") or 0) if budget_cap_fen is None else int(budget_cap_fen)
    if cap <= 0:
        raise DesignError("预算上限必须为正")
    if cap > int(campaign_row.get("max_budget_fen") or 0):
        raise DesignError("登记预算不能超过活动预算上限")

    eligible = _eligible_units(data, unit_type, regions)
    if external_assignment is not None:
        if unit_type == "person":
            eligible = person_roster(data, external_assignment, before=created_at)
        assignment = dict(external_assignment)
        _validate_assignment(eligible, assignment)
        method = "external_registered"
        seed_value = None
    else:
        assignment = allocate(eligible, seed=seed, salt=campaign)
        _validate_assignment(eligible, assignment)
        method = ALGORITHM
        seed_value = seed

    merged_thresholds = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        merged_thresholds.update(thresholds)

    core = {
        "campaign": campaign,
        "unit_type": unit_type,
        "planned_start": format_ts(start),
        "planned_end": format_ts(end),
        "observation_end": format_ts(obs_end),
        "maturity_days": maturity,
        "refund_maturity_days": refund_maturity,
        "primary_metric": "net_contribution",
        "normalization": normalization,
        "analysis_spec": analysis_spec,
        "alpha": alpha,
        "confidence": confidence,
        "max_looks": max_looks,
        "assignment": assignment,
        "strata": {unit: info["stratum"] for unit, info in eligible.items()},
        "unit_regions": {
            unit: info["region"] for unit, info in eligible.items() if info.get("region")
        },
        "seed": seed_value,
        "salt": campaign,
        "algorithm": method,
        "allocation_basis": allocation_basis,
    }
    fingerprint = _assignment_fingerprint(core)

    mutually_exclusive = []
    for other_name, other in sorted(data.campaigns.items()):
        if other_name == campaign:
            continue
        try:
            other_start = parse_ts(other["start"])
            other_end = parse_ts(other["end"])
        except (KeyError, ValueError):
            continue
        if other.get("exclusive_family") == campaign_row.get("exclusive_family") and other_start < end and start < other_end:
            mutually_exclusive.append(other_name)

    design = {
        "id": design_id,
        "campaign": campaign,
        "channel": campaign_row.get("channel"),
        "exclusive_family": campaign_row.get("exclusive_family"),
        "mutually_exclusive_campaigns": mutually_exclusive,
        "version": 1,
        "lineage": design_id,
        "status": "active",
        "confirmatory": not registered_after_exposure,
        "prior_outcome_views": registered_after_exposure,
        "registered_after_exposure": registered_after_exposure,
        "created_at": format_ts(created_at),
        "unit_type": unit_type,
        "primary_metric": "net_contribution",
        "normalization": normalization,
        "analysis_spec": analysis_spec,
        "alpha": alpha,
        "confidence": confidence,
        "maturity_days": maturity,
        "refund_maturity_days": refund_maturity,
        "max_looks": max_looks,
        "planned_start": format_ts(start),
        "planned_end": format_ts(end),
        "observation_end": format_ts(obs_end),
        "budget_cap_fen": cap,
        "allocation": {
            "method": method,
            "seed": seed_value,
            "salt": campaign,
            "algorithm": ALGORITHM if method != "external_registered" else method,
            "basis": allocation_basis,
            "assignment": assignment,
            "strata": core["strata"],
            "unit_regions": core["unit_regions"],
            "eligible_units": sorted(eligible),
            "fingerprint": fingerprint,
            "frozen_at": format_ts(created_at),
        },
        "thresholds": merged_thresholds,
        "rules": {
            "end_requires_significance": True,
            "end_requires_observation_complete": True,
            "pause_on_guard_failure": True,
            "futility_end_after_max_looks": True,
        },
        "notes": notes,
    }
    return design


def revise_design(
    design: dict[str, Any],
    *,
    new_id: str,
    created_at: datetime,
    data: SourceData,
    prior_reviews: int,
    changes: dict[str, Any],
    reason: str,
    rerandomize: bool = False,
    seed: int | None = None,
) -> dict[str, Any]:
    """生成新的决策版本。已看过的结果不能被包装成事先规定。"""
    if not reason.strip():
        raise DesignError("修订必须写明原因")
    start = parse_ts(changes.get("planned_start", design["planned_start"]))
    end = parse_ts(changes.get("planned_end", design["planned_end"]))
    obs_end = parse_ts(changes.get("observation_end", design["observation_end"]))
    maturity = int(changes.get("maturity_days", design["maturity_days"]))
    refund_maturity = int(changes.get("refund_maturity_days", design.get("refund_maturity_days", maturity)))
    if refund_maturity < maturity:
        raise DesignError("退款成熟期不能短于取消成熟期")
    if not (start < end <= obs_end):
        raise DesignError("必须满足 planned_start < planned_end <= observation_end")
    if obs_end < add_days(end, refund_maturity):
        raise DesignError("观察截止点必须覆盖计划终点加退款成熟期")
    spec = changes.get("analysis_spec", design["analysis_spec"])
    if spec not in stats.SPEC_LIBRARY:
        raise DesignError(f"未知分析口径: {spec}")
    normalization = changes.get("normalization", design["normalization"])
    if normalization not in {"total", "per_1000_baseline"}:
        raise DesignError("归一化口径必须是 total 或 per_1000_baseline")
    if design["unit_type"] == "person" and normalization == "per_1000_baseline":
        raise DesignError("用户群单位没有常规客群基数，请使用 total")
    max_looks = int(changes.get("max_looks", design["max_looks"]))
    if max_looks < 1:
        raise DesignError("max_looks 至少为 1")
    alpha = float(changes.get("alpha", design["alpha"]))
    confidence = float(changes.get("confidence", design["confidence"]))
    if not (0 < alpha < 1) or not (0 < confidence < 1):
        raise DesignError("alpha 与 confidence 必须在 (0,1) 内")

    updated = json.loads(json.dumps(design))
    updated["id"] = new_id
    updated["version"] = int(design["version"]) + 1
    updated["lineage"] = design["lineage"]
    updated["status"] = "active"
    updated["created_at"] = format_ts(created_at)
    updated["planned_start"] = format_ts(start)
    updated["planned_end"] = format_ts(end)
    updated["observation_end"] = format_ts(obs_end)
    updated["maturity_days"] = maturity
    updated["refund_maturity_days"] = refund_maturity
    updated["analysis_spec"] = spec
    updated["normalization"] = normalization
    new_metric = changes.get("primary_metric", design["primary_metric"])
    if new_metric != "net_contribution":
        raise DesignError("当前只支持以净贡献作为主要指标；更换主要指标需另行登记新的设计")
    updated["primary_metric"] = new_metric
    updated["max_looks"] = max_looks
    updated["alpha"] = alpha
    updated["confidence"] = confidence
    if "budget_cap_fen" in changes:
        cap = int(changes["budget_cap_fen"])
        if cap <= 0 or cap > int(data.campaigns[design["campaign"]].get("max_budget_fen") or 0):
            raise DesignError("预算上限超出活动上限或不为正")
        updated["budget_cap_fen"] = cap
    registered_after_exposure = bool(design.get("registered_after_exposure", False))
    updated["registered_after_exposure"] = registered_after_exposure
    updated["prior_outcome_views"] = prior_reviews > 0 or registered_after_exposure
    updated["confirmatory"] = prior_reviews == 0 and not registered_after_exposure
    updated["revision_reason"] = reason
    updated["supersedes"] = design["id"]

    if rerandomize:
        if prior_reviews > 0:
            raise DesignError("已经查看过结果，不能重新随机分组；这会破坏原分配")
        if design["unit_type"] == "person":
            eligible = person_roster(
                data, design["allocation"]["assignment"], before=created_at,
            )
        else:
            eligible = _eligible_units(data, design["unit_type"], design["allocation"]["eligible_units"])
        if seed is None:
            raise DesignError("重新随机分组必须登记新的 seed")
        assignment = allocate(eligible, seed=seed, salt=f"{design['campaign']}#v{updated['version']}")
        _validate_assignment(eligible, assignment)
        core = {
            "campaign": updated["campaign"],
            "unit_type": updated["unit_type"],
            "planned_start": updated["planned_start"],
            "planned_end": updated["planned_end"],
            "observation_end": updated["observation_end"],
            "assignment": assignment,
            "strata": {unit: info["stratum"] for unit, info in eligible.items()},
            "seed": seed,
            "salt": f"{design['campaign']}#v{updated['version']}",
            "algorithm": ALGORITHM,
        }
        updated["allocation"] = dict(updated["allocation"])
        updated["allocation"].update({
            "method": ALGORITHM,
            "seed": seed,
            "salt": f"{design['campaign']}#v{updated['version']}",
            "algorithm": ALGORITHM,
            "assignment": assignment,
            "strata": core["strata"],
            "unit_regions": {
                unit: info["region"] for unit, info in eligible.items() if info.get("region")
            },
            "eligible_units": sorted(eligible),
            "fingerprint": _assignment_fingerprint(core),
            "frozen_at": format_ts(created_at),
            "rerandomized": True,
        })
    else:
        updated["allocation"] = dict(updated["allocation"])
        updated["allocation"]["carried_forward_from"] = design["id"]
        core = {
            "campaign": updated["campaign"],
            "unit_type": updated["unit_type"],
            "planned_start": updated["planned_start"],
            "planned_end": updated["planned_end"],
            "observation_end": updated["observation_end"],
            "assignment": updated["allocation"]["assignment"],
            "strata": updated["allocation"].get("strata"),
            "seed": updated["allocation"].get("seed"),
            "salt": updated["allocation"].get("salt"),
            "algorithm": updated["allocation"].get("algorithm"),
        }
        updated["allocation"]["fingerprint"] = _assignment_fingerprint(core)
    return updated


# ---------------------------------------------------------------------------
# 守卫与分析
# ---------------------------------------------------------------------------

def _arm_counts(units: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "treatment": sum(1 for unit in units if unit["arm"] == "treatment"),
        "control": sum(1 for unit in units if unit["arm"] == "control"),
    }


def _balance(units: list[dict[str, Any]], thresholds: dict[str, float]) -> dict[str, Any]:
    strata: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for unit in units:
        strata[unit["stratum"]].append(unit)
    max_smd = 0.0
    detail = []
    missing_arm = []
    for stratum in sorted(strata):
        group = strata[stratum]
        treated = [unit["baseline"] for unit in group if unit["arm"] == "treatment"]
        control = [unit["baseline"] for unit in group if unit["arm"] == "control"]
        smd = stats.pooled_smd(treated, control)
        if smd is not None:
            max_smd = max(max_smd, abs(smd))
        if not treated or not control:
            missing_arm.append(stratum)
        ratio = (max(len(treated), len(control)) / min(len(treated), len(control))) if treated and control else None
        detail.append({
            "stratum": stratum,
            "treatment_units": len(treated),
            "control_units": len(control),
            "baseline_smd": smd,
            "ratio": ratio,
        })
    counts = _arm_counts(units)
    ratio = max(counts.values()) / min(counts.values()) if min(counts.values()) else None
    severe = (
        bool(missing_arm)
        or max_smd > float(thresholds["max_baseline_smd"])
        or (ratio is not None and ratio > float(thresholds["max_arm_ratio"]))
        or min(counts.values()) < int(thresholds["min_units_per_arm"])
    )
    return {
        "max_baseline_smd": max_smd,
        "arm_ratio": ratio,
        "missing_arms": missing_arm,
        "severe": severe,
        "strata": detail,
    }


def _coverage(units: list[dict[str, Any]], thresholds: dict[str, float]) -> dict[str, Any]:
    treated = [unit for unit in units if unit["arm"] == "treatment"]
    control = [unit for unit in units if unit["arm"] == "control"]
    treatment_covered = sum(1 for unit in treated if unit["paid_touches"] > 0)
    control_leaked = sum(1 for unit in control if unit["paid_touches"] > 0)
    treatment_coverage = treatment_covered / len(treated) if treated else None
    control_leakage = control_leaked / len(control) if control else None
    gaps = sorted(unit["unit"] for unit in treated if unit["paid_touches"] == 0)
    leaks = sorted(unit["unit"] for unit in control if unit["paid_touches"] > 0)
    gap = (
        treatment_coverage is None
        or treatment_coverage < float(thresholds["min_treatment_coverage"])
        or (control_leakage is not None and control_leakage > float(thresholds["max_control_leakage"]))
    )
    return {
        "treatment_coverage": treatment_coverage,
        "control_leakage": control_leakage,
        "treatment_gap_units": gaps,
        "control_leak_units": leaks,
        "gap": gap,
    }


def _maturity(units: list[dict[str, Any]], context: dict[str, Any]) -> dict[str, Any]:
    known = context["orders_known"]
    mature = context["orders_mature"]
    refund_mature = context.get("refund_mature_orders", mature)
    share = mature / known if known else None
    refund_share = refund_mature / known if known else None
    return {
        "orders_known": known,
        "orders_mature": mature,
        "mature_share": share,
        "refund_mature_orders": refund_mature,
        "refund_mature_share": refund_share,
        "unknown_net_orders": context["unknown_net_orders"],
    }


def evaluation_context(
    data: SourceData,
    design: dict[str, Any],
    cutoff: datetime,
    *,
    workspace_state: dict[str, Any] | None = None,
    exclude_units: Iterable[str] = (),
) -> tuple[list[stats.Unit], dict[str, Any], dict[str, Any]]:
    raw_units, context = build_units(
        data, design, cutoff, workspace_state=workspace_state, exclude_units=exclude_units,
    )
    units: list[stats.Unit] = []
    kept = []
    for raw in raw_units:
        # 保留原始净贡献；归一化只在 stats.estimate 中做一次，避免重复缩放。
        units.append(stats.Unit(
            unit_id=raw["unit"],
            arm=raw["arm"],
            stratum=raw["stratum"],
            baseline=float(raw["baseline"]),
            value=raw["raw_net"],
            contaminated=raw["contaminated"],
        ))
        kept.append(raw)
    return units, context, {"kept": kept}


def run_review(
    data: SourceData,
    design: dict[str, Any],
    *,
    cutoff: datetime,
    at: datetime,
    review_id: str,
    look: int,
    workspace_state: dict[str, Any] | None,
    seed: int,
    permutation_draws: int = 10_000,
    bootstrap_draws: int = 2_000,
) -> dict[str, Any]:
    if design["status"] != "active":
        raise ReviewError("只有当前有效的设计版本可以分析")
    if look < 1 or look > int(design["max_looks"]):
        raise ReviewError(
            f"查看序号 {look} 超出登记的观察计划（最多 {design['max_looks']} 次）；"
            "请先登记新的决策版本"
        )
    if cutoff < parse_ts(design["planned_start"]):
        raise ReviewError("数据截止点早于计划起点，还没有可观察的投放期")
    previous_cutoffs = [
        parse_ts(review["cutoff"]) for review in (workspace_state or {}).get("reviews", [])
        if review["design_id"] == design["id"]
    ]
    if previous_cutoffs and cutoff < max(previous_cutoffs):
        raise ReviewError("数据截止点早于上一次观察；同一设计的观察必须随日历推进")

    units, context, extra = evaluation_context(
        data, design, cutoff, workspace_state=workspace_state,
    )
    raw_units = extra["kept"]
    thresholds = design["thresholds"]
    balance = _balance(raw_units, thresholds)
    coverage = _coverage(raw_units, thresholds)
    maturity = _maturity(raw_units, context)

    primary = stats.estimate(
        units,
        spec_id=design["analysis_spec"],
        normalization=design["normalization"],
        confidence=float(design["confidence"]),
        alpha=float(design["alpha"]),
        look=look,
        max_looks=int(design["max_looks"]),
        seed=seed,
        permutation_draws=permutation_draws,
        bootstrap_draws=bootstrap_draws,
    )

    specs = {}
    for spec_id in stats.SPEC_LIBRARY:
        specs[spec_id] = stats.estimate(
            units,
            spec_id=spec_id,
            normalization=design["normalization"],
            confidence=float(design["confidence"]),
            alpha=float(design["alpha"]),
            look=look,
            max_looks=int(design["max_looks"]),
            seed=seed,
            permutation_draws=permutation_draws,
            bootstrap_draws=bootstrap_draws,
        )

    contaminated = [raw["unit"] for raw in raw_units if raw["contaminated"]]
    sensitivity = None
    sensitivity_error = None
    sensitivity_units = [unit for unit in units if not unit.contaminated]
    if contaminated and any(unit.arm == "treatment" for unit in sensitivity_units) and any(
        unit.arm == "control" for unit in sensitivity_units
    ):
        try:
            sensitivity = stats.estimate(
                sensitivity_units,
                spec_id=design["analysis_spec"],
                normalization=design["normalization"],
                confidence=float(design["confidence"]),
                alpha=float(design["alpha"]),
                look=look,
                max_looks=int(design["max_looks"]),
                seed=seed ^ 0xC0FFEE,
                permutation_draws=permutation_draws,
                bootstrap_draws=bootstrap_draws,
            )
        except stats.StatsError as exc:
            sensitivity_error = str(exc)
    elif contaminated:
        sensitivity_error = "排除受污染单位后缺少处理组或对照组，无法做稳健性检查"

    contamination_share = len(contaminated) / len(raw_units) if raw_units else 0.0
    contamination_detail = [
        {"unit": raw["unit"], "arm": raw["arm"], "activity": raw["contamination"]}
        for raw in raw_units if raw["contaminated"]
    ]

    guards: dict[str, dict[str, Any]] = {}

    mature_share = maturity["mature_share"]
    guards["maturity"] = {
        "pass": mature_share is not None and mature_share >= float(thresholds["min_mature_share"]),
        "detail": maturity,
        "reason": None if mature_share is not None and mature_share >= float(thresholds["min_mature_share"])
        else "订单尚未成熟或截止点内没有已知订单，不能据此判胜",
    }
    guards["balance"] = {
        "pass": not balance["severe"],
        "detail": balance,
        "reason": None if not balance["severe"] else "分组严重失衡（相似层缺组、基线差异或组规模差异过大）",
    }
    guards["coverage"] = {
        "pass": not coverage["gap"],
        "detail": coverage,
        "reason": None if not coverage["gap"] else "渠道覆盖存在缺口或对照组出现付费触达泄漏",
    }
    guards["missing_values"] = {
        "pass": context["unknown_net_orders"] == 0,
        "detail": {"unknown_net_orders": context["unknown_net_orders"]},
        "reason": None if context["unknown_net_orders"] == 0 else "存在净贡献未知（null）的订单，不能按 0 处理",
    }

    spec_rows = {
        spec_id: {
            "estimate": result["estimate"],
            "p_two_sided": result["p_two_sided"],
            "significant": result["significant"],
            "direction": result["direction"],
            "ci_low": result["ci_low"],
            "ci_high": result["ci_high"],
        }
        for spec_id, result in specs.items()
    }
    # “不一致”指方向相反，或主要口径显著而其他登记口径不显著。
    # 所有口径都不显著且方向相同属于一致（共同显示证据不足），不触发该守卫。
    primary_result = specs[design["analysis_spec"]]
    disagreement = False
    for spec_id, row in spec_rows.items():
        if spec_id == design["analysis_spec"]:
            continue
        if row["direction"] != primary_result["direction"] and row["estimate"] != 0:
            disagreement = True
        if primary_result["significant"] and not row["significant"]:
            disagreement = True

    if contamination_share > 0:
        # 只有主结果声称有效（显著为正）时，污染才构成需要稳健性支持的门槛；
        # 主结果本身不显著时，不因为污染而“洗白”，但当前已不可能判胜。
        if primary["significant"] and primary["direction"] == "treatment":
            robust = (
                sensitivity is not None
                and sensitivity["direction"] == "treatment"
                and sensitivity["significant"]
            )
            contamination_reason = "存在其他活动污染，排除受污染单位后无法得到稳健的正向结论"
        else:
            robust = True
            contamination_reason = None
        guards["contamination"] = {
            "pass": robust,
            "detail": {
                "contaminated_units": contaminated,
                "share": contamination_share,
                "sensitivity": sensitivity,
                "sensitivity_error": sensitivity_error,
                "note": "主结果始终包含全部冻结单位；排除受污染单位只是登记口径下的稳健性检查，不改动原分组",
            },
            "reason": contamination_reason,
        }
    else:
        guards["contamination"] = {
            "pass": True,
            "detail": {"contaminated_units": [], "share": 0.0, "sensitivity": None},
            "reason": None,
        }

    scheduled = [
        {"unit": raw["unit"], "arm": raw["arm"],
         "activity": raw["scheduled_elsewhere"]}
        for raw in raw_units if raw["scheduled_elsewhere"]
    ]
    guards["contamination"]["detail"]["scheduled_elsewhere"] = scheduled

    guards["spec_robustness"] = {
        "pass": not disagreement,
        "detail": {"pre_registered_specs": spec_rows, "primary_spec": design["analysis_spec"]},
        "reason": None if not disagreement else "预先登记的多口径结论不一致，不能挑选有利口径",
    }

    observation_complete = cutoff >= parse_ts(design["observation_end"])
    guards["observation_complete"] = {
        "pass": observation_complete,
        "detail": {
            "observation_end": design["observation_end"],
            "cutoff": format_ts(cutoff),
            "look": look,
            "max_looks": int(design["max_looks"]),
        },
        "reason": None if observation_complete else "尚未到达登记的计划观察截止点",
    }

    blocking = [name for name, guard in guards.items() if not guard["pass"]]
    # 非确认性设计（投放后登记或看过结果后修订）永远不能自动判胜，
    # 但其余守卫照常计算，便于说明原因。
    if not design.get("confirmatory", True):
        guards["confirmatory"] = {
            "pass": False,
            "detail": {
                "confirmatory": False,
                "prior_outcome_views": design.get("prior_outcome_views", False),
                "registered_after_exposure": design.get("registered_after_exposure", False),
            },
            "reason": "非确认性设计（投放后登记或看过结果后修订），只能作为探索性证据",
        }
        blocking = [name for name, guard in guards.items() if not guard["pass"]]
    auto_win = (
        not blocking
        and primary["significant"]
        and primary["direction"] == "treatment"
        and design.get("confirmatory", True)
        and look <= int(design["max_looks"])
    )

    if guards["maturity"]["pass"] is False:
        recommended = "pause"
    elif not primary["significant"] and look >= int(design["max_looks"]):
        recommended = "end"
    elif observation_complete and primary["significant"]:
        recommended = "end"
    elif observation_complete and look >= int(design["max_looks"]):
        recommended = "end"
    else:
        recommended = "continue"
    if not guards["maturity"]["pass"] or not guards["balance"]["pass"] or not guards["coverage"]["pass"]:
        recommended = "pause"
    if not design.get("confirmatory", True) and primary["significant"]:
        # 探索性结果即使显著，也不具备“按有效结束”的资格。
        recommended = "pause"

    result = {
        "review_id": review_id,
        "design_id": design["id"],
        "campaign": design["campaign"],
        "design_version": design["version"],
        "decision_version": f"{design['lineage']}#v{design['version']}",
        "confirmatory": design.get("confirmatory", True),
        "prior_outcome_views": design.get("prior_outcome_views", False),
        "cutoff": format_ts(cutoff),
        "at": format_ts(at),
        "look": look,
        "max_looks": int(design["max_looks"]),
        "primary_metric": design["primary_metric"],
        "normalization": design["normalization"],
        "primary": primary,
        "specs": spec_rows,
        "sensitivity": sensitivity,
        "guards": guards,
        "blocking_guards": blocking,
        "auto_win_eligible": auto_win,
        "exploratory_only": not design.get("confirmatory", True),
        "recommended_action": recommended,
        "contamination_share": contamination_share,
        "contamination": contamination_detail,
        "units": raw_units,
        "unit_context": context,
        "allocation_fingerprint": design["allocation"]["fingerprint"],
        "assignment": design["allocation"]["assignment"],
        "source_fingerprint": None,
        "seed": seed,
    }
    return result


def validate_decision(
    result: dict[str, Any],
    action: str,
    rationale: str,
    *,
    deviation_acknowledged: bool = False,
) -> tuple[list[str], list[str]]:
    """返回 (拒绝原因, 偏离警告)。

    登记规则
    --------
    * 守卫未通过（成熟度、失衡、覆盖、缺失值、观察未完成）时，不能登记
      “结束并判胜”。可以暂停，或在不判胜的前提下结束（如实记录）。
    * 到达最大查看次数后不能继续。
    * 与登记建议不一致时，必须显式确认偏离；未确认则拒绝。
    """
    errors: list[str] = []
    deviations: list[str] = []
    if action not in {"continue", "pause", "end"}:
        errors.append("action 必须是 continue、pause 或 end")
        return errors, deviations
    if len(rationale.strip()) < 8:
        errors.append("必须写明至少 8 个字的决策理由")
    recommended = result["recommended_action"]
    blocking = list(result.get("blocking_guards", []))
    positive = (
        result["primary"]["direction"] == "treatment"
        and result["primary"]["significant"]
    )
    if action == "continue" and result["look"] >= result["max_looks"]:
        errors.append("已到达登记的最大查看次数，不能继续；请登记新的决策版本")
    if action == "end":
        # 订单未成熟时结束，会用一个还可能翻转的结论做不可逆决定，必须拒绝；
        # 其余阻断守卫（失衡、覆盖、缺失值）允许“不判胜的结束”，但不能判胜。
        if "maturity" in blocking:
            errors.append(
                "订单尚未成熟，不能结束观察；请暂停等待成熟期，或登记新的决策版本"
            )
        hard_blockers = [name for name in blocking if name != "observation_complete"]
        if positive and not result["auto_win_eligible"] and hard_blockers:
            errors.append(
                "存在阻断守卫（" + "、".join(hard_blockers) + "），不能登记“有效结束”；"
                "请先暂停并修正数据问题，或如实登记为“不判胜的结束”"
            )
        if positive and not result["guards"]["observation_complete"]["pass"]:
            deviations.append("在计划观察截止点之前结束；这不是事先规定的判胜时点")
    if blocking and action == "continue":
        errors.append(
            "存在阻断守卫时不能继续原计划；请暂停，并登记新的决策版本说明修正方式"
        )
    if action == "pause" and recommended != "pause":
        deviations.append("当前守卫均通过，暂停属于偏离登记计划的操作")
    if action != recommended:
        deviations.append(f"登记规则建议 {recommended}，实际选择 {action}")
    if deviations and not deviation_acknowledged:
        errors.append(
            "该决定偏离登记的观察计划或统计建议；必须显式确认偏离（--acknowledge-deviation）"
        )
    return errors, deviations
