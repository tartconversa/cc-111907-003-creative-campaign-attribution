"""可解释的组随机化统计。

方法
----
* 单位：地区或合成用户群。处理组与对照组的差异按单位计算。
* 分层：默认按地区的相似层分层；每层内先算处理组均值减对照组均值，再按
  登记口径加权（层等权或按常规客群规模加权）。
* 不确定性：层内自助法百分位区间；组随机化置换检验给出双侧与单侧 p 值。
  小样本时优先精确枚举所有分配，超过上限才用固定种子的蒙特卡洛。
* 多次查看：按登记的最大查看次数使用 O'Brien-Fleming 型 alpha 消耗，
  早期查看的判定阈值更严；所有随机种子都写入结果，便于复现。

局限
----
组随机化在单位很少时检验力有限；自助区间在每层只有 1-2 个单位时只是
粗略近似。置换检验只能覆盖“分配随机”这一不确定性，不能修复污染、
覆盖缺口或未成熟订单。应用因此在守卫条件不通过时禁止自动判胜。
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations, product
import math
import random
from typing import Iterable, Sequence

from .errors import DomainError


class StatsError(DomainError):
    """无法按登记口径计算（例如缺少处理组或对照组）。"""

SPEC_LIBRARY: dict[str, dict[str, object]] = {
    "stratified_baseline": {
        "label": "分层 + 常规客群加权",
        "stratified": True,
        "weighting": "baseline",
    },
    "stratified_equal": {
        "label": "分层 + 层等权",
        "stratified": True,
        "weighting": "equal",
    },
    "unstratified": {
        "label": "不分层 + 单位等权",
        "stratified": False,
        "weighting": "equal",
    },
}

SENSITIVITY_SPEC = "exclude_contaminated"


def normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def normal_ppf(probability: float) -> float:
    if not 0.0 < probability < 1.0:
        raise StatsError("概率必须在 (0,1) 内")
    low, high = -10.0, 10.0
    for _ in range(200):
        mid = (low + high) / 2.0
        if normal_cdf(mid) < probability:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


def obf_alpha(alpha: float, look: int, max_looks: int) -> float:
    """O'Brien-Fleming 型 alpha 消耗（Lan-DeMets 形式）。"""
    if look < 1:
        raise StatsError("查看序号从 1 开始")
    if max_looks < 1:
        raise StatsError("最大查看次数至少为 1")
    if look > max_looks:
        return alpha
    information = look / max_looks
    critical = normal_ppf(1.0 - alpha / 2.0) / math.sqrt(information)
    return 2.0 * (1.0 - normal_cdf(critical))


def alpha_schedule(alpha: float, max_looks: int) -> list[dict[str, float]]:
    schedule = []
    for look in range(1, max_looks + 1):
        spent = obf_alpha(alpha, look, max_looks)
        schedule.append({
            "look": look,
            "alpha": spent,
            "z_critical": normal_ppf(1.0 - spent / 2.0) if spent < 1 else 0.0,
        })
    return schedule


@dataclass(frozen=True)
class Unit:
    unit_id: str
    arm: str  # treatment / control
    stratum: str
    baseline: float
    value: float | None
    contaminated: bool = False

    @property
    def usable(self) -> bool:
        return self.value is not None


def normalized_value(value: float | None, baseline: float, normalization: str) -> float | None:
    if value is None:
        return None
    if normalization == "total":
        return value
    if normalization == "per_1000_baseline":
        if baseline <= 0:
            return None
        return value / baseline * 1000.0
    raise StatsError(f"未知归一化口径: {normalization}")


def _stratum_groups(units: Sequence[Unit], stratified: bool) -> dict[str, list[Unit]]:
    groups: dict[str, list[Unit]] = {}
    for unit in units:
        key = unit.stratum if stratified else "all"
        groups.setdefault(key, []).append(unit)
    return groups


def _stratum_weight(group: list[Unit], weighting: str) -> float:
    if weighting == "equal":
        return 1.0
    if weighting == "baseline":
        return float(sum(unit.baseline for unit in group))
    raise StatsError(f"未知加权口径: {weighting}")


def _mean(values: Iterable[float]) -> float | None:
    values = list(values)
    if not values:
        return None
    return sum(values) / len(values)


def difference(units: Sequence[Unit], spec: dict[str, object]) -> float | None:
    """按口径计算处理组减对照组的差异。"""
    stratified = bool(spec["stratified"])
    weighting = str(spec["weighting"])
    groups = _stratum_groups(units, stratified)
    numerator = 0.0
    denominator = 0.0
    for key in sorted(groups):
        group = groups[key]
        treated = [unit.value for unit in group if unit.arm == "treatment" and unit.usable]
        control = [unit.value for unit in group if unit.arm == "control" and unit.usable]
        if not treated or not control:
            continue
        weight = _stratum_weight(group, weighting)
        numerator += weight * (_mean(treated) - _mean(control))
        denominator += weight
    if denominator == 0:
        return None
    return numerator / denominator


