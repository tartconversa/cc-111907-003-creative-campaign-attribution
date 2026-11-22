"""命令行入口：只使用标准库。

示例
----
python -m campaign_app init  --work work --name demo --inputs inputs
python -m campaign_app status --workspace work/demo
python -m campaign_app describe --workspace work/demo --cutoff 2026-11-18T00:00:00Z
"""

from __future__ import annotations

from datetime import datetime
import argparse
import json
from pathlib import Path
import sys
from typing import Any

from .service import Service, ServiceError
from .workspace import Workspace, WorkspaceError, resolve_workspace


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _open(args: argparse.Namespace) -> Service:
    if args.workspace is None:
        raise WorkspaceError("必须指定 --workspace（或使用 init 创建）")
    root = resolve_workspace(args.workspace)
    inputs = Path(args.inputs).resolve() if getattr(args, "inputs", None) else None
    workspace = Workspace.open(root, inputs_dir=inputs)
    recorded = workspace.state["workspace"].get("inputs_dir")
    if inputs is not None and recorded and Path(recorded).resolve() != inputs:
        raise WorkspaceError(
            f"工作目录登记的来源目录是 {recorded}，与 --inputs {inputs} 不一致；"
            "请使用原来源目录或新建工作目录"
        )
    return Service(workspace)


