"""来源资料装载、身份版本解析与业务流程校验。

设计要点
--------
* 来源文件只读；本模块从不写回 ``inputs``。
* 来源主键标识事实。带版本的事实（创意 revision、订单的 ``received`` 修订）
  按来源身份与版本解释；同一版本出现不同内容时保留冲突问题，不静默覆盖。
* 重复投递同一事实不产生新的业务活动：相同来源主键、或业务载荷完全相同的
  行只计一次，并在问题清单中留下说明。
* ``received`` 是事实进入系统的时点；任何分析截止点早于它的事实不得使用。
* null 表示缺失或尚未知，不按 0 处理。
* 输入顺序不代表业务顺序；所有汇总均先排序再计算。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
from typing import Any

from .timeutil import format_ts, parse_ts

DATASETS = (
    "regions",
    "campaigns",
    "creatives",
    "touches",
    "orders",
    "contracts",
    "observation_policy",
)

DATASET_KEYS = {
    "regions": ("region",),
    "campaigns": ("campaign",),
    "creatives": ("creative", "revision"),
    "touches": ("id",),
    "orders": ("id",),
    "contracts": ("id",),
    "observation_policy": ("id",),
}

STATUSES = {"signed", "executed", "cancelled"}
SOURCES = {"paid", "organic"}


@dataclass(frozen=True)
class Issue:
    severity: str  # error / warning / info
    code: str
    dataset: str
    identity: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "code": self.code,
            "dataset": self.dataset,
            "identity": self.identity,
            "message": self.message,
        }


@dataclass
class SourceData:
    regions: dict[str, dict[str, Any]] = field(default_factory=dict)
    campaigns: dict[str, dict[str, Any]] = field(default_factory=dict)
    creatives: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    touches: list[dict[str, Any]] = field(default_factory=list)
    orders: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    contracts: dict[str, dict[str, Any]] = field(default_factory=dict)
    policies: dict[str, dict[str, Any]] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    duplicate_counts: dict[str, int] = field(default_factory=dict)
    import_meta: list[dict[str, Any]] = field(default_factory=list)
    file_hashes: dict[str, str] = field(default_factory=dict)

    # ---- 截止点辅助 -------------------------------------------------
    def touches_as_of(self, as_of) -> list[dict[str, Any]]:
        """触达表没有 received 字段；按 occurred <= as_of 视为已知。
        这是输入契约的缺口，使用说明中单独声明。"""
        rows = [row for row in self.touches if row["_occurred"] <= as_of]
        return sorted(rows, key=lambda row: (row["_occurred"], row["id"]))

    def orders_as_of(self, as_of) -> list[dict[str, Any]]:
        """每个订单取 received <= as_of 的最新版本；没有可用版本则不计入。"""
        selected: list[dict[str, Any]] = []
        for identity in sorted(self.orders):
            eligible = [row for row in self.orders[identity] if row["_received"] <= as_of]
            if not eligible:
                continue
            eligible.sort(key=lambda row: (row["_received"], row.get("_version", 0)))
            selected.append(eligible[-1])
        return selected

    def order_version_at(self, order_id: str, as_of) -> dict[str, Any] | None:
        eligible = [row for row in self.orders.get(order_id, []) if row["_received"] <= as_of]
        if not eligible:
            return None
        eligible.sort(key=lambda row: (row["_received"], row.get("_version", 0)))
        return eligible[-1]

    def creatives_of_campaign(self, campaign: str) -> list[dict[str, Any]]:
        rows = [row for versions in self.creatives.values() for row in versions]
        return sorted(
            (row for row in rows if row["campaign"] == campaign),
            key=lambda row: (row["_live"], row["creative"], row["revision"]),
        )

    def creative_versions(self, creative: str) -> list[dict[str, Any]]:
        return sorted(
            self.creatives.get(creative, []),
            key=lambda row: (row["revision"], row["_live"]),
        )

    def creative_campaign(self, creative: str) -> str | None:
        versions = self.creatives.get(creative)
        if not versions:
            return None
        return versions[0]["campaign"]

    def campaign_for_creative(self, creative: str) -> dict[str, Any] | None:
        campaign = self.creative_campaign(creative)
        return self.campaigns.get(campaign) if campaign else None

    def policy(self, policy_id: str = "default") -> dict[str, Any]:
        policy = self.policies.get(policy_id)
        if policy is None:
            raise KeyError(f"观察口径不存在: {policy_id}")
        return policy


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"缺少来源文件: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"来源文件不是合法 JSON: {path}: {exc}") from exc


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _identity(dataset: str, row: dict[str, Any]) -> str:
    keys = DATASET_KEYS[dataset]
    if dataset == "creatives":
        return f"{row.get('creative')}@{row.get('revision')}"
    return str(row.get(keys[0]))


def _payload(row: dict[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)


def _parse_decimal(value: Any, *, field_name: str, identity: str, issues: list[Issue],
                   dataset: str, required: bool = True) -> Decimal | None:
    if value is None:
        issues.append(Issue("warning", "null_fact", dataset, identity,
                            f"{field_name} 为 null（缺失/尚未知），不按 0 处理"))
        if required:
            return None
        return None
    if isinstance(value, bool):
        issues.append(Issue("error", "bad_number", dataset, identity,
                            f"{field_name} 不能是布尔值: {value!r}"))
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        issues.append(Issue("error", "bad_number", dataset, identity,
                            f"{field_name} 不是合法数字: {value!r}"))
        return None
    if not parsed.is_finite():
        issues.append(Issue("error", "bad_number", dataset, identity,
                            f"{field_name} 不是有限数字: {value!r}"))
        return None
    return parsed


def _parse_time(value: Any, *, field_name: str, identity: str, issues: list[Issue],
                dataset: str) -> Any | None:
    if value is None:
        issues.append(Issue("error", "null_time", dataset, identity,
                            f"{field_name} 为 null，无法确定业务时点"))
        return None
    try:
        return parse_ts(str(value), field=field_name)
    except ValueError as exc:
        issues.append(Issue("error", "bad_time", dataset, identity, str(exc)))
        return None


def _check_columns(dataset: str, row: dict[str, Any], schema: dict[str, Any],
                   issues: list[Issue]) -> bool:
    expected = set(schema[dataset]["columns"])
    if set(row) != expected:
        issues.append(Issue(
            "error", "bad_columns", dataset, _identity(dataset, row),
            f"字段集合与契约不符：多出 {sorted(set(row) - expected)}，缺少 {sorted(expected - set(row))}",
        ))
        return False
    return True


class SourceLoader:
    def __init__(
        self,
        inputs_dir: Path,
        *,
        extra_records: dict[str, list[dict[str, Any]]] | None = None,
        import_meta: list[dict[str, Any]] | None = None,
        verify_manifest: bool = True,
    ) -> None:
        self.inputs_dir = Path(inputs_dir)
        self.extra_records = extra_records or {}
        self.import_meta = import_meta or []
        self.verify_manifest = verify_manifest

    # ---- 主入口 -----------------------------------------------------
    def load(self) -> SourceData:
        data = SourceData(import_meta=list(self.import_meta))
        schema = _read_json(self.inputs_dir / "schema.json")
        raw: dict[str, list[dict[str, Any]]] = {}
        for dataset in DATASETS:
            path = self.inputs_dir / f"{dataset}.json"
            rows = _read_json(path)
            if not isinstance(rows, list):
                raise ValueError(f"{path} 必须是 JSON 数组")
            raw[dataset] = list(rows)
            data.file_hashes[path.name] = _sha256(path)
            if dataset in self.extra_records:
                raw[dataset].extend(self.extra_records[dataset])

        if self.verify_manifest:
            self._verify_manifest(data)

        # 统一字段与身份检查
        checked: dict[str, list[dict[str, Any]]] = {}
        for dataset in DATASETS:
            checked[dataset] = []
            for row in raw[dataset]:
                if not isinstance(row, dict):
                    data.issues.append(Issue("error", "bad_row", dataset, "?", "行必须是 JSON 对象"))
                    continue
                if not _check_columns(dataset, row, schema, data.issues):
                    continue
                identity = _identity(dataset, row)
                if dataset != "creatives" and (row.get(DATASET_KEYS[dataset][0]) in (None, "")):
                    data.issues.append(Issue("error", "missing_key", dataset, "?", "来源主键为空"))
                    continue
                checked[dataset].append(dict(row))

        data.regions = self._single_version(dataset="regions", rows=checked["regions"], data=data)
        data.campaigns = self._single_version(dataset="campaigns", rows=checked["campaigns"], data=data)
        data.policies = self._single_version(dataset="observation_policy", rows=checked["observation_policy"], data=data)
        data.contracts = self._single_version(dataset="contracts", rows=checked["contracts"], data=data)
        data.creatives = self._creative_versions(checked["creatives"], data)
        data.touches = self._touch_rows(checked["touches"], data)
        data.orders = self._order_versions(checked["orders"], data)

        self._validate_campaigns(data)
        self._validate_regions(data)
        self._validate_creatives(data)
        self._validate_touches(data)
        self._validate_orders(data)
        self._validate_contracts(data)
        self._validate_policies(data)
        return data

    # ---- 清单 -------------------------------------------------------
    def _verify_manifest(self, data: SourceData) -> None:
        manifest_path = self.inputs_dir / "manifest.json"
        if not manifest_path.exists():
            data.issues.append(Issue("warning", "manifest_missing", "manifest", "-", "缺少清单，无法核对来源字节"))
            return
        manifest = _read_json(manifest_path)
        for filename, expected in manifest.get("files", {}).items():
            path = self.inputs_dir / filename
            if not path.exists():
                data.issues.append(Issue("error", "manifest_file_missing", "manifest", filename,
                                         "清单声明的来源文件不存在"))
                continue
            actual = _sha256(path)
            if actual != expected:
                data.issues.append(Issue("error", "manifest_mismatch", "manifest", filename,
                                         "来源文件字节与清单哈希不一致，事实可能已被改写"))
        if manifest.get("synthetic") is not True:
            data.issues.append(Issue("warning", "not_marked_synthetic", "manifest", "-",
                                     "清单未标记 synthetic=true，请确认资料性质"))

    # ---- 版本与去重 -------------------------------------------------
    @staticmethod
    def _single_version(dataset: str, rows: list[dict[str, Any]], data: SourceData) -> dict[str, dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(_identity(dataset, row), []).append(row)
        result: dict[str, dict[str, Any]] = {}
        for identity in sorted(grouped):
            versions = grouped[identity]
            payloads = {_payload(row) for row in versions}
            if len(payloads) > 1:
                data.issues.append(Issue(
                    "error", "source_version_conflict", dataset, identity,
                    "同一来源身份出现内容不同的版本，无法判定哪一个为事实；该身份不参与分析",
                ))
                continue
            if len(versions) > 1:
                data.duplicate_counts[dataset] = data.duplicate_counts.get(dataset, 0) + len(versions) - 1
                data.issues.append(Issue(
                    "info", "duplicate_delivery", dataset, identity,
                    f"重复投递 {len(versions) - 1} 次，不产生新的业务活动",
                ))
            result[identity] = dict(versions[0])
        return result

    @staticmethod
    def _creative_versions(rows: list[dict[str, Any]], data: SourceData) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault((str(row["creative"]), int(row["revision"])), []).append(row)
        result: dict[str, list[dict[str, Any]]] = {}
        for key in sorted(grouped, key=lambda item: (item[0], item[1])):
            versions = grouped[key]
            payloads = {_payload(row) for row in versions}
            identity = f"{key[0]}@{key[1]}"
            if len(payloads) > 1:
                data.issues.append(Issue(
                    "error", "source_version_conflict", "creatives", identity,
                    "同一创意版本出现内容不同的来源行；该版本不参与分析",
                ))
                continue
            if len(versions) > 1:
                data.duplicate_counts["creatives"] = data.duplicate_counts.get("creatives", 0) + len(versions) - 1
                data.issues.append(Issue("info", "duplicate_delivery", "creatives", identity,
                                         "重复投递同一创意版本，只计一次"))
            row = dict(versions[0])
            row["_identity"] = identity
            row["_live"] = _parse_time(row.get("live"), field_name="live", identity=identity,
                                       issues=data.issues, dataset="creatives")
            result.setdefault(str(row["creative"]), []).append(row)
        for creative in result:
            result[creative].sort(key=lambda row: (row["revision"], row["_live"] or row["live"]))
        return result

    @staticmethod
    def _touch_rows(rows: list[dict[str, Any]], data: SourceData) -> list[dict[str, Any]]:
        by_id: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_id.setdefault(str(row["id"]), []).append(row)
        unique: list[dict[str, Any]] = []
        for identity in sorted(by_id):
            versions = by_id[identity]
            payloads = {_payload(row) for row in versions}
            if len(payloads) > 1:
                data.issues.append(Issue("error", "source_version_conflict", "touches", identity,
                                         "同一触达主键出现内容不同的来源行；该触达不参与分析"))
                continue
            if len(versions) > 1:
                data.duplicate_counts["touches"] = data.duplicate_counts.get("touches", 0) + len(versions) - 1
                data.issues.append(Issue("info", "duplicate_delivery", "touches", identity,
                                         "重复投递同一触达，不增加触达"))
            row = dict(versions[0])
            row["_occurred"] = _parse_time(row.get("occurred"), field_name="occurred", identity=identity,
                                           issues=data.issues, dataset="touches")
            unique.append(row)

        # 自然键去重：不同来源主键但业务载荷相同的回传，只计一次。
        unique.sort(key=lambda row: (str(row["_occurred"]), row["id"]))
        kept: list[dict[str, Any]] = []
        aliases: dict[str, str] = {}
        seen: dict[tuple, str] = {}
        for row in unique:
            natural = (
                row["person"], row["region"], row["creative"],
                row["_occurred"].isoformat() if row["_occurred"] else row["occurred"],
                row["source"],
            )
            if natural in seen:
                aliases[row["id"]] = seen[natural]
                data.duplicate_counts["touches"] = data.duplicate_counts.get("touches", 0) + 1
                data.issues.append(Issue("info", "duplicate_business_fact", "touches", row["id"],
                                         f"与 {seen[natural]} 表示同一触达，不增加触达"))
                continue
            seen[natural] = row["id"]
            kept.append(row)
        for row in kept:
            if row["id"] in aliases:
                row["_alias_of"] = aliases[row["id"]]
        return kept

    @staticmethod
    def _order_versions(rows: list[dict[str, Any]], data: SourceData) -> dict[str, list[dict[str, Any]]]:
        by_id: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_id.setdefault(str(row["id"]), []).append(row)
        result: dict[str, list[dict[str, Any]]] = {}
        for identity in sorted(by_id):
            versions = by_id[identity]
            parsed: dict[Any, list[dict[str, Any]]] = {}
            for row in versions:
                received = _parse_time(row.get("received"), field_name="received", identity=identity,
                                       issues=data.issues, dataset="orders")
                if received is None:
                    continue
                parsed.setdefault(received, []).append(row)
            usable: list[dict[str, Any]] = []
            hard_conflict = False
            for received in sorted(parsed):
                same_time = parsed[received]
                payloads = {_payload(row) for row in same_time}
                if len(payloads) > 1:
                    hard_conflict = True
                    data.issues.append(Issue(
                        "error", "source_version_conflict", "orders", identity,
                        f"received={format_ts(received)} 的同一订单版本内容不同，该订单不参与分析",
                    ))
                    continue
                if len(same_time) > 1:
                    data.duplicate_counts["orders"] = data.duplicate_counts.get("orders", 0) + len(same_time) - 1
                    data.issues.append(Issue("info", "duplicate_delivery", "orders", identity,
                                             "重复投递同一订单版本，不增加订单"))
                row = dict(same_time[0])
                row["_received"] = received
                row["_ordered"] = _parse_time(row.get("ordered"), field_name="ordered", identity=identity,
                                              issues=data.issues, dataset="orders")
                row["_version"] = len(usable)
                usable.append(row)
            if hard_conflict:
                continue
            if len(usable) > 1:
                data.issues.append(Issue(
                    "info", "order_version_update", "orders", identity,
                    "发现同一订单的后续版本；按 received 截止点选择可用版本，历史版本保留",
                ))
            if usable:
                result[identity] = usable

        # 自然键去重：不同订单主键但业务载荷完全相同，只计一次。
        latest: list[tuple[str, dict[str, Any]]] = []
        for identity, versions in result.items():
            last = versions[-1]
            latest.append((identity, last))
        latest.sort(key=lambda item: (item[1]["_ordered"].isoformat() if item[1]["_ordered"] else "",
                                      item[0]))
        seen: dict[tuple, str] = {}
        keep_ids: set[str] = set()
        for identity, row in latest:
            natural = (
                row["person"], row["region"],
                row["_ordered"].isoformat() if row["_ordered"] else row["ordered"],
                str(row["revenue"]), str(row["cost"]), str(row["refund"]),
            )
            if natural in seen:
                data.duplicate_counts["orders"] = data.duplicate_counts.get("orders", 0) + 1
                data.issues.append(Issue("info", "duplicate_business_fact", "orders", identity,
                                         f"与 {seen[natural]} 表示同一订单，不增加订单"))
                continue
            seen[natural] = identity
            keep_ids.add(identity)
        return {identity: versions for identity, versions in result.items() if identity in keep_ids}

    # ---- 业务校验 ---------------------------------------------------
    def _validate_regions(self, data: SourceData) -> None:
        for region, row in sorted(data.regions.items()):
            if row.get("group") in (None, ""):
                data.issues.append(Issue("error", "missing_group", "regions", region,
                                         "地区缺少相似层 group，无法做分层比较"))
            baseline = row.get("baseline_customers")
            if baseline is None:
                data.issues.append(Issue("warning", "null_baseline", "regions", region,
                                         "常规客群规模为 null，分层加权与基线调整不可用"))
            elif isinstance(baseline, bool) or not isinstance(baseline, int) or baseline < 0:
                data.issues.append(Issue("error", "bad_baseline", "regions", region,
                                         "baseline_customers 必须是非负整数"))

    def _validate_campaigns(self, data: SourceData) -> None:
        for campaign, row in sorted(data.campaigns.items()):
            start = _parse_time(row.get("start"), field_name="start", identity=campaign,
                                issues=data.issues, dataset="campaigns")
            end = _parse_time(row.get("end"), field_name="end", identity=campaign,
                              issues=data.issues, dataset="campaigns")
            budget = row.get("max_budget_fen")
            if budget is None:
                data.issues.append(Issue("warning", "null_budget", "campaigns", campaign,
                                         "活动预算为 null，预算上限未知"))
            elif isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
                data.issues.append(Issue("error", "bad_budget", "campaigns", campaign,
                                         "max_budget_fen 必须是非负整数"))
            if row.get("exclusive_family") in (None, ""):
                data.issues.append(Issue("error", "missing_family", "campaigns", campaign,
                                         "缺少 exclusive_family，无法判定地区互斥"))
            if start and end and start >= end:
                data.issues.append(Issue("error", "bad_campaign_window", "campaigns", campaign,
                                         "活动起点必须早于终点（左闭右开）"))

    def _validate_creatives(self, data: SourceData) -> None:
        for creative, versions in sorted(data.creatives.items()):
            campaigns = {row["campaign"] for row in versions}
            if len(campaigns) > 1:
                data.issues.append(Issue("error", "creative_campaign_conflict", "creatives", creative,
                                         "同一创意的不同版本指向多个活动"))
            for row in versions:
                identity = row["_identity"]
                if row["campaign"] not in data.campaigns:
                    data.issues.append(Issue("error", "unknown_campaign", "creatives", identity,
                                             f"活动不存在: {row['campaign']}"))
                if row["_live"] is None:
                    continue
                campaign = data.campaigns.get(row["campaign"])
                if not campaign:
                    continue
                try:
                    start = parse_ts(campaign["start"])
                    end = parse_ts(campaign["end"])
                except (KeyError, ValueError):
                    continue
                if not (start <= row["_live"] < end):
                    data.issues.append(Issue("warning", "creative_outside_campaign", "creatives", identity,
                                             "创意上线时点不在活动计划区间内"))

    def _validate_touches(self, data: SourceData) -> None:
        for row in data.touches:
            identity = row["id"]
            if row["region"] not in data.regions:
                data.issues.append(Issue("error", "unknown_region", "touches", identity,
                                         f"地区不存在: {row['region']}"))
            if row["source"] not in SOURCES:
                data.issues.append(Issue("error", "bad_source", "touches", identity,
                                         f"source 必须是 paid/organic，收到 {row['source']!r}"))
            if not row.get("person"):
                data.issues.append(Issue("error", "missing_person", "touches", identity,
                                         "触达缺少合成人员键"))
            versions = data.creative_versions(row["creative"])
            if not versions:
                data.issues.append(Issue("error", "unknown_creative", "touches", identity,
                                         f"创意不存在: {row['creative']}"))
                continue
            campaign = data.campaigns.get(versions[0]["campaign"])
            occurred = row["_occurred"]
            if occurred is None:
                continue
            live_versions = [version for version in versions
                             if version["_live"] is not None and version["_live"] <= occurred]
            if not live_versions:
                data.issues.append(Issue("error", "touch_before_creative_live", "touches", identity,
                                         "触达早于所有创意版本上线时点"))
            if campaign:
                try:
                    start = parse_ts(campaign["start"])
                    end = parse_ts(campaign["end"])
                except (KeyError, ValueError):
                    start = end = None
                if start and end and not (start <= occurred < end):
                    data.issues.append(Issue("warning", "touch_outside_campaign", "touches", identity,
                                             "触达时点不在活动计划区间内"))

    def _validate_orders(self, data: SourceData) -> None:
        for identity in sorted(data.orders):
            versions = data.orders[identity]
            last = versions[-1]
            if last["region"] not in data.regions:
                data.issues.append(Issue("error", "unknown_region", "orders", identity,
                                         f"地区不存在: {last['region']}"))
            if not last.get("person"):
                data.issues.append(Issue("error", "missing_person", "orders", identity,
                                         "订单缺少合成人员键"))
            revenue = cost = refund = None
            for row in versions:
                if row["_ordered"] is None or row["_received"] is None:
                    continue
                if row["_received"] < row["_ordered"]:
                    data.issues.append(Issue("warning", "received_before_ordered", "orders", identity,
                                             "received 早于 ordered，事实进入系统时点可疑"))
                revenue = _parse_decimal(row.get("revenue"), field_name="revenue", identity=identity,
                                         issues=data.issues, dataset="orders")
                cost = _parse_decimal(row.get("cost"), field_name="cost", identity=identity,
                                      issues=data.issues, dataset="orders")
                refund = _parse_decimal(row.get("refund"), field_name="refund", identity=identity,
                                        issues=data.issues, dataset="orders")
                if revenue is not None and revenue < 0:
                    data.issues.append(Issue("error", "negative_money", "orders", identity,
                                             "revenue 不能为负"))
                if cost is not None and cost < 0:
                    data.issues.append(Issue("error", "negative_money", "orders", identity,
                                             "cost 不能为负"))
                if refund is not None and refund < 0:
                    data.issues.append(Issue("error", "negative_money", "orders", identity,
                                             "refund 不能为负"))
                if revenue is not None and refund is not None and refund > revenue:
                    data.issues.append(Issue("warning", "refund_over_revenue", "orders", identity,
                                             "累计退款超过收入，净贡献为负，请核对退款口径"))
                row["_revenue"] = revenue
                row["_cost"] = cost
                row["_refund"] = refund
                if revenue is None or cost is None or refund is None:
                    row["_net"] = None
                else:
                    row["_net"] = revenue - cost - refund

    def _validate_contracts(self, data: SourceData) -> None:
        for identity, row in sorted(data.contracts.items()):
            if row["campaign"] not in data.campaigns:
                data.issues.append(Issue("error", "unknown_campaign", "contracts", identity,
                                         f"活动不存在: {row['campaign']}"))
            if row["region"] not in data.regions:
                data.issues.append(Issue("error", "unknown_region", "contracts", identity,
                                         f"地区不存在: {row['region']}"))
            if row["status"] not in STATUSES:
                data.issues.append(Issue("error", "bad_contract_status", "contracts", identity,
                                         f"status 必须是 signed/executed/cancelled，收到 {row['status']!r}"))
            amount = row.get("amount_fen")
            if amount is None:
                data.issues.append(Issue("warning", "null_contract_amount", "contracts", identity,
                                         "合同金额为 null，无法计入预算占用"))
            elif isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
                data.issues.append(Issue("error", "bad_contract_amount", "contracts", identity,
                                         "amount_fen 必须是非负整数"))
            if not isinstance(row.get("cancellable"), bool):
                data.issues.append(Issue("error", "bad_cancellable", "contracts", identity,
                                         "cancellable 必须是布尔值"))

    def _validate_policies(self, data: SourceData) -> None:
        for identity, row in sorted(data.policies.items()):
            maturity = row.get("maturity_days")
            if isinstance(maturity, bool) or not isinstance(maturity, int) or maturity < 0:
                data.issues.append(Issue("error", "bad_maturity", "observation_policy", identity,
                                         "maturity_days 必须是非负整数"))
            confidence = row.get("confidence")
            if confidence is None or isinstance(confidence, bool) or not isinstance(confidence, (int, float)) \
                    or not (0 < float(confidence) < 1):
                data.issues.append(Issue("error", "bad_confidence", "observation_policy", identity,
                                         "confidence 必须在 (0,1) 之间"))
            if row.get("currency") in (None, ""):
                data.issues.append(Issue("error", "bad_currency", "observation_policy", identity,
                                         "currency 不能为空"))

    # ---- 导入 -------------------------------------------------------
    @staticmethod
    def validate_import(dataset: str, rows: Any, schema_path: Path) -> list[str]:
        """校验待导入记录；返回错误列表。导入不修改来源文件。"""
        if dataset not in DATASETS:
            return [f"未知数据集: {dataset}"]
        if not isinstance(rows, list):
            return ["导入文件必须是 JSON 数组"]
        schema = _read_json(schema_path)
        errors: list[str] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                errors.append(f"第 {index} 行必须是 JSON 对象")
                continue
            expected = set(schema[dataset]["columns"])
            if set(row) != expected:
                errors.append(f"第 {index} 行字段不符：多出 {sorted(set(row) - expected)}，"
                              f"缺少 {sorted(expected - set(row))}")
        return errors


def load_source(
    inputs_dir: Path,
    *,
    extra_records: dict[str, list[dict[str, Any]]] | None = None,
    import_meta: list[dict[str, Any]] | None = None,
    verify_manifest: bool = True,
) -> SourceData:
    return SourceLoader(
        inputs_dir,
        extra_records=extra_records,
        import_meta=import_meta,
        verify_manifest=verify_manifest,
    ).load()


def issue_summary(data: SourceData) -> dict[str, int]:
    counts = {"error": 0, "warning": 0, "info": 0}
    for issue in data.issues:
        counts[issue.severity] = counts.get(issue.severity, 0) + 1
    return counts
