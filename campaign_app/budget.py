"""预算续投：剩余预算、候选比较、方案确认、执行回填。

业务规则
--------
* 预算总额 600000 分。已签约（signed）且不可取消的合同、已执行（executed）
  的合同都占用预算；可取消且未执行的合同不占用，但会列示。
* 方案只有在负责人确认后才形成占用；确认有有效期。两个方案若会争用同一
  剩余预算或同一地区的互斥名额，后确认者必须重新比较，不能沿用旧快照。
* 已确认方案的投放期与既有互斥活动重叠、或同一地区名额已被其他方案占用时，
  方案被判定为“需重新比较”或“冲突”。
* 实际下单/取消通过执行回填登记；新增但未执行的投放进入重新审批，
  不会静默继续。已履约（executed）的广告不能当成未花费。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

from .errors import DomainError
from .source import SourceData
from .timeutil import format_ts, parse_ts

CANCELLED = "cancelled"


class BudgetError(DomainError):
    pass


@dataclass
class Committed:
    amount_fen: int
    source: str
    contract_id: str | None
    campaign: str | None
    region: str | None
    cancelled: bool
    cancellable: bool


def signed_commitments(data: SourceData) -> list[Committed]:
    rows = []
    for contract in sorted(data.contracts.values(), key=lambda row: row["id"]):
        status = contract.get("status")
        amount = contract.get("amount_fen") or 0
        if status == "executed":
            rows.append(Committed(int(amount), "contract", contract["id"], contract["campaign"],
                                  contract["region"], False, bool(contract.get("cancellable"))))
        elif status == "signed":
            if contract.get("cancellable"):
                rows.append(Committed(0, "cancellable_signed", contract["id"], contract["campaign"],
                                      contract["region"], False, True))
                rows.append(Committed(int(amount), "held_until_decision", contract["id"],
                                      contract["campaign"], contract["region"], False, True))
            else:
                rows.append(Committed(int(amount), "contract", contract["id"], contract["campaign"],
                                      contract["region"], False, False))
        else:
            rows.append(Committed(0, "cancelled_contract", contract["id"], contract["campaign"],
                                  contract["region"], True, bool(contract.get("cancellable"))))
    return rows


def budget_ledger(
    data: SourceData,
    state: dict[str, Any],
    *,
    at: datetime,
) -> dict[str, Any]:
    total = int(state["workspace"]["budget_total_fen"])
    committed: list[dict[str, Any]] = []
    held = 0
    for row in signed_commitments(data):
        if row.source == "held_until_decision":
            held += row.amount_fen
            continue
        committed.append({
            "source": row.source,
            "reference": row.contract_id,
            "campaign": row.campaign,
            "region": row.region,
            "amount_fen": row.amount_fen,
            "cancelled": row.cancelled,
            "cancellable": row.cancellable,
        })
    contract_spend = sum(row["amount_fen"] for row in committed if row["source"] == "contract")

    reservations: list[dict[str, Any]] = []
    reserved_fen = 0
    executions_by_plan: dict[str, dict[str, Any]] = {}
    for execution in sorted(state.get("executions", {}).values(), key=lambda row: row["id"]):
        # 同一方案按“最新回填状态”解释：下单后取消，占用归零。
        executions_by_plan[execution["proposal_id"]] = execution
    for plan in sorted(state.get("plans", {}).values(), key=lambda row: row["id"]):
        if plan["status"] != "confirmed":
            continue
        if plan["id"] in executions_by_plan:
            continue  # 已回填执行；改由执行记录占用，避免重复计算
        expires = parse_ts(plan["expires_at"]) if plan.get("expires_at") else None
        active = expires is None or expires > at
        amount = int(plan["requested_fen"])
        reservations.append({
            "source": "confirmed_plan",
            "reference": plan["id"],
            "campaign": plan["campaign"],
            "regions": plan["regions"],
            "amount_fen": amount,
            "active": active,
            "expires_at": plan.get("expires_at"),
        })
        if active:
            reserved_fen += amount

    signed_execution_fen = 0
    executed_fen = 0
    executed_rows: list[dict[str, Any]] = []
    for execution in sorted(executions_by_plan.values(), key=lambda row: row["id"]):
        amount = int(execution["executed_fen"])
        if execution["status"] == "cancelled":
            continue
        if execution["status"] == "executed":
            executed_fen += amount
        else:
            signed_execution_fen += amount
        executed_rows.append({
            "source": "execution",
            "reference": execution["id"],
            "proposal_id": execution["proposal_id"],
            "campaign": execution["campaign"],
            "amount_fen": amount,
            "status": execution["status"],
        })

    committed_total = contract_spend + reserved_fen + signed_execution_fen + executed_fen
    remaining = total - committed_total
    return {
        "total_fen": total,
        "contract_committed_fen": contract_spend,
        "cancellable_signed_fen": held,
        "confirmed_plan_reserved_fen": reserved_fen,
        "signed_execution_fen": signed_execution_fen,
        "executed_fen": executed_fen,
        "remaining_fen": remaining,
        "committed": committed,
        "reservations": reservations,
        "executions": executed_rows,
        "overcommitted": remaining < 0,
        "at": format_ts(at),
    }


def _overlaps(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    return start_a < end_b and start_b < end_a


def _family_of(data: SourceData, campaign: str) -> str | None:
    row = data.campaigns.get(campaign)
    return row.get("exclusive_family") if row else None


def _campaign_commitment(
    data: SourceData,
    state: dict[str, Any],
    campaign: str,
    *,
    at: datetime,
) -> int:
    """某活动已占用的活动额度：合同 + 有效确认方案 + 下单/执行（不重复计）。"""
    total = 0
    for contract in data.contracts.values():
        if contract["campaign"] != campaign or contract["status"] == CANCELLED:
            continue
        if contract["status"] == "signed" and contract.get("cancellable"):
            continue
        total += int(contract.get("amount_fen") or 0)
    executed_plans: set[str] = set()
    latest_execution: dict[str, dict[str, Any]] = {}
    for execution in sorted(state.get("executions", {}).values(), key=lambda row: row["id"]):
        if execution["campaign"] != campaign:
            continue
        latest_execution[execution["proposal_id"]] = execution
    for plan_id, execution in latest_execution.items():
        if execution["status"] in {"signed", "executed"}:
            total += int(execution["executed_fen"])
            executed_plans.add(plan_id)
    for plan in state.get("plans", {}).values():
        if plan["campaign"] != campaign or plan["status"] != "confirmed":
            continue
        if plan["id"] in executed_plans:
            continue
        expires = plan.get("expires_at")
        if expires and parse_ts(expires) <= at:
            continue
        total += int(plan["requested_fen"])
    return total


def _plan_occupies(plan: dict[str, Any], region: str, start: datetime, end: datetime,
                   data: SourceData) -> bool:
    if region not in plan.get("regions", []):
        return False
    plan_start = parse_ts(plan["start"])
    plan_end = parse_ts(plan["end"])
    return _overlaps(start, end, plan_start, plan_end)


def existing_region_occupancy(
    data: SourceData,
    state: dict[str, Any],
    *,
    region: str,
    start: datetime,
    end: datetime,
    campaign: str,
    ignore_plan: str | None = None,
    at: datetime | None = None,
) -> list[dict[str, Any]]:
    family = _family_of(data, campaign)
    rows: list[dict[str, Any]] = []
    for contract in sorted(data.contracts.values(), key=lambda row: row["id"]):
        if contract["region"] != region or contract["status"] == CANCELLED:
            continue
        if contract["campaign"] == campaign:
            continue  # 本活动既有合同的预算已另行占用，不构成互斥冲突
        contract_family = _family_of(data, contract["campaign"])
        if contract_family != family:
            continue
        rows.append({
            "kind": "contract",
            "region": region,
            "reference": contract["id"],
            "campaign": contract["campaign"],
            "exclusive_family": contract_family,
            "start": None,
            "end": None,
            "note": "既有合同没有日期；按活动计划区间判断重叠",
            "overlapping": _contract_overlaps_campaign(data, contract["campaign"], start, end),
        })
    for plan in sorted(state.get("plans", {}).values(), key=lambda row: row["id"]):
        if plan["status"] != "confirmed":
            continue
        if ignore_plan is not None and plan["id"] == ignore_plan:
            continue
        expires = plan.get("expires_at")
        if expires and parse_ts(expires) <= (at or start):
            # 过期确认不再占用地区名额；若它已过期，必须重新比较后才能复用。
            continue
        same_campaign = plan["campaign"] == campaign
        if not same_campaign and _family_of(data, plan["campaign"]) != family:
            continue
        if _plan_occupies(plan, region, start, end, data):
            rows.append({
                "kind": "confirmed_plan",
                "region": region,
                "reference": plan["id"],
                "campaign": plan["campaign"],
                "exclusive_family": family,
                "start": plan["start"],
                "end": plan["end"],
                "note": (
                    "同地区互斥组已被另一份已确认方案占用"
                    if plan["campaign"] != campaign
                    else "同地区名额已被本活动的另一份已确认方案占用"
                ),
                "overlapping": True,
            })
    return rows


def _contract_overlaps_campaign(data: SourceData, contract_campaign: str,
                                start: datetime, end: datetime) -> bool:
    row = data.campaigns.get(contract_campaign)
    if not row:
        return False
    try:
        c_start = parse_ts(row["start"])
        c_end = parse_ts(row["end"])
    except (KeyError, ValueError):
        return False
    return _overlaps(start, end, c_start, c_end)


def candidate_evidence(data: SourceData, state: dict[str, Any], campaign: str) -> dict[str, Any]:
    reviews = [
        review for review in state.get("reviews", [])
        if review["campaign"] == campaign and review.get("design_id")
    ]
    latest = reviews[-1] if reviews else None
    if latest is None:
        return {
            "campaign": campaign,
            "has_evidence": False,
            "status": "no_confirmed_evidence",
            "note": "没有该活动的组随机化观察；不能把描述性路径当作增量证据",
        }
    primary = latest["primary"]
    guards = latest["guards"]
    blocking = [name for name, guard in guards.items() if not guard["pass"]]
    return {
        "campaign": campaign,
        "has_evidence": True,
        "review_id": latest["review_id"],
        "design_id": latest["design_id"],
        "cutoff": latest["cutoff"],
        "look": latest["look"],
        "estimate": primary["estimate"],
        "ci_low": primary["ci_low"],
        "ci_high": primary["ci_high"],
        "p_two_sided": primary["p_two_sided"],
        "significant": primary["significant"],
        "direction": primary["direction"],
        "blocking_guards": blocking,
        "auto_win_eligible": latest["auto_win_eligible"],
        "recommended_action": latest["recommended_action"],
        "confirmatory": latest.get("confirmatory", True),
        "status": "supported" if latest["auto_win_eligible"] else "not_supported",
        "note": (
            "证据来自冻结的组随机化设计；仍受采样不确定性限制"
            if latest["auto_win_eligible"]
            else "守卫未全部通过或结果不显著，不能据此扩投"
        ),
    }


def build_proposal(
    data: SourceData,
    state: dict[str, Any],
    *,
    proposal_id: str,
    campaign: str,
    regions: list[str],
    requested_fen: int,
    start: str,
    end: str,
    ttl_hours: int,
    created_at: datetime,
    rationale: str,
) -> dict[str, Any]:
    if campaign not in data.campaigns:
        raise BudgetError(f"未知活动: {campaign}")
    if requested_fen <= 0:
        raise BudgetError("申请金额必须为正整数分")
    if not regions:
        raise BudgetError("必须指定至少一个投放地区")
    unknown = sorted(set(regions) - set(data.regions))
    if unknown:
        raise BudgetError(f"未知地区: {unknown}")
    if len(set(regions)) != len(regions):
        raise BudgetError("地区不能重复")
    if not rationale.strip():
        raise BudgetError("必须写明续投理由")
    if ttl_hours <= 0:
        raise BudgetError("确认有效期必须为正")
    start_dt = parse_ts(start, field="start")
    end_dt = parse_ts(end, field="end")
    if start_dt >= end_dt:
        raise BudgetError("投放起点必须早于终点")
    campaign_row = data.campaigns[campaign]
    campaign_cap = int(campaign_row.get("max_budget_fen") or 0)
    if requested_fen > campaign_cap:
        raise BudgetError("申请金额超过活动预算上限")

    # 活动预算上限是累计上限：既有合同与已确认/已执行方案都先占用该活动额度。
    campaign_committed = _campaign_commitment(data, state, campaign, at=created_at)
    if campaign_committed + requested_fen > campaign_cap:
        raise BudgetError(
            f"活动 {campaign} 已占用 {campaign_committed} 分，加上本次 {requested_fen} 分"
            f"超过活动上限 {campaign_cap} 分"
        )

    ledger = budget_ledger(data, state, at=created_at)
    if requested_fen > ledger["remaining_fen"]:
        raise BudgetError(
            f"申请 {requested_fen} 分超过剩余预算 {ledger['remaining_fen']} 分；"
            "不能占用已签约或已执行合同的预算"
        )

    conflicts = []
    for region in sorted(regions):
        occupancy = existing_region_occupancy(
            data, state, region=region, start=start_dt, end=end_dt, campaign=campaign,
            at=created_at,
        )
        for row in occupancy:
            if row.get("overlapping", True):
                conflicts.append({"region": region, **row})
    if conflicts:
        raise BudgetError(
            "同一地区存在互斥活动，不能同时占用: " + "; ".join(
                f"{row['region']}<-{row['reference']}" for row in conflicts
            )
        )

    evidence = candidate_evidence(data, state, campaign)
    expires_at = created_at + timedelta(hours=ttl_hours)
    proposal = {
        "id": proposal_id,
        "campaign": campaign,
        "channel": campaign_row.get("channel"),
        "exclusive_family": campaign_row.get("exclusive_family"),
        "regions": sorted(regions),
        "requested_fen": int(requested_fen),
        "start": format_ts(start_dt),
        "end": format_ts(end_dt),
        "status": "proposed",
        "evidence": evidence,
        "created_at": format_ts(created_at),
        "expires_at": format_ts(expires_at),
        "confirmed_at": None,
        "cancelled_at": None,
        "rationale": rationale,
        "snapshot": {
            "remaining_fen": ledger["remaining_fen"],
            "contract_committed_fen": ledger["contract_committed_fen"],
            "confirmed_plan_reserved_fen": ledger["confirmed_plan_reserved_fen"],
            "executed_fen": ledger["executed_fen"],
            "source_fingerprint": state["workspace"].get("source_fingerprint"),
        },
        "recheck_required": False,
        "recheck_reasons": [],
        "execution_id": None,
    }
    return proposal


def compare_proposals(
    data: SourceData,
    state: dict[str, Any],
    proposal_ids: Iterable[str],
    *,
    at: datetime,
) -> list[dict[str, Any]]:
    """比较候选：增量证据、剩余地区覆盖、预算上限；按证据强度排序。"""
    ledger = budget_ledger(data, state, at=at)
    rows = []
    for proposal_id in proposal_ids:
        proposal = state["plans"].get(proposal_id)
        if proposal is None:
            raise BudgetError(f"方案不存在: {proposal_id}")
        evidence = proposal.get("evidence") or candidate_evidence(data, state, proposal["campaign"])
        regions = proposal["regions"]
        occupancy = []
        for region in regions:
            occupancy.extend(existing_region_occupancy(
                data, state,
                region=region,
                start=parse_ts(proposal["start"]),
                end=parse_ts(proposal["end"]),
                campaign=proposal["campaign"],
                at=at,
            ))
        overlapping = [row for row in occupancy if row.get("overlapping", True)]
        region_conflict_share = len({row["region"] for row in overlapping}) / len(regions)
        remaining_region_capacity = round(1.0 - region_conflict_share, 4)
        evidence_strength = 0
        if evidence.get("auto_win_eligible"):
            evidence_strength = 3
        elif evidence.get("significant") and evidence.get("direction") == "treatment":
            evidence_strength = 2
        elif evidence.get("has_evidence"):
            evidence_strength = 1
        affordable = proposal["requested_fen"] <= ledger["remaining_fen"]
        # 风险不足以支持扩投时，允许保留预算：推荐动作不是“扩投”，而是保留。
        if evidence_strength == 0 or region_conflict_share > 0 or not affordable:
            recommendation = "hold_budget"
            if evidence_strength == 0:
                reason = "没有确认性增量证据，保留预算等待新的观察"
            elif region_conflict_share > 0:
                reason = "地区名额存在冲突，保留预算，待重新比较"
            else:
                reason = "剩余预算不足，保留预算，待重新比较"
        elif evidence_strength == 3:
            recommendation = "invest"
            reason = "守卫通过且证据支持，可按上限投放"
        else:
            recommendation = "pilot"
            reason = "证据有限，只支持小额试探，不能按有效结论扩投"
        rows.append({
            "proposal_id": proposal_id,
            "campaign": proposal["campaign"],
            "status": proposal["status"],
            "requested_fen": proposal["requested_fen"],
            "evidence_status": evidence.get("status"),
            "evidence_strength": evidence_strength,
            "recommendation": recommendation,
            "recommendation_reason": reason,
            "estimate": evidence.get("estimate"),
            "ci": [
                evidence.get("ci_low"),
                evidence.get("ci_high"),
            ] if evidence.get("has_evidence") else None,
            "blocking_guards": evidence.get("blocking_guards", []),
            "region_capacity": remaining_region_capacity,
            "regions_in_conflict": sorted({row["region"] for row in overlapping}),
            "within_budget": affordable,
            "remaining_budget_fen": ledger["remaining_fen"],
            "expired": proposal.get("expires_at") is not None
            and parse_ts(proposal["expires_at"]) <= at,
            "regions": regions,
        })
    rows.sort(key=lambda row: (-row["evidence_strength"], row["regions_in_conflict"],
                               row["requested_fen"], row["proposal_id"]))
    return rows


def confirm_proposal(
    data: SourceData,
    state: dict[str, Any],
    proposal_id: str,
    *,
    at: datetime,
    expected_snapshot: dict[str, Any] | None = None,
    rationale: str,
) -> dict[str, Any]:
    proposal = state["plans"].get(proposal_id)
    if proposal is None:
        raise BudgetError(f"方案不存在: {proposal_id}")
    if proposal["status"] != "proposed":
        raise BudgetError(f"方案状态为 {proposal['status']}，不能确认")
    if len(rationale.strip()) < 8:
        raise BudgetError("确认方案必须写明至少 8 个字的理由")
    expires = proposal.get("expires_at")
    if expires and parse_ts(expires) <= at:
        raise BudgetError("方案已过期；请重新运行比较后再确认")

    ledger = budget_ledger(data, state, at=at)
    if proposal["requested_fen"] > ledger["remaining_fen"]:
        raise BudgetError(
            f"剩余预算 {ledger['remaining_fen']} 分不足以确认该方案；请重新比较"
        )
    campaign_cap = int(data.campaigns[proposal["campaign"]].get("max_budget_fen") or 0)
    campaign_committed = _campaign_commitment(data, state, proposal["campaign"], at=at)
    if campaign_committed + proposal["requested_fen"] > campaign_cap:
        raise BudgetError("确认后超过活动预算上限；请先减少或取消其他占用")
    snapshot = proposal["snapshot"]
    if snapshot.get("remaining_fen") != ledger["remaining_fen"]:
        raise BudgetError(
            "剩余预算自方案登记后已变化（可能已有其他方案确认或执行）；"
            "过期确认必须重新比较"
        )
    if expected_snapshot is not None and expected_snapshot.get("remaining_fen") != ledger["remaining_fen"]:
        raise BudgetError("提交的剩余预算与当前台账不一致，请重新比较")

    for region in proposal["regions"]:
        occupancy = existing_region_occupancy(
            data, state,
            region=region,
            start=parse_ts(proposal["start"]),
            end=parse_ts(proposal["end"]),
            campaign=proposal["campaign"],
            ignore_plan=proposal_id,
            at=at,
        )
        overlapping = [row for row in occupancy if row.get("overlapping", True)]
        if overlapping:
            raise BudgetError(
                f"地区 {region} 的互斥名额已被占用（{overlapping[0]['reference']}）；请重新比较"
            )

    updated = dict(proposal)
    updated["status"] = "confirmed"
    updated["confirmed_at"] = format_ts(at)
    updated["snapshot"] = {
        "remaining_fen": ledger["remaining_fen"],
        "contract_committed_fen": ledger["contract_committed_fen"],
        "confirmed_plan_reserved_fen": ledger["confirmed_plan_reserved_fen"],
        "executed_fen": ledger["executed_fen"],
        "source_fingerprint": state["workspace"].get("source_fingerprint"),
    }
    updated["confirmation_rationale"] = rationale
    return updated


def expire_or_flag(data: SourceData, state: dict[str, Any], *, at: datetime) -> list[dict[str, Any]]:
    """列出已过期但仍处于 proposed/confirmed 的方案。"""
    flagged = []
    for proposal in sorted(state["plans"].values(), key=lambda row: row["id"]):
        if proposal["status"] not in {"proposed", "confirmed"}:
            continue
        expires = proposal.get("expires_at")
        if expires and parse_ts(expires) <= at:
            flagged.append({
                "id": proposal["id"],
                "status": proposal["status"],
                "expires_at": expires,
                "message": "确认已过期；若其他方案已使用同一剩余预算或地区名额，必须重新比较",
            })
    return flagged


def record_execution(
    data: SourceData,
    state: dict[str, Any],
    proposal_id: str,
    *,
    execution_id: str,
    at: datetime,
    status: str,
    executed_fen: int | None,
    note: str,
) -> dict[str, Any]:
    proposal = state["plans"].get(proposal_id)
    if proposal is None:
        raise BudgetError(f"方案不存在: {proposal_id}")
    if proposal["status"] not in {"confirmed", "executed"}:
        raise BudgetError("只有已确认或已下单方案可以回填执行状态")
    if status not in {"signed", "cancelled", "executed"}:
        raise BudgetError("执行状态必须是 signed、cancelled 或 executed")
    if status == "executed" and executed_fen is None:
        raise BudgetError("执行完成必须回填实际执行金额")
    amount = int(executed_fen if executed_fen is not None else 0)
    if amount < 0:
        raise BudgetError("实际执行金额不能为负")
    if amount > proposal["requested_fen"]:
        raise BudgetError("实际执行金额不能超过确认方案的金额；超出部分必须重新审批")
    for existing in state["executions"].values():
        if existing["proposal_id"] != proposal_id:
            continue
        if existing["status"] == "executed" and existing["id"] != execution_id:
            raise BudgetError("该方案已执行完成，不能再改写执行记录")
        if existing["status"] == "cancelled":
            raise BudgetError("该方案的执行已取消，不能重复占用")
        if status == "signed" and existing["status"] == "signed" and existing["id"] != execution_id:
            raise BudgetError("该方案已有下单记录；取消或执行请回填新的状态")
    amount = int(executed_fen if executed_fen is not None else 0)
    if amount < 0:
        raise BudgetError("实际执行金额不能为负")
    if amount > proposal["requested_fen"]:
        raise BudgetError("实际执行金额不能超过确认方案的金额；超出部分必须重新审批")
    allocations = []
    order = sorted(proposal["regions"])
    base, remainder = divmod(amount, len(order))
    for index, region in enumerate(order):
        allocations.append({
            "region": region,
            "amount_fen": base + (remainder if index == len(order) - 1 else 0),
        })
    execution = {
        "id": execution_id,
        "proposal_id": proposal_id,
        "campaign": proposal["campaign"],
        "regions": proposal["regions"],
        "start": proposal["start"],
        "end": proposal["end"],
        "allocations": allocations,
        "status": status,
        "executed_fen": amount,
        "recorded_at": format_ts(at),
        "note": note,
    }
    if status == "cancelled":
        execution["executed_fen"] = 0
    return execution


def refund_review(
    data: SourceData,
    state: dict[str, Any],
    *,
    at: datetime,
    min_retroactive_fen: int = 0,
) -> list[dict[str, Any]]:
    """识别迟到退款可能推翻统计结论的投放；区分已履约与新增加投放。

    检查两类状态：已回填执行（signed/executed），以及已确认但尚未回填的
    新增投放。后者若未执行，应进入重新审批，而不是因为“还没下单”被漏掉。
    """
    flagged: list[dict[str, Any]] = []
    reviews = state.get("reviews", [])
    latest_by_design: dict[str, dict[str, Any]] = {}
    for review in reviews:
        latest_by_design[review["design_id"]] = review
    latest_by_campaign: dict[str, dict[str, Any]] = {}
    for review in latest_by_design.values():
        if review["auto_win_eligible"]:
            latest_by_campaign[review["campaign"]] = review

    subjects: list[dict[str, Any]] = []
    for execution in sorted(state["executions"].values(), key=lambda row: row["id"]):
        if execution["status"] == "cancelled":
            continue
        subjects.append({
            "reference": execution["id"],
            "proposal_id": execution["proposal_id"],
            "campaign": execution["campaign"],
            "regions": execution["regions"],
            "status": execution["status"],
            "is_execution": True,
        })
    executed_proposals = {
        execution["proposal_id"] for execution in state["executions"].values()
        if execution["status"] in {"signed", "executed"}
    }
    for proposal in sorted(state["plans"].values(), key=lambda row: row["id"]):
        if proposal["status"] != "confirmed" or proposal["id"] in executed_proposals:
            continue
        subjects.append({
            "reference": proposal["id"],
            "proposal_id": proposal["id"],
            "campaign": proposal["campaign"],
            "regions": proposal["regions"],
            "status": "confirmed_unexecuted",
            "is_execution": False,
        })

    for subject in subjects:
        review = latest_by_campaign.get(subject["campaign"])
        if review is None:
            continue
        cutoff = parse_ts(review["cutoff"])
        late_orders = [
            order for order in sorted(data.orders_as_of(at), key=lambda row: row["id"])
            if order["region"] in subject["regions"]
            and order["_received"] > cutoff
            and order["_refund"] is not None
            and order["_refund"] > 0
        ]
        late_refund = int(round(sum(float(order["_refund"]) for order in late_orders) * 100))
        if late_refund < min_retroactive_fen or not late_orders:
            continue
        executed = subject["status"] == "executed"
        if subject["status"] in {"signed", "executed"}:
            action = "revise_conclusion_not_contract"
            message = (
                "迟到退款可能推翻统计结论；已履约广告不能当成未花费，合同保持，结论需修订"
                if executed
                else "迟到退款可能推翻统计结论；已下单投放按已发生成本保留，结论需修订"
            )
        else:
            action = "reopen_review"
            message = "迟到退款可能推翻统计结论；新增投放尚未执行，进入重新审批，不能静默继续"
        flagged.append({
            "execution_id": subject["reference"],
            "proposal_id": subject["proposal_id"],
            "campaign": subject["campaign"],
            "late_refund_fen": late_refund,
            "late_orders": [order["id"] for order in late_orders],
            "review_id": review["review_id"],
            "review_cutoff": review["cutoff"],
            "action": action,
            "message": message,
            "execution_status": subject["status"],
        })
    return flagged