def _load_json_arg(value: str) -> Any:
    if value.startswith("@"):
        return json.loads(Path(value[1:]).read_text(encoding="utf-8"))
    if value.startswith("{") or value.startswith("["):
        return json.loads(value)
    path = Path(value)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return json.loads(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="campaign_app",
        description="AI 内容活动的增量贡献与预算续投（本地合成资料、标准库实现）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="创建隔离工作目录")
    init.add_argument("--work", required=True, help="工作目录根")
    init.add_argument("--name", required=True, help="工作目录名（字母/数字/._-）")
    init.add_argument("--inputs", default="inputs", help="只读来源目录")
    init.add_argument("--budget-total-fen", type=int, default=600_000)
    init.add_argument("--at", default=None, help="登记的当前时间（UTC ISO-8601）")
    init.add_argument("--replace", action="store_true", help="覆盖已存在的工作目录")

    status = sub.add_parser("status", help="查看工作目录与来源状态")
    status.add_argument("--workspace", required=True)
    status.add_argument("--inputs", default=None)

    issues = sub.add_parser("issues", help="列出来源问题")
    issues.add_argument("--workspace", required=True)
    issues.add_argument("--inputs", default=None)

    imp = sub.add_parser("import", help="导入与契约相同字段的新记录（不改来源文件）")
    imp.add_argument("--workspace", required=True)
    imp.add_argument("--dataset", required=True)
    imp.add_argument("--file", required=True, help="JSON 数组文件或 @file")
    imp.add_argument("--note", default="")
    imp.add_argument("--at", default=None)

    desc = sub.add_parser("describe", help="观察性描述（不构成增量）")
    desc.add_argument("--workspace", required=True)
    desc.add_argument("--cutoff", required=True)
    desc.add_argument("--campaign", default=None)

    design = sub.add_parser("design-create", help="登记并冻结对照安排")
    design.add_argument("--workspace", required=True)
    design.add_argument("--campaign", required=True)
    design.add_argument("--unit-type", choices=["region", "person"], default="region")
    design.add_argument("--planned-start", required=True)
    design.add_argument("--planned-end", required=True)
    design.add_argument("--observation-end", required=True)
    design.add_argument("--seed", type=int, required=True)
    design.add_argument("--allocation-basis", required=True)
    design.add_argument("--analysis-spec", default="stratified_baseline",
                        choices=["stratified_baseline", "stratified_equal", "unstratified"])
    design.add_argument("--normalization", default="per_1000_baseline",
                        choices=["total", "per_1000_baseline"])
    design.add_argument("--alpha", type=float, default=0.05)
    design.add_argument("--confidence", type=float, default=0.95)
    design.add_argument("--maturity-days", type=int, default=None)
    design.add_argument("--refund-maturity-days", type=int, default=None,
                        help="退款实现所需天数；不得短于取消成熟期")
    design.add_argument("--max-looks", type=int, default=1)
    design.add_argument("--regions", default=None, help="逗号分隔的合格地区")
    design.add_argument("--budget-cap-fen", type=int, default=None)
    design.add_argument("--assignment", default=None, help="外部已登记分组 JSON: {unit: arm}")
    design.add_argument("--notes", default="")
    design.add_argument("--late-registration", action="store_true",
                        help="承认这是投放后才登记的回顾性设计（将被标记为非确认性）")
    design.add_argument("--at", default=None)

    revise = sub.add_parser("design-revise", help="登记新的决策版本")
    revise.add_argument("--workspace", required=True)
    revise.add_argument("--design-id", required=True)
    revise.add_argument("--reason", required=True)
    revise.add_argument("--changes", required=True, help="JSON 对象")
    revise.add_argument("--rerandomize", action="store_true")
    revise.add_argument("--seed", type=int, default=None)
    revise.add_argument("--at", default=None)

    designs = sub.add_parser("designs", help="列出设计版本")
    designs.add_argument("--workspace", required=True)

    review = sub.add_parser("review", help="按冻结设计运行一次观察")
    review.add_argument("--workspace", required=True)
    review.add_argument("--design-id", required=True)
    review.add_argument("--cutoff", required=True)
    review.add_argument("--permutation-draws", type=int, default=10_000)
    review.add_argument("--bootstrap-draws", type=int, default=2_000)
    review.add_argument("--at", default=None)

    reviews = sub.add_parser("reviews", help="列出观察")
    reviews.add_argument("--workspace", required=True)
    reviews.add_argument("--design-id", default=None)

    show = sub.add_parser("review-show", help="查看一次观察的完整证据")
    show.add_argument("--workspace", required=True)
    show.add_argument("--review-id", required=True)

    decide = sub.add_parser("decide", help="登记继续/暂停/结束")
    decide.add_argument("--workspace", required=True)
    decide.add_argument("--review-id", required=True)
    decide.add_argument("--action", required=True, choices=["continue", "pause", "end"])
    decide.add_argument("--rationale", required=True)
    decide.add_argument("--note", default="")
    decide.add_argument("--acknowledge-deviation", action="store_true",
                        help="显式确认该决定偏离登记的观察计划或统计建议")
    decide.add_argument("--at", default=None)

    decisions = sub.add_parser("decisions", help="列出决策")
    decisions.add_argument("--workspace", required=True)

    ledger = sub.add_parser("ledger", help="预算台账")
    ledger.add_argument("--workspace", required=True)
    ledger.add_argument("--at", default=None)

    propose = sub.add_parser("propose", help="登记续投方案（确认前不形成占用）")
    propose.add_argument("--workspace", required=True)
    propose.add_argument("--campaign", required=True)
    propose.add_argument("--regions", required=True, help="逗号分隔")
    propose.add_argument("--requested-fen", type=int, required=True)
    propose.add_argument("--start", required=True)
    propose.add_argument("--end", required=True)
    propose.add_argument("--ttl-hours", type=int, default=72)
    propose.add_argument("--rationale", required=True)
    propose.add_argument("--at", default=None)

    compare = sub.add_parser("compare", help="比较候选方案")
    compare.add_argument("--workspace", required=True)
    compare.add_argument("--proposal", action="append", required=True)
    compare.add_argument("--at", default=None)

    confirm = sub.add_parser("confirm", help="确认方案（形成预算占用）")
    confirm.add_argument("--workspace", required=True)
    confirm.add_argument("--proposal-id", required=True)
    confirm.add_argument("--rationale", required=True)
    confirm.add_argument("--expected-remaining-fen", type=int, default=None)
    confirm.add_argument("--at", default=None)

    execute = sub.add_parser("execute", help="回填下单/取消/执行")
    execute.add_argument("--workspace", required=True)
    execute.add_argument("--proposal-id", required=True)
    execute.add_argument("--status", required=True, choices=["signed", "cancelled", "executed"])
    execute.add_argument("--executed-fen", type=int, default=None)
    execute.add_argument("--note", default="")
    execute.add_argument("--at", default=None)

    expire = sub.add_parser("expire", help="列出过期确认")
    expire.add_argument("--workspace", required=True)
    expire.add_argument("--at", default=None)

    refunds = sub.add_parser("refund-check", help="迟到退款与重新审批")
    refunds.add_argument("--workspace", required=True)
    refunds.add_argument("--min-retroactive-fen", type=int, default=0)
    refunds.add_argument("--at", default=None)

    audit = sub.add_parser("audit", help="查看并校验审计链")
    audit.add_argument("--workspace", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            workspace = Workspace.create(
                Path(args.work),
                args.name,
                inputs_dir=Path(args.inputs).resolve(),
                budget_total_fen=args.budget_total_fen,
                now=datetime.fromisoformat(args.at.replace("Z", "+00:00")) if args.at else None,
                replace=args.replace,
            )
            _print({
                "workspace": workspace.state["workspace"],
                "path": str(workspace.root),
                "status": Service(workspace).status(),
            })
            return 0
        with _open(args) as service:
            _dispatch(service, args)
        return 0
    except (WorkspaceError, ServiceError, ValueError) as exc:
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}, ensure_ascii=False),
              file=sys.stderr)
        return 2


