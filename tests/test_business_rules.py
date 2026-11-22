"""针对题述关系的业务测试（全部使用本地合成资料）。"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

from synth import make_inputs  # noqa: E402
from campaign_app import analysis, budget, stats  # noqa: E402
from campaign_app.service import Service, ServiceError  # noqa: E402
from campaign_app.source import load_source  # noqa: E402
from campaign_app.workspace import Workspace, WorkspaceError  # noqa: E402


def utc(year, month, day, hour=0):
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


def big_fixture(root: Path, **kwargs) -> Path:
    treatment = tuple(f"R{i}" for i in range(1, 7))
    control = tuple(f"R{i}" for i in range(7, 13))
    return make_inputs(root, treatment_regions=treatment, control_regions=control, **kwargs)


class BaseWorkspaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="campaign-test-"))
        self.inputs = big_fixture(self.tmp / "inputs")
        self.work = self.tmp / "work"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def service(self, name: str = "ws", **kwargs) -> Service:
        workspace = Workspace.create(
            self.work, name, inputs_dir=self.inputs,
            now=kwargs.pop("now", utc(2025, 12, 31)), **kwargs,
        )
        return Service(workspace)

    def register(self, service: Service, assignment: dict[str, str], **overrides) -> dict:
        kwargs = dict(
            campaign="ad",
            unit_type="region",
            planned_start="2026-01-01T00:00:00Z",
            planned_end="2026-02-01T00:00:00Z",
            observation_end="2026-02-10T00:00:00Z",
            seed=11,
            allocation_basis="测试：相似层基线配对区块随机，观察前冻结",
            maturity_days=7,
            max_looks=1,
            external_assignment=assignment,
            at="2025-12-31T00:00:00Z",
        )
        kwargs.update(overrides)
        return service.create_design(**kwargs)

    @staticmethod
    def split(treatment=range(1, 7), control=range(7, 13)) -> dict[str, str]:
        return {f"R{i}": "treatment" for i in treatment} | {
            f"R{i}": "control" for i in control
        }


class SourceIdentityTests(BaseWorkspaceTest):
    def test_duplicate_delivery_does_not_add_touch_or_order(self):
        data = load_source(self.inputs, verify_manifest=False)
        touch = dict(next(iter(data.touches)))
        order = dict(data.orders[sorted(data.orders)[0]][0])
        for key in ("_occurred", "_received", "_ordered", "_version", "_net",
                    "_revenue", "_cost", "_refund", "_identity", "_alias_of"):
            touch.pop(key, None)
            order.pop(key, None)
        duplicated = load_source(
            self.inputs,
            extra_records={"touches": [touch], "orders": [order]},
            verify_manifest=False,
        )
        self.assertEqual(len(duplicated.touches), len(data.touches))
        self.assertEqual(len(duplicated.orders), len(data.orders))
        self.assertGreaterEqual(duplicated.duplicate_counts["touches"], 1)

    def test_same_identity_conflicting_versions_are_kept_as_issues(self):
        data = load_source(self.inputs, verify_manifest=False)
        order_id = sorted(data.orders)[0]
        row = dict(data.orders[order_id][0])
        for key in list(row):
            if key.startswith("_"):
                row.pop(key)
        conflicting = dict(row, revenue="999999")
        loaded = load_source(
            self.inputs,
            extra_records={"orders": [conflicting]},
            verify_manifest=False,
        )
        self.assertNotIn(order_id, loaded.orders)
        self.assertTrue(any(issue.code == "source_version_conflict" for issue in loaded.issues))

    def test_versioned_order_uses_received_cutoff(self):
        data = load_source(self.inputs, verify_manifest=False)
        order_id = sorted(data.orders)[0]
        first = dict(data.orders[order_id][0])
        for key in list(first):
            if key.startswith("_"):
                first.pop(key)
        earlier_cutoff = utc(2026, 1, 10)
        updated = dict(first, refund="50", received="2026-01-20T00:00:00Z")
        loaded = load_source(
            self.inputs,
            extra_records={"orders": [updated]},
            verify_manifest=False,
        )
        before = loaded.order_version_at(order_id, earlier_cutoff)
        after = loaded.order_version_at(order_id, utc(2026, 1, 21))
        self.assertEqual(before["refund"], first["refund"])
        self.assertEqual(after["refund"], "50")

    def test_refund_maturity_defers_refund_but_not_cancellation(self):
        # 订单 1 月 2 日下单；取消成熟 3 天，退款成熟 20 天。
        # 2 月 10 日的观察：订单已成熟，但退款尚未实现，净贡献应等于收入减成本。
        self.inputs = big_fixture(self.tmp / "inputs-refund", treatment_refund=200,
                                  control_refund=0, order_start_day=26)
        service = self.service()
        design = self.register(service, self.split(),
                               refund_maturity_days=20, max_looks=2,
                               observation_end="2026-02-25T00:00:00Z")
        self.assertEqual(design["refund_maturity_days"], 20)
        review = service.run_review(design["id"], cutoff="2026-02-10T00:00:00Z",
                                    at="2026-02-10T00:00:00Z",
                                    permutation_draws=200, bootstrap_draws=60)
        by_unit = {row["unit"]: row for row in review["units"]}
        self.assertEqual(by_unit["R1"]["orders_refund_mature"], 0)
        self.assertEqual(by_unit["R1"]["raw_net"],
                         (500 - 100) * by_unit["R1"]["orders_mature"])
        # 到达退款成熟期后，退款被计入
        late = service.run_review(design["id"], cutoff="2026-03-10T00:00:00Z",
                                  at="2026-03-10T00:00:00Z",
                                  permutation_draws=200, bootstrap_draws=60)
        late_units = {row["unit"]: row for row in late["units"]}
        self.assertGreater(late_units["R1"]["orders_refund_mature"], 0)
        self.assertLess(late_units["R1"]["raw_net"], by_unit["R1"]["raw_net"])

    def test_refund_maturity_shorter_than_cancellation_is_refused(self):
        service = self.service()
        with self.assertRaises(ServiceError):
            self.register(service, self.split(), maturity_days=10,
                          refund_maturity_days=5)

    def test_null_fact_is_not_treated_as_zero(self):
        data = load_source(self.inputs, verify_manifest=False)
        order_id = sorted(data.orders)[0]
        row = dict(data.orders[order_id][0])
        for key in list(row):
            if key.startswith("_"):
                row.pop(key)
        row = {
            "id": "MISSING-1", "person": "missing-person", "region": "R1",
            "ordered": "2026-01-15T10:00:00Z", "received": "2026-01-16T00:00:00Z",
            "revenue": None, "cost": "100", "refund": "0",
        }
        loaded = load_source(
            self.inputs, extra_records={"orders": [row]}, verify_manifest=False,
        )
        selected = loaded.order_version_at("MISSING-1", utc(2026, 3, 1))
        self.assertIsNone(selected["_net"])
        self.assertTrue(any(issue.code == "null_fact" for issue in loaded.issues))

    def test_manifest_mismatch_is_reported(self):
        data = load_source(self.inputs, verify_manifest=True)
        # 合成目录没有 manifest，应给出提示而不是静默通过
        self.assertTrue(any(issue.code == "manifest_missing" for issue in data.issues))


class DescriptiveTests(BaseWorkspaceTest):
    def test_describe_is_marked_non_causal_and_keeps_dimensions(self):
        service = self.service()
        result = service.describe("2026-02-10T00:00:00Z")
        self.assertEqual(result["kind"], "descriptive")
        self.assertEqual(result["causal_claim"], "none")
        self.assertTrue(result["not_incremental"])
        creative = result["by_creative"][0]
        for key in ("campaign", "creative", "revision", "channel", "cohort",
                    "paid_touches", "persons_with_orders", "net_contribution"):
            self.assertIn(key, creative)

    def test_channel_summary_does_not_double_count_or_split_revenue(self):
        service = self.service()
        result = service.describe("2026-02-10T00:00:00Z")
        # 每个人只按“首个触达渠道”归入一次，因此各渠道人群数之和等于总触达人数。
        self.assertEqual(sum(row["persons_reached"] for row in result["by_channel"]),
                         result["persons_reached"])
        self.assertEqual(sum(row["persons_first_touch"] for row in result["by_channel"]),
                         result["persons_reached"])
        for row in result["by_channel"]:
            self.assertIn("net_contribution_first_touch", row)
            # 不再提供“按渠道分摊”的字段，避免被误读成渠道增量
            self.assertNotIn("net_contribution", row)

    def test_describe_respects_cutoff_and_received(self):
        service = self.service()
        early = service.describe("2026-01-02T00:00:00Z")
        later = service.describe("2026-02-10T00:00:00Z")
        self.assertLessEqual(early["persons_with_orders"], later["persons_with_orders"])


class DesignFreezeTests(BaseWorkspaceTest):
    def test_allocation_is_deterministic_and_frozen(self):
        service = self.service()
        first = service.create_design(
            campaign="ad", unit_type="region",
            planned_start="2026-01-01T00:00:00Z", planned_end="2026-02-01T00:00:00Z",
            observation_end="2026-02-10T00:00:00Z", seed=42,
            allocation_basis="测试复现", maturity_days=7, max_looks=1,
            at="2025-12-31T00:00:00Z",
        )
        second = self.service("ws2").create_design(
            campaign="ad", unit_type="region",
            planned_start="2026-01-01T00:00:00Z", planned_end="2026-02-01T00:00:00Z",
            observation_end="2026-02-10T00:00:00Z", seed=42,
            allocation_basis="测试复现", maturity_days=7, max_looks=1,
            at="2025-12-31T00:00:00Z",
        )
        self.assertEqual(first["allocation"]["assignment"], second["allocation"]["assignment"])
        self.assertEqual(first["allocation"]["fingerprint"],
                         second["allocation"]["fingerprint"])

    def test_assignment_must_cover_every_eligible_unit(self):
        service = self.service()
        incomplete = self.split()
        incomplete.pop("R12")
        with self.assertRaises(ServiceError):
            self.register(service, incomplete)

    def test_observation_end_must_cover_maturity(self):
        service = self.service()
        with self.assertRaises(ServiceError):
            self.register(
                service, self.split(),
                planned_end="2026-02-01T00:00:00Z",
                observation_end="2026-02-03T00:00:00Z",
                maturity_days=7,
            )

    def test_late_registration_is_refused_unless_acknowledged(self):
        service = self.service(now=utc(2026, 1, 15))
        with self.assertRaises(ServiceError):
            self.register(service, self.split(), at="2026-01-15T00:00:00Z")
        design = self.register(service, self.split(), at="2026-01-15T00:00:00Z",
                               late_registration=True)
        self.assertFalse(design["confirmatory"])
        self.assertTrue(design["registered_after_exposure"])
        review = service.run_review(
            design["id"], cutoff="2026-02-10T00:00:00Z", at="2026-02-10T00:00:00Z",
            permutation_draws=200, bootstrap_draws=60,
        )
        self.assertFalse(review["auto_win_eligible"])
        self.assertFalse(review["confirmatory"])

    def test_person_design_cannot_be_backfilled_from_touches(self):
        service = self.service()
        with self.assertRaises(ServiceError):
            self.register(service, self.split(), unit_type="person", normalization="total")


class AnalysisGuardTests(BaseWorkspaceTest):
    def run_on(self, service: Service, assignment=None, **design_overrides):
        design = self.register(service, assignment or self.split(), **design_overrides)
        return design, service.run_review(
            design["id"], cutoff="2026-02-10T00:00:00Z", at="2026-02-10T00:00:00Z",
            permutation_draws=400, bootstrap_draws=120,
        )

    def test_large_effect_can_be_eligible_but_never_auto_decided(self):
        _, review = self.run_on(self.service())
        self.assertEqual(review["blocking_guards"], [])
        self.assertTrue(review["primary"]["significant"])
        self.assertTrue(review["auto_win_eligible"])
        # 应用只给建议，不代负责人登记决策
        self.assertEqual(review["recommended_action"], "end")
        service = Service.open(self.work / "ws")
        self.assertEqual(service.list_decisions(), [])

    def test_immature_orders_block_win_even_when_effect_is_large(self):
        self.inputs = big_fixture(self.tmp / "inputs-late", order_start_day=26)
        service = self.service()
        design = self.register(service, self.split())
        review = service.run_review(design["id"], cutoff="2026-02-03T00:00:00Z",
                                    at="2026-02-03T00:00:00Z",
                                    permutation_draws=200, bootstrap_draws=80)
        self.assertLess(review["guards"]["maturity"]["detail"]["mature_share"], 1.0)
        self.assertFalse(review["auto_win_eligible"])
        self.assertIn("maturity", review["blocking_guards"])

    def test_control_paid_touches_are_a_coverage_gap(self):
        self.inputs = big_fixture(self.tmp / "inputs2", control_paid=True)
        _, review = self.run_on(self.service())
        self.assertIn("coverage", review["blocking_guards"])
        self.assertFalse(review["auto_win_eligible"])

    def test_missing_treatment_delivery_is_a_coverage_gap(self):
        self.inputs = big_fixture(self.tmp / "inputs3", treatment_paid=False)
        _, review = self.run_on(self.service())
        self.assertIn("coverage", review["blocking_guards"])

    def test_severe_imbalance_blocks_win(self):
        # 让处理组全部是基线 1000，对照组全部是 100 会过不了设计校验，
        # 因此这里只把两个对照地区改成极端基线，检查 SMD 守卫生效。
        baselines = {f"R{i}": 1000 for i in range(1, 13)}
        baselines["R7"] = 1
        baselines["R9"] = 1
        self.inputs = big_fixture(self.tmp / "inputs4", baselines=baselines)
        _, review = self.run_on(self.service())
        self.assertIn("balance", review["blocking_guards"])

    def test_unknown_net_contribution_is_never_zero_filled(self):
        data = load_source(self.inputs, verify_manifest=False)
        order_id = sorted(data.orders)[0]
        row = {
            "id": "MISSING-2", "person": "missing-person-2", "region": "R1",
            "ordered": "2026-01-15T10:00:00Z", "received": "2026-01-16T00:00:00Z",
            "revenue": "500", "cost": None, "refund": "0",
        }
        service = self.service()
        service.import_rows("orders", [row], at="2026-02-09T00:00:00Z",
                            note="制造缺失净贡献")
        design = self.register(service, self.split())
        review = service.run_review(design["id"], cutoff="2026-02-10T00:00:00Z",
                                    at="2026-02-10T00:00:00Z",
                                    permutation_draws=200, bootstrap_draws=80)
        self.assertGreaterEqual(review["guards"]["missing_values"]["detail"]["unknown_net_orders"], 1)
        self.assertFalse(review["auto_win_eligible"])
        self.assertIn("missing_values", review["blocking_guards"])

    def test_contaminated_region_is_reported_without_deleting_it(self):
        self.inputs = big_fixture(self.tmp / "inputs5", include_other_paid=True,
                                  other_paid_region="R8")
        _, review = self.run_on(self.service())
        self.assertIn("R8", review["unit_context"]["contaminated_units"])
        self.assertIn("R8", review["assignment"])  # 原分组保留
        self.assertEqual(review["assignment"]["R8"], "control")

    def test_exact_and_monte_carlo_reproduce(self):
        _, first = self.run_on(self.service("ws-repro-a"))
        _, second = self.run_on(self.service("ws-repro-b"))
        self.assertEqual(first["primary"]["estimate"], second["primary"]["estimate"])
        self.assertEqual(first["primary"]["p_two_sided"], second["primary"]["p_two_sided"])
        self.assertEqual(first["primary"]["ci_low"], second["primary"]["ci_low"])

    def test_alpha_spending_is_stricter_at_early_looks(self):
        schedule = stats.alpha_schedule(0.05, 3)
        self.assertLess(schedule[0]["alpha"], schedule[1]["alpha"])
        self.assertLess(schedule[1]["alpha"], schedule[2]["alpha"])

    def test_observational_contrast_is_not_a_win_eligible_review(self):
        # 没有登记设计、只用描述性数据，不可能产生 auto_win_eligible。
        service = self.service()
        described = service.describe("2026-02-10T00:00:00Z", campaign="ad")
        self.assertEqual(described["causal_claim"], "none")
        self.assertNotIn("auto_win_eligible", described)


class ObservationPlanTests(BaseWorkspaceTest):
    def test_look_beyond_registered_plan_is_refused(self):
        service = self.service()
        design = self.register(service, self.split(), max_looks=1)
        service.run_review(design["id"], cutoff="2026-02-10T00:00:00Z",
                           at="2026-02-10T00:00:00Z",
                           permutation_draws=100, bootstrap_draws=40)
        with self.assertRaises(ServiceError):
            service.run_review(design["id"], cutoff="2026-02-10T00:00:00Z",
                               at="2026-02-10T00:00:00Z",
                               permutation_draws=100, bootstrap_draws=40)

    def test_cutoff_cannot_move_backwards(self):
        service = self.service()
        design = self.register(service, self.split(), max_looks=3)
        service.run_review(design["id"], cutoff="2026-02-05T00:00:00Z",
                           at="2026-02-05T00:00:00Z",
                           permutation_draws=100, bootstrap_draws=40)
        with self.assertRaises(ServiceError):
            service.run_review(design["id"], cutoff="2026-02-02T00:00:00Z",
                               at="2026-02-05T00:00:00Z",
                               permutation_draws=100, bootstrap_draws=40)

    def test_revision_after_seeing_results_is_labelled_new_decision_version(self):
        service = self.service()
        design = self.register(service, self.split(), max_looks=3)
        service.run_review(design["id"], cutoff="2026-02-05T00:00:00Z",
                           at="2026-02-05T00:00:00Z",
                           permutation_draws=100, bootstrap_draws=40)
        revised = service.revise_design(
            design["id"], at="2026-02-06T00:00:00Z",
            reason="观察到早期正向信号后调整观察计划",
            changes={"observation_end": "2026-02-20T00:00:00Z", "max_looks": 2},
        )
        self.assertFalse(revised["confirmatory"])
        self.assertTrue(revised["prior_outcome_views"])
        self.assertEqual(revised["version"], 2)
        self.assertEqual(revised["supersedes"], design["id"])
        old = service.ws.state["designs"][design["id"]]
        self.assertEqual(old["status"], "superseded")

    def test_revision_cannot_switch_primary_metric_implicitly(self):
        service = self.service()
        design = self.register(service, self.split(), max_looks=2)
        with self.assertRaises(ServiceError):
            service.revise_design(
                design["id"], at="2025-12-31T12:00:00Z",
                reason="想换一个更容易显著的指标",
                changes={"primary_metric": "revenue"},
            )

    def test_revision_cannot_rerandomize_after_results_seen(self):
        service = self.service()
        design = self.register(service, self.split(), max_looks=2)
        service.run_review(design["id"], cutoff="2026-02-05T00:00:00Z",
                           at="2026-02-05T00:00:00Z",
                           permutation_draws=100, bootstrap_draws=40)
        with self.assertRaises(ServiceError):
            service.revise_design(
                design["id"], at="2026-02-06T00:00:00Z",
                reason="想换一个更有利的分组",
                changes={"observation_end": "2026-02-20T00:00:00Z"},
                rerandomize=True, seed=9,
            )

    def test_decision_requires_rationale_and_respects_plan(self):
        service = self.service()
        design = self.register(service, self.split(), max_looks=1)
        review = service.run_review(design["id"], cutoff="2026-02-10T00:00:00Z",
                                    at="2026-02-10T00:00:00Z",
                                    permutation_draws=200, bootstrap_draws=60)
        with self.assertRaises(ServiceError):
            service.decide(review["review_id"], action="end", rationale="短")
        with self.assertRaises(ServiceError):
            service.decide(review["review_id"], action="continue",
                           rationale="想继续再看一次，尽管已经到最大查看次数")
        decision = service.decide(review["review_id"], action="end",
                                  rationale="达到登记观察截止点且守卫全部通过，结束投放")
        self.assertEqual(decision["action"], "end")
        with self.assertRaises(ServiceError):
            service.decide(review["review_id"], action="pause", rationale="重复登记另一个动作")

    def test_deviation_from_registered_plan_requires_explicit_acknowledgement(self):
        service = self.service()
        design = self.register(service, self.split(), max_looks=2)
        review = service.run_review(design["id"], cutoff="2026-02-05T00:00:00Z",
                                    at="2026-02-05T00:00:00Z",
                                    permutation_draws=200, bootstrap_draws=60)
        with self.assertRaises(ServiceError):
            service.decide(review["review_id"], action="end",
                           rationale="未到观察截止点但想提前结束")
        decision = service.decide(review["review_id"], action="end",
                                  rationale="未到观察截止点但想提前结束",
                                  acknowledge_deviation=True)
        self.assertTrue(decision["deviation_acknowledged"])
        self.assertTrue(decision["deviations"])

    def test_continue_is_refused_while_guards_block(self):
        service = self.service()
        design = self.register(service, self.split(), max_looks=3)
        review = service.run_review(design["id"], cutoff="2026-02-03T00:00:00Z",
                                    at="2026-02-03T00:00:00Z",
                                    permutation_draws=200, bootstrap_draws=60)
        with self.assertRaises(ServiceError):
            service.decide(review["review_id"], action="continue",
                           rationale="数据未成熟但仍按原计划继续观察")

    def test_end_is_refused_when_guards_block(self):
        # 订单在 1 月 26 日之后才下单，2 月 3 日时尚未成熟，成熟度守卫阻断。
        self.inputs = big_fixture(self.tmp / "inputs-late-end", order_start_day=26)
        service = self.service()
        design = self.register(service, self.split(), max_looks=1)
        review = service.run_review(design["id"], cutoff="2026-02-03T00:00:00Z",
                                    at="2026-02-03T00:00:00Z",
                                    permutation_draws=200, bootstrap_draws=60)
        self.assertFalse(review["auto_win_eligible"])
        self.assertIn("maturity", review["blocking_guards"])
        with self.assertRaises(ServiceError):
            service.decide(review["review_id"], action="end",
                           rationale="数据还没成熟但想直接宣布有效结束",
                           acknowledge_deviation=True)


class BudgetTests(BaseWorkspaceTest):
    def setUp(self) -> None:
        super().setUp()
        self.svc = self.service()
        design = self.register(self.svc, self.split())
        self.review = self.svc.run_review(
            design["id"], cutoff="2026-02-10T00:00:00Z", at="2026-02-10T00:00:00Z",
            permutation_draws=200, bootstrap_draws=60,
        )

    def propose(self, **kwargs):
        payload = dict(
            campaign="ad", regions=["R9"], requested_fen=30000,
            start="2026-03-01T00:00:00Z", end="2026-03-15T00:00:00Z",
            ttl_hours=48, rationale="保持与登记设计一致的地区继续投放",
            at="2026-02-11T00:00:00Z",
        )
        payload.update(kwargs)
        return self.svc.create_proposal(**payload)

    def test_signed_contracts_are_deducted_from_remaining_budget(self):
        ledger = self.svc.ledger(at="2026-02-11T00:00:00Z")
        # 合成资料：不可取消 signed 50000 + executed 20000；可取消合同不占用
        self.assertEqual(ledger["contract_committed_fen"], 70000)
        self.assertEqual(ledger["cancellable_signed_fen"], 99999)
        self.assertEqual(ledger["remaining_fen"], 530000)

    def test_proposal_does_not_occupy_until_confirmed(self):
        proposal = self.propose()
        ledger = self.svc.ledger(at="2026-02-11T00:00:00Z")
        self.assertEqual(ledger["confirmed_plan_reserved_fen"], 0)
        confirmed = self.svc.confirm(proposal["id"], rationale="确认使用剩余预算",
                                     at="2026-02-11T01:00:00Z")
        ledger = self.svc.ledger(at="2026-02-11T01:00:00Z")
        self.assertEqual(ledger["confirmed_plan_reserved_fen"], 30000)
        self.assertEqual(confirmed["status"], "confirmed")

    def test_second_plan_cannot_reuse_same_region_quota(self):
        first = self.propose()
        second = self.propose(requested_fen=10000)
        self.svc.confirm(first["id"], rationale="确认第一个候选方案", at="2026-02-11T01:00:00Z")
        with self.assertRaises(ServiceError):
            self.svc.confirm(second["id"], rationale="确认第二个同地区候选方案",
                             at="2026-02-11T02:00:00Z")

    def test_same_remaining_budget_requires_recompare(self):
        small = Service(Workspace.create(
            self.work, "small-budget", inputs_dir=self.inputs,
            now=utc(2025, 12, 31), budget_total_fen=100000,
        ))
        design = self.register(small, self.split())
        small.run_review(design["id"], cutoff="2026-02-10T00:00:00Z",
                         at="2026-02-10T00:00:00Z",
                         permutation_draws=200, bootstrap_draws=60)
        first = small.create_proposal(
            campaign="ad", regions=["R9"], requested_fen=20000,
            start="2026-03-01T00:00:00Z", end="2026-03-15T00:00:00Z",
            ttl_hours=48, rationale="争用同一预算的第一个方案",
            at="2026-02-11T00:00:00Z",
        )
        second = small.create_proposal(
            campaign="ad", regions=["R10"], requested_fen=20000,
            start="2026-03-01T00:00:00Z", end="2026-03-15T00:00:00Z",
            ttl_hours=48, rationale="争用同一预算的第二个方案",
            at="2026-02-11T00:00:00Z",
        )
        small.confirm(first["id"], rationale="确认第一个大额候选方案",
                      at="2026-02-11T01:00:00Z")
        with self.assertRaises(ServiceError):
            small.confirm(second["id"], rationale="确认第二个大额候选方案",
                          at="2026-02-11T02:00:00Z")

    def test_stale_snapshot_is_rejected(self):
        proposal = self.propose()
        snapshot = self.svc.ledger(at="2026-02-11T00:00:00Z")["remaining_fen"]
        other = self.propose(requested_fen=1000, regions=["R10"])
        self.svc.confirm(other["id"], rationale="先确认另一个候选方案",
                         at="2026-02-11T01:00:00Z")
        with self.assertRaises(ServiceError):
            self.svc.confirm(proposal["id"], rationale="使用旧快照进行确认",
                             at="2026-02-11T02:00:00Z",
                             expected_remaining_fen=snapshot)

    def test_expired_confirmation_is_flagged(self):
        proposal = self.propose()
        self.svc.confirm(proposal["id"], rationale="确认候选方案投放", at="2026-02-11T01:00:00Z")
        flagged = self.svc.expire(at="2026-02-20T00:00:00Z")
        self.assertEqual([row["id"] for row in flagged], [proposal["id"]])

    def test_expired_confirmation_releases_region_but_requires_recompare(self):
        first = self.propose(regions=["R9"], requested_fen=5000)
        self.svc.confirm(first["id"], rationale="确认短期使用方案", at="2026-02-11T01:00:00Z")
        ledger = self.svc.ledger(at="2026-02-11T01:00:00Z")
        self.assertEqual(ledger["confirmed_plan_reserved_fen"], 5000)
        # 过期后不再占用预算与地区名额
        self.assertEqual(self.svc.ledger(at="2026-02-20T00:00:00Z")["confirmed_plan_reserved_fen"], 0)
        second = self.propose(regions=["R9"], requested_fen=4000,
                              at="2026-02-20T00:30:00Z")
        confirmed = self.svc.confirm(second["id"], rationale="对比后复用该地区名额",
                                     at="2026-02-20T01:00:00Z")
        self.assertEqual(confirmed["status"], "confirmed")

    def test_mutually_exclusive_region_cannot_be_double_booked(self):
        with self.assertRaises(ServiceError):
            self.propose(campaign="other", regions=["R1"], requested_fen=1000,
                         start="2026-01-05T00:00:00Z", end="2026-01-10T00:00:00Z")

    def test_budget_cap_and_negative_amounts_are_refused(self):
        with self.assertRaises(ServiceError):
            self.propose(requested_fen=10_000_000, regions=["R9"])
        with self.assertRaises(ServiceError):
            self.propose(requested_fen=0)

    def test_execution_backfill_and_cancel(self):
        proposal = self.propose()
        self.svc.confirm(proposal["id"], rationale="确认候选方案投放", at="2026-02-11T01:00:00Z")
        signed = self.svc.record_execution(
            proposal["id"], status="signed", executed_fen=30000,
            note="已下单", at="2026-02-12T00:00:00Z",
        )
        self.assertEqual(signed["status"], "signed")
        ledger = self.svc.ledger(at="2026-02-12T00:00:00Z")
        self.assertEqual(ledger["signed_execution_fen"], 30000)
        cancelled = self.svc.record_execution(
            proposal["id"], status="cancelled", executed_fen=None,
            note="取消", at="2026-02-13T00:00:00Z",
        )
        ledger = self.svc.ledger(at="2026-02-13T00:00:00Z")
        self.assertEqual(ledger["signed_execution_fen"], 0)
        self.assertEqual(ledger["remaining_fen"], 530000)

    def test_noise_plan_cannot_hold_region_without_evidence(self):
        # 描述性结果不是证据，但方案仍可比；比较结果要明确列出没有确认证据。
        proposal = self.propose(campaign="other", regions=["R11"], requested_fen=1000,
                                start="2026-03-01T00:00:00Z", end="2026-03-10T00:00:00Z")
        rows = self.svc.compare([proposal["id"]], at="2026-02-11T02:00:00Z")
        self.assertEqual(rows[0]["evidence_status"], "no_confirmed_evidence")
        self.assertEqual(rows[0]["evidence_strength"], 0)
        self.assertEqual(rows[0]["recommendation"], "hold_budget")

    def test_limited_evidence_only_supports_pilot_not_expansion(self):
        # 结果显著为正但尚未到登记观察截止点：证据强度为 2，只能小额试探。
        service = Service(Workspace.create(
            self.work, "pilot", inputs_dir=self.inputs, now=utc(2025, 12, 31),
        ))
        design = self.register(service, self.split(),
                               observation_end="2026-02-20T00:00:00Z")
        service.run_review(design["id"], cutoff="2026-02-10T00:00:00Z",
                           at="2026-02-10T00:00:00Z",
                           permutation_draws=200, bootstrap_draws=60)
        proposal = service.create_proposal(
            campaign="ad", regions=["R9"], requested_fen=5000,
            start="2026-03-01T00:00:00Z", end="2026-03-15T00:00:00Z",
            ttl_hours=48, rationale="证据尚早，只做小额试探", at="2026-02-11T00:00:00Z",
        )
        rows = service.compare([proposal["id"]], at="2026-02-11T02:00:00Z")
        self.assertEqual(rows[0]["evidence_strength"], 2)
        self.assertEqual(rows[0]["recommendation"], "pilot")


class RefundReopenTests(BaseWorkspaceTest):
    def test_late_refund_reopens_unexecuted_plan_and_keeps_executed_contract(self):
        service = self.service()
        design = self.register(service, self.split())
        service.run_review(design["id"], cutoff="2026-02-10T00:00:00Z",
                           at="2026-02-10T00:00:00Z",
                           permutation_draws=200, bootstrap_draws=60)
        first = service.create_proposal(
            campaign="ad", regions=["R9"], requested_fen=20000,
            start="2026-03-01T00:00:00Z", end="2026-03-10T00:00:00Z",
            ttl_hours=48, rationale="已履约投放不动", at="2026-02-11T00:00:00Z",
        )
        service.confirm(first["id"], rationale="确认已履约投放方案", at="2026-02-11T01:00:00Z")
        service.record_execution(first["id"], status="executed", executed_fen=20000,
                                 note="已履约", at="2026-02-11T02:00:00Z")
        second = service.create_proposal(
            campaign="ad", regions=["R10"], requested_fen=20000,
            start="2026-03-01T00:00:00Z", end="2026-03-10T00:00:00Z",
            ttl_hours=48, rationale="新增投放尚未执行", at="2026-02-11T00:00:00Z",
        )
        service.confirm(second["id"], rationale="确认新增投放方案", at="2026-02-11T01:00:00Z")
        late_rows = [
            {"id": "LATE-1", "person": "late-person", "region": "R9",
             "ordered": "2026-02-05T10:00:00Z", "received": "2026-02-25T00:00:00Z",
             "revenue": "500", "cost": "100", "refund": "400"},
            {"id": "LATE-2", "person": "late-person", "region": "R10",
             "ordered": "2026-02-05T10:00:00Z", "received": "2026-02-25T00:00:00Z",
             "revenue": "500", "cost": "100", "refund": "400"},
        ]
        service.import_rows("orders", late_rows, at="2026-02-25T01:00:00Z",
                            note="迟到退款")
        flagged = service.refund_check(at="2026-02-26T00:00:00Z")
        actions = {row["execution_id"]: row["action"] for row in flagged}
        self.assertEqual(len(actions), 2)
        self.assertIn("revise_conclusion_not_contract", actions.values())
        self.assertIn("reopen_review", actions.values())
        executed = [row for row in flagged
                    if row["action"] == "revise_conclusion_not_contract"][0]
        self.assertEqual(executed["execution_status"], "executed")
        # 已履约合同预算仍在台账中
        ledger = service.ledger(at="2026-02-26T00:00:00Z")
        self.assertGreaterEqual(ledger["executed_fen"], 20000)


class AuditAndIsolationTests(BaseWorkspaceTest):
    def test_audit_chain_detects_tampering(self):
        service = self.service()
        design = self.register(service, self.split())
        service.run_review(design["id"], cutoff="2026-02-10T00:00:00Z",
                           at="2026-02-10T00:00:00Z",
                           permutation_draws=100, bootstrap_draws=40)
        audit = service.audit()
        self.assertEqual(audit["problems"], [])
        log = service.ws.root / "audit.jsonl"
        lines = log.read_text(encoding="utf-8").splitlines()
        tampered = json.loads(lines[1])
        tampered["payload"]["campaign"] = "tampered"
        lines[1] = json.dumps(tampered, ensure_ascii=False)
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertTrue(service.audit()["problems"])

    def test_workspaces_do_not_share_budget_or_designs(self):
        left = self.service("left")
        right = self.service("right")
        left_design = self.register(left, self.split())
        self.assertEqual(right.list_designs(), [])
        left.create_proposal(campaign="ad", regions=["R9"], requested_fen=1000,
                             start="2026-03-01T00:00:00Z", end="2026-03-05T00:00:00Z",
                             ttl_hours=10, rationale="左侧工作目录的预算",
                             at="2026-02-11T00:00:00Z")
        left.confirm("P001", rationale="确认左侧工作目录方案", at="2026-02-11T01:00:00Z")
        self.assertEqual(right.ledger(at="2026-02-11T01:00:00Z")["confirmed_plan_reserved_fen"], 0)
        self.assertEqual(left.ws.state["designs"][left_design["id"]]["campaign"], "ad")

    def test_workspace_refuses_to_overwrite_without_flag(self):
        self.service("dup")
        with self.assertRaises(WorkspaceError):
            self.service("dup")

    def test_audit_records_cutoff_and_reasons(self):
        service = self.service()
        design = self.register(service, self.split())
        review = service.run_review(design["id"], cutoff="2026-02-10T00:00:00Z",
                                    at="2026-02-10T00:00:00Z",
                                    permutation_draws=100, bootstrap_draws=40)
        service.decide(review["review_id"], action="end",
                       rationale="达到观察截止点，守卫通过，结束投放")
        events = service.audit()["events"]
        review_event = [event for event in events if event["event"] == "review.completed"][0]
        self.assertEqual(review_event["payload"]["cutoff"], "2026-02-10T00:00:00Z")
        decision_event = [event for event in events if event["event"] == "decision.recorded"][0]
        self.assertIn("观察截止点", decision_event["payload"]["rationale"])


if __name__ == "__main__":
    unittest.main()
