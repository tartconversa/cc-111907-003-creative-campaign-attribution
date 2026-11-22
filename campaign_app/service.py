"""服务层：把来源装载、分析、预算与工作区状态串起来。

所有会改变业务状态的命令都经由此处，统一写入原子状态与追加式审计链。
来源文件只读；导入记录、观察结果和预算状态都落在工作目录内。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from . import analysis, budget
from .errors import DomainError
from .source import SourceData, issue_summary
from .timeutil import format_ts, parse_ts
from .workspace import Workspace


class ServiceError(RuntimeError):
    pass


def translates(method):
    """把领域异常统一转成服务层错误，CLI 与调用方只需处理一种类型。"""
    def wrapper(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except DomainError as exc:
            raise ServiceError(str(exc)) from exc
    wrapper.__name__ = method.__name__
    wrapper.__doc__ = method.__doc__
    return wrapper


class Service:
    def __init__(self, workspace: Workspace) -> None:
        self.ws = workspace

    @classmethod
    def open(cls, root, *, inputs_dir=None) -> "Service":
        from pathlib import Path
        return cls(Workspace.open(Path(root), inputs_dir=inputs_dir))

    def __enter__(self) -> "Service":
        self._lock = self.ws.locked()
        self._lock.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        lock = getattr(self, "_lock", None)
        if lock is not None:
            lock.__exit__(exc_type, exc, tb)

    # ---- 通用 ------------------------------------------------------
    def _now(self, value: str | None) -> datetime:
        return self.ws.now(value)

    def _data(self) -> SourceData:
        return self.ws.load_source()

    def _event(self, event_type: str, at: datetime, payload: dict[str, Any]) -> None:
        self.ws.append_event(event_type, at, payload)

    def save(self) -> None:
        self.ws.save()

    # ---- 资料 ------------------------------------------------------
    @translates
    def status(self) -> dict[str, Any]:
        data = self._data()
        return {
            "workspace": self.ws.state["workspace"],
            "source": {
                "inputs_dir": str(self.ws.inputs_dir),
                "issues": issue_summary(data),
                "issue_count": len(data.issues),
                "duplicates_ignored": data.duplicate_counts,
                "imports": self.ws.state.get("import_history", []),
                "current_fingerprint": self.ws.fingerprint_sources(),
                "fingerprint_changed": self.ws.fingerprint_sources()
                != self.ws.state["workspace"].get("source_fingerprint"),
            },
            "counts": {
                "regions": len(data.regions),
                "campaigns": len(data.campaigns),
                "creatives": sum(len(v) for v in data.creatives.values()),
                "touches": len(data.touches),
                "orders": len(data.orders),
                "contracts": len(data.contracts),
                "designs": len(self.ws.state["designs"]),
                "reviews": len(self.ws.state["reviews"]),
                "plans": len(self.ws.state["plans"]),
                "executions": len(self.ws.state["executions"]),
            },
            "budget": budget.budget_ledger(data, self.ws.state, at=datetime.now().astimezone()),
        }

    @translates
    def issues(self) -> list[dict[str, str]]:
        return [issue.as_dict() for issue in self._data().issues]

    @translates
    def import_rows(
        self,
        dataset: str,
        rows: list[dict[str, Any]],
        *,
        at: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        from .source import DATASETS, SourceLoader

        if dataset not in DATASETS:
            raise ServiceError(f"未知数据集: {dataset}")
        errors = SourceLoader.validate_import(dataset, rows, self.ws.inputs_dir / "schema.json")
        if errors:
            raise ServiceError("导入记录字段不符：" + "; ".join(errors[:10]))
        now = self._now(at)
        result = self.ws.add_import(dataset, rows, now)
        history = self.ws.state.setdefault("import_history", [])
        entry = {
            **result,
            "at": format_ts(now),
            "note": note,
        }
        if not result.get("duplicate_import"):
            history.append(entry)
        self._event("source.imported", now, entry)
        self.save()
        return entry

    # ---- 描述 ------------------------------------------------------
    @translates
    def describe(self, cutoff: str, campaign: str | None = None) -> dict[str, Any]:
        data = self._data()
        cutoff_dt = parse_ts(cutoff, field="cutoff")
        return analysis.describe(data, cutoff_dt, campaign=campaign)

    # ---- 设计 ------------------------------------------------------
    @translates
    def create_design(self, **kwargs: Any) -> dict[str, Any]:
        at = self._now(kwargs.pop("at", None))
        data = self._data()
        preview_id = f"D{int(self.ws.state['counters'].get('design', 0)) + 1:03d}"
        preview = analysis.create_design(
            data,
            design_id=preview_id,
            created_at=at,
            **kwargs,
        )
        design_id = self.ws.next_id("design", "D")
        design = dict(preview, id=design_id)
        self.ws.state["designs"][design_id] = design
        self._event("design.registered", at, {
            "design_id": design_id,
            "campaign": design["campaign"],
            "unit_type": design["unit_type"],
            "version": design["version"],
            "allocation_fingerprint": design["allocation"]["fingerprint"],
            "assignment": design["allocation"]["assignment"],
            "analysis_spec": design["analysis_spec"],
            "observation_end": design["observation_end"],
            "max_looks": design["max_looks"],
        })
        self.save()
        return design

    @translates
    def revise_design(
        self,
        design_id: str,
        *,
        at: str | None = None,
        reason: str,
        changes: dict[str, Any],
        rerandomize: bool = False,
        seed: int | None = None,
    ) -> dict[str, Any]:
        if design_id not in self.ws.state["designs"]:
            raise ServiceError(f"设计不存在: {design_id}")
        old = self.ws.state["designs"][design_id]
        prior_reviews = [review for review in self.ws.state["reviews"]
                         if review.get("lineage") == old.get("lineage")]
        now = self._now(at)
        data = self._data()
        new_id = self.ws.next_id("design", "D")
        revised = analysis.revise_design(
            old,
            new_id=new_id,
            created_at=now,
            data=data,
            prior_reviews=len(prior_reviews),
            changes=changes,
            reason=reason,
            rerandomize=rerandomize,
            seed=seed,
        )
        for existing in self.ws.state["designs"].values():
            if existing.get("lineage") == old.get("lineage") and existing["status"] == "active":
                existing["status"] = "superseded"
        self.ws.state["designs"][new_id] = revised
        self._event("design.revised", now, {
            "design_id": new_id,
            "supersedes": design_id,
            "reason": reason,
            "prior_outcome_views": revised["prior_outcome_views"],
            "confirmatory": revised["confirmatory"],
            "changes": changes,
            "rerandomize": rerandomize,
            "allocation_fingerprint": revised["allocation"]["fingerprint"],
        })
        self.save()
        return revised

    @translates
    def list_designs(self) -> list[dict[str, Any]]:
        return [
            {
                "design_id": design["id"],
                "campaign": design["campaign"],
                "version": design["version"],
                "lineage": design["lineage"],
                "status": design["status"],
                "confirmatory": design.get("confirmatory", True),
                "prior_outcome_views": design.get("prior_outcome_views", False),
                "created_at": design["created_at"],
                "allocated_units": len(design["allocation"]["assignment"]),
            }
            for design in sorted(self.ws.state["designs"].values(),
                                 key=lambda row: (row["lineage"], row["version"]))
        ]

    # ---- 观察 ------------------------------------------------------
    @translates
    def run_review(
        self,
        design_id: str,
        *,
        cutoff: str,
        at: str | None = None,
        permutation_draws: int = 10_000,
        bootstrap_draws: int = 2_000,
    ) -> dict[str, Any]:
        design = self.ws.state["designs"].get(design_id)
        if design is None:
            raise ServiceError(f"设计不存在: {design_id}")
        if design["status"] != "active":
            raise ServiceError("该设计版本已被替代，不能再用旧版本观察；请使用当前版本")
        now = self._now(at)
        cutoff_dt = parse_ts(cutoff, field="cutoff")
        if cutoff_dt > now:
            raise ServiceError("数据截止点不能晚于当前登记时间")
        prior = [
            review for review in self.ws.state["reviews"]
            if review["design_id"] == design_id
        ]
        look = len(prior) + 1
        review_id = self.ws.next_id("review", "R")
        seed = int(design["allocation"].get("seed") or 0) + look * 7919
        data = self._data()
        result = analysis.run_review(
            data,
            design,
            cutoff=cutoff_dt,
            at=now,
            review_id=review_id,
            look=look,
            workspace_state=self.ws.state,
            seed=seed,
            permutation_draws=permutation_draws,
            bootstrap_draws=bootstrap_draws,
        )
        result["lineage"] = design["lineage"]
        result["source_fingerprint"] = self.ws.fingerprint_sources()
        result["data_as_of"] = {
            "touches": format_ts(cutoff_dt),
            "orders_received_before": format_ts(cutoff_dt),
            "note": "触达表没有 received 字段，按 occurred < cutoff 视为已知；"
                    "订单按 received < cutoff 选择最新版本",
        }
        self.ws.state["reviews"].append(result)
        if design.get("lineage"):
            self.ws.state["last_recheck"] = {
                "design_id": design_id,
                "review_id": review_id,
                "cutoff": result["cutoff"],
            }
        self._event("review.completed", now, {
            "review_id": review_id,
            "design_id": design_id,
            "campaign": design["campaign"],
            "cutoff": result["cutoff"],
            "look": look,
            "primary": {
                "spec_id": result["primary"]["spec_id"],
                "estimate": result["primary"]["estimate"],
                "ci": [result["primary"]["ci_low"], result["primary"]["ci_high"]],
                "p_two_sided": result["primary"]["p_two_sided"],
                "alpha_at_look": result["primary"]["alpha_at_look"],
            },
            "blocking_guards": result["blocking_guards"],
            "auto_win_eligible": result["auto_win_eligible"],
            "recommended_action": result["recommended_action"],
            "contamination": result["contamination"],
            "source_fingerprint": result["source_fingerprint"],
        })
        self.save()
        return result

    @translates
    def list_reviews(self, design_id: str | None = None) -> list[dict[str, Any]]:
        rows = []
        for review in self.ws.state["reviews"]:
            if design_id and review["design_id"] != design_id:
                continue
            rows.append({
                "review_id": review["review_id"],
                "design_id": review["design_id"],
                "decision_version": review["decision_version"],
                "campaign": review["campaign"],
                "cutoff": review["cutoff"],
                "look": review["look"],
                "estimate": review["primary"]["estimate"],
                "ci": [review["primary"]["ci_low"], review["primary"]["ci_high"]],
                "p_two_sided": review["primary"]["p_two_sided"],
                "blocking_guards": review["blocking_guards"],
                "auto_win_eligible": review["auto_win_eligible"],
                "recommended_action": review["recommended_action"],
            })
        return rows

    @translates
    def show_review(self, review_id: str) -> dict[str, Any]:
        for review in self.ws.state["reviews"]:
            if review["review_id"] == review_id:
                return review
        raise ServiceError(f"观察不存在: {review_id}")

    @translates
    def decide(self, review_id: str, *, action: str, rationale: str,
               at: str | None = None, note: str = "",
               acknowledge_deviation: bool = False) -> dict[str, Any]:
        review = self.show_review(review_id)
        errors, deviations = analysis.validate_decision(
            review, action, rationale, deviation_acknowledged=acknowledge_deviation,
        )
        if errors:
            raise ServiceError("决策被拒绝：" + "; ".join(errors))
        now = self._now(at)
        if review["look"] >= review["max_looks"] and action == "continue":
            raise ServiceError("已到达登记的最大查看次数，不能继续")
        for existing in self.ws.state.setdefault("decisions", []):
            if existing["review_id"] == review_id:
                raise ServiceError("该次观察已经登记过决策；如要改变，请登记新的决策版本")
        decision = {
            "decision_id": self.ws.next_id("decision", "K"),
            "review_id": review_id,
            "design_id": review["design_id"],
            "decision_version": review["decision_version"],
            "campaign": review["campaign"],
            "action": action,
            "rationale": rationale,
            "deviations": deviations,
            "deviation_acknowledged": bool(deviations) and acknowledge_deviation,
            "note": note,
            "at": format_ts(now),
            "auto_win_eligible": review["auto_win_eligible"],
            "recommended_action": review["recommended_action"],
            "blocking_guards": review["blocking_guards"],
            "cutoff": review["cutoff"],
        }
        self.ws.state["decisions"].append(decision)
        self._event("decision.recorded", now, decision)
        self.save()
        return decision

    @translates
    def list_decisions(self) -> list[dict[str, Any]]:
        return list(self.ws.state.get("decisions", []))

    # ---- 预算 ------------------------------------------------------
    @translates
    def ledger(self, *, at: str | None = None) -> dict[str, Any]:
        now = self._now(at)
        return budget.budget_ledger(self._data(), self.ws.state, at=now)

    @translates
    def create_proposal(self, **kwargs: Any) -> dict[str, Any]:
        # 先在候选编号上校验，通过后才占用计数，避免被拒绝的请求留下空洞编号。
        at = self._now(kwargs.pop("at", None))
        data = self._data()
        preview_id = f"P{int(self.ws.state['counters'].get('plan', 0)) + 1:03d}"
        preview = budget.build_proposal(
            data,
            self.ws.state,
            proposal_id=preview_id,
            created_at=at,
            **kwargs,
        )
        proposal_id = self.ws.next_id("plan", "P")
        proposal = dict(preview, id=proposal_id)
        self.ws.state["plans"][proposal_id] = proposal
        self._event("budget.proposed", at, {
            "proposal_id": proposal_id,
            "campaign": proposal["campaign"],
            "regions": proposal["regions"],
            "requested_fen": proposal["requested_fen"],
            "expires_at": proposal["expires_at"],
            "evidence": proposal["evidence"],
            "snapshot": proposal["snapshot"],
        })
        self.save()
        return proposal

    @translates
    def compare(self, proposal_ids: Iterable[str], *, at: str | None = None) -> list[dict[str, Any]]:
        now = self._now(at)
        rows = budget.compare_proposals(self._data(), self.ws.state, proposal_ids, at=now)
        self._event("budget.compared", now, {"proposals": list(proposal_ids), "rows": rows})
        self.save()
        return rows

    @translates
    def confirm(self, proposal_id: str, *, rationale: str, at: str | None = None,
                expected_remaining_fen: int | None = None) -> dict[str, Any]:
        now = self._now(at)
        snapshot = None
        if expected_remaining_fen is not None:
            snapshot = {"remaining_fen": expected_remaining_fen}
        updated = budget.confirm_proposal(
            self._data(),
            self.ws.state,
            proposal_id,
            at=now,
            expected_snapshot=snapshot,
            rationale=rationale,
        )
        self.ws.state["plans"][proposal_id] = updated
        self._event("budget.confirmed", now, {
            "proposal_id": proposal_id,
            "campaign": updated["campaign"],
            "regions": updated["regions"],
            "requested_fen": updated["requested_fen"],
            "snapshot": updated["snapshot"],
            "rationale": rationale,
        })
        self.save()
        return updated

    @translates
    def expire(self, *, at: str | None = None) -> list[dict[str, Any]]:
        now = self._now(at)
        flagged = budget.expire_or_flag(self._data(), self.ws.state, at=now)
        if flagged:
            self._event("budget.expired", now, {"flagged": flagged})
            self.save()
        return flagged

    @translates
    def record_execution(
        self,
        proposal_id: str,
        *,
        status: str,
        executed_fen: int | None,
        note: str,
        at: str | None = None,
    ) -> dict[str, Any]:
        now = self._now(at)
        execution_id = self.ws.next_id("execution", "E")
        execution = budget.record_execution(
            self._data(),
            self.ws.state,
            proposal_id,
            execution_id=execution_id,
            at=now,
            status=status,
            executed_fen=executed_fen,
            note=note,
        )
        self.ws.state["executions"][execution_id] = execution
        proposal = self.ws.state["plans"][proposal_id]
        if status == "cancelled":
            proposal["status"] = "cancelled"
        else:
            # 下单或执行都形成预算占用；实际金额以执行记录为准。
            proposal["status"] = "executed"
        proposal["execution_id"] = execution_id
        self._event("budget.execution_recorded", now, execution)
        self.save()
        return execution

    @translates
    def refund_check(self, *, at: str | None = None, min_retroactive_fen: int = 0) -> list[dict[str, Any]]:
        now = self._now(at)
        rows = budget.refund_review(self._data(), self.ws.state, at=now,
                                    min_retroactive_fen=min_retroactive_fen)
        if rows:
            self._event("budget.refund_review", now, {"flagged": rows})
            self.save()
        return rows

    # ---- 审计 ------------------------------------------------------
    @translates
    def audit(self) -> dict[str, Any]:
        return {
            "events": self.ws.read_audit(),
            "problems": self.ws.verify_audit(),
            "imports": self.ws.state.get("import_history", []),
        }