def _stat_from_assignment(
    units: Sequence[Unit],
    treated_ids: set[str],
    spec: dict[str, object],
) -> float | None:
    reassigned = [
        Unit(
            unit_id=unit.unit_id,
            arm="treatment" if unit.unit_id in treated_ids else "control",
            stratum=unit.stratum,
            baseline=unit.baseline,
            value=unit.value,
            contaminated=unit.contaminated,
        )
        for unit in units
    ]
    return difference(reassigned, spec)


def _exact_permutation(
    units: Sequence[Unit],
    observed: float,
    spec: dict[str, object],
) -> tuple[int, int, int]:
    """精确枚举分层内保持各组单位数的全部随机分配。

    返回 (总分配数, 双侧极端数, 单侧极端数)。
    """
    stratified = bool(spec["stratified"])
    groups = _stratum_groups(units, stratified)
    strata_units: list[list[Unit]] = []
    strata_treated: list[int] = []
    for key in sorted(groups):
        group = groups[key]
        strata_units.append(group)
        strata_treated.append(sum(1 for unit in group if unit.arm == "treatment"))

    total = 1
    for group, treated_count in zip(strata_units, strata_treated):
        total *= math.comb(len(group), treated_count)
    if total > 200_000:
        raise OverflowError("精确枚举规模过大")

    two_sided = 0
    one_sided = 0
    tolerance = 1e-12
    for choices in product(*[
        list(combinations(range(len(group)), count))
        for group, count in zip(strata_units, strata_treated)
    ]):
        treated_ids: set[str] = set()
        for group, chosen in zip(strata_units, choices):
            treated_ids.update(group[index].unit_id for index in chosen)
        stat = _stat_from_assignment(units, treated_ids, spec)
        if stat is None:
            continue
        if abs(stat) >= abs(observed) - tolerance:
            two_sided += 1
        if stat >= observed - tolerance:
            one_sided += 1
    return total, two_sided, one_sided


def _monte_carlo_permutation(
    units: Sequence[Unit],
    observed: float,
    spec: dict[str, object],
    draws: int,
    rng: random.Random,
) -> tuple[int, int, int]:
    stratified = bool(spec["stratified"])
    groups = _stratum_groups(units, stratified)
    strata_units = [groups[key] for key in sorted(groups)]
    strata_treated = [sum(1 for unit in group if unit.arm == "treatment") for group in strata_units]
    two_sided = 0
    one_sided = 0
    tolerance = 1e-12
    for _ in range(draws):
        treated_ids: set[str] = set()
        for group, count in zip(strata_units, strata_treated):
            pool = list(group)
            rng.shuffle(pool)
            treated_ids.update(unit.unit_id for unit in pool[:count])
        stat = _stat_from_assignment(units, treated_ids, spec)
        if stat is None:
            continue
        if abs(stat) >= abs(observed) - tolerance:
            two_sided += 1
        if stat >= observed - tolerance:
            one_sided += 1
    # 加一修正把已观测分配本身视为一次抽样。
    return draws + 1, two_sided + 1, one_sided + 1


