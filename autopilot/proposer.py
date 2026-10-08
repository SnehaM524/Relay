"""Experiment proposal and ranking.

Priority score = expected users gained per window × confidence × effort weight.
Expected funnel lift = relative lift on overall conversion if the target step
rate moves by `expected_lift` (every downstream step inherits the extra users).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from .funnel import step_type
from .llm import LLM
from .models import ExperimentProposal, Funnel, FunnelAnalysis, RankedProposal

log = logging.getLogger(__name__)

SYSTEM = """You are a senior growth engineer. You propose A/B experiments for SaaS signup funnels.
Be concrete and specific to the product context. Each proposal must target one funnel step, state a
falsifiable hypothesis, describe the exact UI change, and give a calibrated expected relative lift
(most real winners are 3-15%; only friction removals reach 20%+) with a confidence between 0.3 and 0.8.
Prefer experiments on the step losing the most users. Do not propose the same idea twice."""


def load_playbook(path: str | Path) -> list[dict[str, Any]]:
    with open(path) as f:
        return yaml.safe_load(f)["patterns"]


def funnel_lift_from_step_lift(funnel: Funnel, analysis: FunnelAnalysis, target_step: str, step_lift: float,
                               marginal_decay: float = 0.7) -> tuple[float, float, int]:
    """Translate a relative lift on `target_step`'s rate into (funnel_lift, users_gained, reach).

    Extra users created at the target step flow through downstream steps at `marginal_decay`
    × the baseline rate, because marginal users are lower intent than the baseline cohort.
    """
    entered = {d.from_step: d.entered for d in analysis.dropoffs}
    rates = {d.from_step: d.step_rate for d in analysis.dropoffs}
    if target_step not in rates:  # last step: treat as the final transition
        last = analysis.dropoffs[-1]
        target_step = last.from_step
    reach, rate = entered[target_step], rates[target_step]
    gained = reach * rate * step_lift
    passed = False
    for d in analysis.dropoffs:
        if passed:
            gained *= d.step_rate * marginal_decay
        if d.from_step == target_step:
            passed = True
    final = funnel.steps[-1].users
    return (gained / final if final else 0.0), gained, int(reach)


class Proposer:
    def __init__(self, llm: LLM, playbook: list[dict[str, Any]], marginal_decay: float = 0.7):
        self.llm = llm
        self.playbook = playbook
        self.marginal_decay = marginal_decay

    def propose(self, funnel: Funnel, analysis: FunnelAnalysis, n: int = 8, exclude_keys: set[str] | None = None) -> list[ExperimentProposal]:
        exclude = exclude_keys or set()
        proposals = self._from_llm(funnel, analysis, n, exclude) or self._from_playbook(funnel, analysis, n, exclude)
        return proposals[:n]

    def rank(self, funnel: Funnel, analysis: FunnelAnalysis, proposals: list[ExperimentProposal]) -> list[RankedProposal]:
        ranked: list[RankedProposal] = []
        for p in proposals:
            funnel_lift, gained_final, reach = funnel_lift_from_step_lift(funnel, analysis, p.target_step, p.expected_lift, self.marginal_decay)
            score = gained_final * p.confidence * p.effort_weight
            ranked.append(RankedProposal(proposal=p, reach=reach, expected_users_gained=round(gained_final, 1),
                                         expected_funnel_lift=round(funnel_lift, 4), priority_score=round(score, 1), rank=0))
        ranked.sort(key=lambda r: -r.priority_score)
        for i, r in enumerate(ranked, 1):
            r.rank = i
        return ranked

    # --- strategies --------------------------------------------------------

    def _from_playbook(self, funnel: Funnel, analysis: FunnelAnalysis, n: int, exclude: set[str]) -> list[ExperimentProposal]:
        out: list[ExperimentProposal] = []
        # weight patterns toward the leakiest steps
        leak_order = sorted(analysis.dropoffs, key=lambda d: -d.users_lost)
        for d in leak_order:
            t = step_type(d.from_step)
            for pat in self.playbook:
                if t in pat["applies_to"] and pat["key"] not in exclude and all(o.key != pat["key"] for o in out):
                    out.append(ExperimentProposal(
                        key=pat["key"], name=pat["name"], hypothesis=pat["hypothesis"], target_step=d.from_step,
                        change=pat["change"], category=pat["category"], expected_lift=pat["expected_lift"],
                        confidence=pat["confidence"], effort=pat["effort"],
                        rationale=f"{d.from_step}->{d.to_step} loses {d.users_lost:,} users ({d.share_of_total_loss:.0%} of all loss) at {d.step_rate:.0%}; playbook pattern '{pat['key']}' applies to {t} steps.",
                        playbook_ref=pat["key"],
                    ))
        return out[:n]

    def _from_llm(self, funnel: Funnel, analysis: FunnelAnalysis, n: int, exclude: set[str]) -> list[ExperimentProposal] | None:
        if not self.llm.enabled:
            return None
        user = (
            f"Product: {funnel.product}\nFunnel: {funnel.name}\nContext: {funnel.context}\n\n"
            f"Steps (users in 28 days): " + " -> ".join(f"{s.name} ({s.users:,})" for s in funnel.steps) + "\n"
            f"Overall conversion: {analysis.overall_conversion:.1%}\n"
            "Dropoffs:\n" + "\n".join(f"  {d.from_step}->{d.to_step}: {d.step_rate:.0%} continue, {d.users_lost:,} lost ({d.share_of_total_loss:.0%} of loss)" for d in analysis.dropoffs) + "\n"
            + ("Benchmarks: " + "; ".join(analysis.benchmark_notes) + "\n" if analysis.benchmark_notes else "")
            + (f"Already tried (do not repeat): {sorted(exclude)}\n" if exclude else "")
            + f"\nPlaybook of known patterns for reference (you may use, adapt, or go beyond them):\n"
            + "\n".join(f"- {p['key']}: {p['name']} ({p['category']}, lift prior {p['expected_lift']:.0%})" for p in self.playbook)
            + f"\n\nPropose {n} experiments as a JSON array of objects with keys: key (kebab-case slug), name, hypothesis, "
              f"target_step (one of {[s.name for s in funnel.steps[:-1]]}), change, category "
              "(copy|layout|friction|social_proof|pricing|onboarding|trust), expected_lift (0-0.4), confidence (0.3-0.8), "
              "effort (S|M|L), rationale, playbook_ref (slug or null)."
        )
        try:
            data = self.llm.json(SYSTEM, user)
            if not isinstance(data, list):
                return None
            out = []
            for item in data:
                item.setdefault("playbook_ref", None)
                p = ExperimentProposal.model_validate(item)
                if p.key not in exclude:
                    out.append(p)
            return out or None
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM proposal failed (%s); using playbook", exc)
            return None