def _dispatch(service: Service, args: argparse.Namespace) -> None:
    if args.command == "status":
        _print(service.status())
    elif args.command == "issues":
        _print(service.issues())
    elif args.command == "import":
        payload = _load_json_arg(args.file)
        _print(service.import_rows(args.dataset, payload, at=args.at, note=args.note))
    elif args.command == "describe":
        _print(service.describe(args.cutoff, campaign=args.campaign))
    elif args.command == "design-create":
        assignment = _load_json_arg(args.assignment) if args.assignment else None
        regions = [part.strip() for part in args.regions.split(",")] if args.regions else None
        _print(service.create_design(
            campaign=args.campaign,
            unit_type=args.unit_type,
            planned_start=args.planned_start,
            planned_end=args.planned_end,
            observation_end=args.observation_end,
            seed=args.seed,
            allocation_basis=args.allocation_basis,
            analysis_spec=args.analysis_spec,
            normalization=args.normalization,
            alpha=args.alpha,
            confidence=args.confidence,
            maturity_days=args.maturity_days,
            refund_maturity_days=args.refund_maturity_days,
            max_looks=args.max_looks,
            regions=regions,
            budget_cap_fen=args.budget_cap_fen,
            external_assignment=assignment,
            notes=args.notes,
            late_registration=args.late_registration,
            at=args.at,
        ))
    elif args.command == "design-revise":
        _print(service.revise_design(
            args.design_id,
            at=args.at,
            reason=args.reason,
            changes=_load_json_arg(args.changes),
            rerandomize=args.rerandomize,
            seed=args.seed,
        ))
    elif args.command == "designs":
        _print(service.list_designs())
    elif args.command == "review":
        _print(service.run_review(
            args.design_id,
            cutoff=args.cutoff,
            at=args.at,
            permutation_draws=args.permutation_draws,
            bootstrap_draws=args.bootstrap_draws,
        ))
    elif args.command == "reviews":
        _print(service.list_reviews(args.design_id))
    elif args.command == "review-show":
        _print(service.show_review(args.review_id))
    elif args.command == "decide":
        _print(service.decide(
            args.review_id,
            action=args.action,
            rationale=args.rationale,
            at=args.at,
            note=args.note,
            acknowledge_deviation=args.acknowledge_deviation,
        ))
    elif args.command == "decisions":
        _print(service.list_decisions())
    elif args.command == "ledger":
        _print(service.ledger(at=args.at))
    elif args.command == "propose":
        regions = [part.strip() for part in args.regions.split(",")]
        _print(service.create_proposal(
            campaign=args.campaign,
            regions=regions,
            requested_fen=args.requested_fen,
            start=args.start,
            end=args.end,
            ttl_hours=args.ttl_hours,
            rationale=args.rationale,
            at=args.at,
        ))
    elif args.command == "compare":
        _print(service.compare(args.proposal, at=args.at))
    elif args.command == "confirm":
        _print(service.confirm(
            args.proposal_id,
            rationale=args.rationale,
            at=args.at,
            expected_remaining_fen=args.expected_remaining_fen,
        ))
    elif args.command == "execute":
        _print(service.record_execution(
            args.proposal_id,
            status=args.status,
            executed_fen=args.executed_fen,
            note=args.note,
            at=args.at,
        ))
    elif args.command == "expire":
        _print(service.expire(at=args.at))
    elif args.command == "refund-check":
        _print(service.refund_check(
            at=args.at, min_retroactive_fen=args.min_retroactive_fen,
        ))
    elif args.command == "audit":
        _print(service.audit())
    else:  # pragma: no cover
        raise ServiceError(f"未知命令: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