def _percentile(sorted_values: Sequence[float], probability: float) -> float:
    if not sorted_values:
        raise StatsError("空样本无法计算分位数")
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = probability * (len(sorted_values) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return sorted_values[lower]
    fraction = position - lower
    return sorted_values[lower] * (1 - fraction) + sorted_values[upper] * fraction


def _bootstrap(
    units: Sequence[Unit],
    spec: dict[str, object],
    draws: int,
    confidence: float,
    rng: random.Random,
) -> tuple[float, float, list[float]]:
    stratified = bool(spec["stratified"])
    groups = _stratum_groups(units, stratified)
    samples: list[float] = []
    for _ in range(draws):
        resampled: list[Unit] = []
        for key in sorted(groups):
            group = groups[key]
            for arm in ("treatment", "control"):
                arm_units = [unit for unit in group if unit.arm == arm]
                if not arm_units:
                    continue
                drawn = [rng.choice(arm_units) for _ in arm_units]
                for index, unit in enumerate(drawn):
                    resampled.append(Unit(
                        unit_id=f"{unit.unit_id}#{index}",
                        arm=arm,
                        stratum=unit.stratum,
                        baseline=unit.baseline,
                        value=unit.value,
                        contaminated=unit.contaminated,
                    ))
        stat = difference(resampled, spec)
        if stat is not None:
            samples.append(stat)
    if not samples:
        return float("nan"), float("nan"), []
    samples.sort()
    tail = (1.0 - confidence) / 2.0
    return _percentile(samples, tail), _percentile(samples, 1.0 - tail), samples


def estimate(
    units: Sequence[Unit],
    *,
    spec_id: str,
    normalization: str,
    confidence: float,
    alpha: float,
    look: int,
    max_looks: int,
    seed: int,
    permutation_draws: int = 10_000,
    bootstrap_draws: int = 2_000,
) -> dict[str, object]:
    """计算一个预先选定口径的处理组减对照组结果。"""
    if spec_id not in SPEC_LIBRARY:
        raise StatsError(f"未知分析口径: {spec_id}")
    spec = SPEC_LIBRARY[spec_id]
    prepared = [
        Unit(
            unit_id=unit.unit_id,
            arm=unit.arm,
            stratum=unit.stratum,
            baseline=unit.baseline,
            value=normalized_value(unit.value, unit.baseline, normalization),
            contaminated=unit.contaminated,
        )
        for unit in units
    ]
    observed = difference(prepared, spec)
    if observed is None:
        raise StatsError("没有可用于该口径的处理组/对照组单位")

    rng_perm = random.Random(seed ^ 0x5EED)
    method = "exact"
    try:
        total, two_sided, one_sided = _exact_permutation(prepared, observed, spec)
    except OverflowError:
        method = "monte_carlo"
        total, two_sided, one_sided = _monte_carlo_permutation(
            prepared, observed, spec, permutation_draws, rng_perm,
        )

    rng_boot = random.Random(seed ^ 0xB007)
    ci_low, ci_high, _ = _bootstrap(prepared, spec, bootstrap_draws, confidence, rng_boot)

    p_two = two_sided / total
    p_one = one_sided / total
    alpha_look = obf_alpha(alpha, look, max_looks)
    z_critical = normal_ppf(1.0 - alpha_look / 2.0) if alpha_look < 1 else 0.0

    treated = [unit for unit in prepared if unit.arm == "treatment"]
    control = [unit for unit in prepared if unit.arm == "control"]
    strata_detail = []
    groups = _stratum_groups(prepared, bool(spec["stratified"]))
    for key in sorted(groups):
        group = groups[key]
        treated_values = [unit.value for unit in group if unit.arm == "treatment" and unit.usable]
        control_values = [unit.value for unit in group if unit.arm == "control" and unit.usable]
        strata_detail.append({
            "stratum": key,
            "treatment_units": sum(1 for unit in group if unit.arm == "treatment"),
            "control_units": sum(1 for unit in group if unit.arm == "control"),
            "treatment_mean": _mean(treated_values),
            "control_mean": _mean(control_values),
            "difference": (_mean(treated_values) - _mean(control_values))
            if treated_values and control_values else None,
            "weight": _stratum_weight(group, str(spec["weighting"])),
        })

    return {
        "spec_id": spec_id,
        "spec_label": spec["label"],
        "normalization": normalization,
        "estimate": observed,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "confidence": confidence,
        "p_two_sided": p_two,
        "p_one_sided": p_one,
        "alpha_at_look": alpha_look,
        "z_critical": z_critical,
        "significant": p_two < alpha_look,
        "direction": "treatment" if observed > 0 else ("control" if observed < 0 else "none"),
        "method": {
            "permutation": method,
            "assignments": total,
            "min_attainable_p": 1.0 / total,
            "permutation_draws": permutation_draws if method == "monte_carlo" else 0,
            "bootstrap_draws": bootstrap_draws,
            "seed": seed,
        },
        "units": {
            "treatment": len(treated),
            "control": len(control),
            "usable_treatment": sum(1 for unit in treated if unit.usable),
            "usable_control": sum(1 for unit in control if unit.usable),
        },
        "strata": strata_detail,
        "look": look,
        "max_looks": max_looks,
    }


def pooled_smd(treatment: Sequence[float], control: Sequence[float]) -> float | None:
    if len(treatment) < 2 or len(control) < 2:
        return None
    mean_t = sum(treatment) / len(treatment)
    mean_c = sum(control) / len(control)
    var_t = sum((value - mean_t) ** 2 for value in treatment) / (len(treatment) - 1)
    var_c = sum((value - mean_c) ** 2 for value in control) / (len(control) - 1)
    pooled = math.sqrt((var_t + var_c) / 2.0)
    if pooled == 0:
        return 0.0 if abs(mean_t - mean_c) < 1e-12 else None
    return (mean_t - mean_c) / pooled


def max_baseline_smd(units: Sequence[Unit]) -> float | None:
    strata: dict[str, list[Unit]] = {}
    for unit in units:
        strata.setdefault(unit.stratum, []).append(unit)
    smds = []
    for group in strata.values():
        treatment = [unit.baseline for unit in group if unit.arm == "treatment"]
        control = [unit.baseline for unit in group if unit.arm == "control"]
        smd = pooled_smd(treatment, control)
        if smd is not None:
            smds.append(abs(smd))
    return max(smds) if smds else None
