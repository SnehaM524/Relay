"""Experiment statistics: two-proportion z-test, CI on relative lift, power,
sample size, and a simple O'Brien-Fleming-style sequential boundary so the
agent can stop early without inflating false positives.
"""

from __future__ import annotations

import math

from scipy.stats import norm

from .models import ArmResult, StatsResult


def required_sample_per_arm(baseline: float, mde_relative: float, alpha: float = 0.05, power: float = 0.8) -> int:
    """Per-arm sample size to detect a relative lift `mde_relative` on `baseline` (two-sided)."""
    p1 = baseline
    p2 = baseline * (1 + mde_relative)
    if p1 <= 0 or p2 >= 1:
        return 10**9
    z_a = norm.ppf(1 - alpha / 2)
    z_b = norm.ppf(power)
    pbar = (p1 + p2) / 2
    num = (z_a * math.sqrt(2 * pbar * (1 - pbar)) + z_b * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2
    return int(math.ceil(num / (p2 - p1) ** 2))


def achieved_power(p1: float, p2: float, n_per_arm: int, alpha: float = 0.05) -> float:
    if n_per_arm <= 0 or p1 <= 0:
        return 0.0
    z_a = norm.ppf(1 - alpha / 2)
    pbar = (p1 + p2) / 2
    se0 = math.sqrt(2 * pbar * (1 - pbar) / n_per_arm)
    se1 = math.sqrt((p1 * (1 - p1) + p2 * (1 - p2)) / n_per_arm)
    if se1 == 0:
        return 1.0
    diff = abs(p2 - p1)
    return float(norm.cdf((diff - z_a * se0) / se1) + norm.cdf((-diff - z_a * se0) / se1))


def _obf_alpha(fraction_of_sample: float, alpha: float) -> float:
    """Spend alpha conservatively early (approximate O'Brien-Fleming boundary)."""
    f = max(min(fraction_of_sample, 1.0), 0.05)
    z = norm.ppf(1 - alpha / 2) / math.sqrt(f)
    return float(2 * (1 - norm.cdf(z)))


def evaluate(control: ArmResult, treatment: ArmResult, *, alpha: float = 0.05, power_target: float = 0.8,
             mde_relative: float = 0.05, days_running: int = 0, min_days: int = 7, max_days: int = 28,
             sequential: bool = True) -> StatsResult:
    p1, p2 = control.rate, treatment.rate
    n1, n2 = control.users, treatment.users
    if n1 == 0 or n2 == 0 or p1 == 0:
        return StatsResult(control=control, treatment=treatment, relative_lift=0, absolute_lift=0, p_value=1, z=0,
                           ci_low=0, ci_high=0, significant=False, power=0, required_per_arm=0, days_running=days_running,
                           decision="keep_running")
    pooled = (control.conversions + treatment.conversions) / (n1 + n2)
    se_pooled = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    z = (p2 - p1) / se_pooled if se_pooled else 0.0
    p_value = float(2 * (1 - norm.cdf(abs(z))))

    # 95% CI on relative lift via delta method on log(p2/p1)
    rel = (p2 - p1) / p1
    se_log = math.sqrt((1 - p1) / (n1 * p1) + (1 - p2) / (n2 * p2)) if p2 > 0 else float("inf")
    ci_low = math.exp(math.log(p2 / p1) - 1.96 * se_log) - 1 if p2 > 0 else -1.0
    ci_high = math.exp(math.log(p2 / p1) + 1.96 * se_log) - 1 if p2 > 0 else 0.0

    required = required_sample_per_arm(p1, mde_relative, alpha, power_target)
    frac = min(n1, n2) / required if required else 1.0
    final_look = days_running >= max_days or frac >= 1.0
    threshold = alpha if (final_look or not sequential) else _obf_alpha(frac, alpha)
    significant = p_value < threshold and days_running >= min_days
    pw = achieved_power(p1, p2, min(n1, n2), alpha)

    if significant:
        decision = "ship" if rel > 0 else "kill"
    elif days_running >= max_days or frac >= 1.5:
        decision = "inconclusive"
    else:
        decision = "keep_running"

    return StatsResult(
        control=control, treatment=treatment, relative_lift=round(rel, 4), absolute_lift=round(p2 - p1, 5),
        p_value=round(p_value, 5), z=round(z, 3), ci_low=round(ci_low, 4), ci_high=round(ci_high, 4),
        significant=significant, power=round(pw, 3), required_per_arm=required, days_running=days_running,
        decision=decision, sequential_boundary_hit=significant and frac < 1.0,
        alpha_threshold=round(threshold, 5), sample_fraction=round(frac, 3),
    )
