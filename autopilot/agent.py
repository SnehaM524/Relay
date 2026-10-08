"""The agent loop.

    analyze(funnel) -> propose + rank -> approve top N -> generate artifacts -> ship
    -> observe(arms) -> evaluate -> at significance: readout, ship/kill -> next

`observe` takes real arm counts (from Mixpanel/PostHog/your warehouse) or, in
simulation, draws them from a model of the true effect so the whole loop can be
exercised end to end offline.
"""

from __future__ import annotations

import logging
import math
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .funnel import analyze, load_funnels
from .generator import Generator
from .llm import LLM
from .models import (ArmResult, Experiment, ExperimentStatus, Funnel, FunnelAnalysis, ProgramSummary, RankedProposal)
from .proposer import Proposer, funnel_lift_from_step_lift, load_playbook
from .readout import ReadoutWriter
from .settings import Settings
from .stats import evaluate
from .store import Store

log = logging.getLogger(__name__)


def _binom(rng: random.Random, n: int, p: float) -> int:
    if hasattr(rng, "binomialvariate"):  # py3.12+
        return rng.binomialvariate(n, p)
    return sum(1 for _ in range(n) if rng.random() < p)


class Agent:
    def __init__(self, settings: Settings | None = None, store: Store | None = None, llm: LLM | None = None):
        self.settings = settings or Settings()
        self.store = store or Store(self.settings.database_url)
        self.llm = llm or LLM(self.settings)
        self.playbook = load_playbook(Path(self.settings.config_dir) / "playbook.yaml")
        self.proposer = Proposer(self.llm, self.playbook, self.settings.marginal_decay)
        self.generator = Generator(self.llm, self.settings.flag_provider)
        self.readouts = ReadoutWriter(self.llm)
        self.funnels: dict[str, Funnel] = {}

    # --- funnels -----------------------------------------------------------

    def load(self, path: str | Path | None = None) -> list[Funnel]:
        fs = load_funnels(path or Path(self.settings.data_dir) / "funnels.json")
        for f in fs:
            self.funnels[f.id] = f
        return fs

    def analyze(self, funnel_id: str) -> FunnelAnalysis:
        return analyze(self.funnels[funnel_id])

    # --- propose -----------------------------------------------------------

    def propose(self, funnel_id: str, n: int = 8) -> list[Experiment]:
        funnel = self.funnels[funnel_id]
        analysis = self.analyze(funnel_id)
        proposals = self.proposer.propose(funnel, analysis, n=n, exclude_keys=self.store.keys_for(funnel_id))
        ranked = self.proposer.rank(funnel, analysis, proposals)
        out: list[Experiment] = []
        for r in ranked:
            exp = Experiment(id=f"{funnel_id}:{r.proposal.key}", funnel_id=funnel_id, proposal=r.proposal, ranking=r)
            exp.transition(ExperimentStatus.PROPOSED, f"rank {r.rank}, score {r.priority_score}")
            self.store.save(exp)
            out.append(exp)
        return out

    # --- approve / generate / ship ----------------------------------------

    def approve(self, exp_id: str) -> Experiment:
        exp = self._get(exp_id)
        exp.transition(ExperimentStatus.APPROVED)
        self.store.save(exp)
        return exp

    def generate(self, exp_id: str, write_to: Path | None = None) -> Experiment:
        exp = self._get(exp_id)
        exp.artifact = self.generator.generate(exp, self.funnels[exp.funnel_id])
        exp.transition(ExperimentStatus.GENERATED, f"{len(exp.artifact.files)} files, flag {exp.artifact.flag_key}")
        if write_to:
            for rel, content in exp.artifact.files.items():
                p = Path(write_to) / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content)
        self.store.save(exp)
        return exp

    def ship(self, exp_id: str, at: datetime | None = None) -> Experiment:
        exp = self._get(exp_id)
        running = self.store.list(exp.funnel_id, ExperimentStatus.RUNNING)
        if len(running) >= self.settings.max_concurrent:
            raise RuntimeError(f"{exp.funnel_id} already has {len(running)} running experiments (max {self.settings.max_concurrent})")
        if exp.artifact is None:
            self.generate(exp_id)
            exp = self._get(exp_id)
        exp.started_at = at or datetime.now(timezone.utc)
        exp.transition(ExperimentStatus.RUNNING, f"flag {exp.artifact.flag_key} at 50/50")
        self.store.save(exp)
        return exp

    def skip(self, exp_id: str, why: str = "") -> Experiment:
        exp = self._get(exp_id)
        exp.transition(ExperimentStatus.SKIPPED, why)
        self.store.save(exp)
        return exp

    # --- observe / evaluate ------------------------------------------------

    def observe(self, exp_id: str, control: ArmResult, treatment: ArmResult, now: datetime | None = None) -> Experiment:
        exp = self._get(exp_id)
        now = now or datetime.now(timezone.utc)
        days = max(0, (now - exp.started_at).days) if exp.started_at else 0
        s = self.settings
        exp.stats = evaluate(control, treatment, alpha=s.alpha, power_target=s.power_target, mde_relative=s.min_detectable_effect,
                             days_running=days, min_days=s.min_days, max_days=s.max_days)
        if exp.stats.decision in ("ship", "kill", "inconclusive"):
            exp.ended_at = now
            exp.readout_md = self.readouts.write(exp, self.funnels[exp.funnel_id])
            status = {"ship": ExperimentStatus.WON, "kill": ExperimentStatus.LOST, "inconclusive": ExperimentStatus.INCONCLUSIVE}[exp.stats.decision]
            exp.transition(status, f"{exp.stats.relative_lift:+.1%} p={exp.stats.p_value:.3f} after {days}d")
        self.store.save(exp)
        return exp

    # --- reporting ---------------------------------------------------------

    def summary(self) -> ProgramSummary:
        all_exps = self.store.list()
        won = [e for e in all_exps if e.status == ExperimentStatus.WON]
        # Per funnel: compound each winner's observed step lift into a funnel lift (marginal users
        # decay downstream). Across funnels: weight by signups so a small funnel can't dominate.
        per_funnel: dict[str, float] = {fid: 1.0 for fid in self.funnels}
        winners = []
        for e in won:
            step_lift = e.stats.relative_lift if e.stats else 0.0
            funnel = self.funnels.get(e.funnel_id)
            if funnel:
                funnel_lift, _, _ = funnel_lift_from_step_lift(funnel, analyze(funnel), e.proposal.target_step, step_lift, self.settings.marginal_decay)
            else:
                funnel_lift = step_lift
            per_funnel[e.funnel_id] = per_funnel.get(e.funnel_id, 1.0) * (1 + funnel_lift)
            winners.append({"id": e.id, "name": e.proposal.name, "funnel": e.funnel_id, "step_lift": round(step_lift, 4),
                            "funnel_lift": round(funnel_lift, 4), "p": e.stats.p_value if e.stats else None})
        weights = {fid: f.steps[-1].users for fid, f in self.funnels.items()}
        total_w = sum(weights.values()) or 1
        combined = sum((per_funnel.get(fid, 1.0) - 1) * w for fid, w in weights.items()) / total_w + 1
        shipped = [e for e in all_exps if e.status in (ExperimentStatus.RUNNING, ExperimentStatus.WON, ExperimentStatus.LOST, ExperimentStatus.INCONCLUSIVE)]
        return ProgramSummary(
            funnels=len({e.funnel_id for e in all_exps}), proposed=len(all_exps), shipped=len(shipped), won=len(won),
            lost=sum(e.status == ExperimentStatus.LOST for e in all_exps),
            inconclusive=sum(e.status == ExperimentStatus.INCONCLUSIVE for e in all_exps),
            running=sum(e.status == ExperimentStatus.RUNNING for e in all_exps),
            combined_lift=round(combined - 1, 4), winners=winners,
        )

    # --- simulation --------------------------------------------------------

    def simulate(self, seed: int = 1, per_funnel: int = 11, ship_top: int = 7, log_fn=None) -> ProgramSummary:
        """Run the whole program offline. The 'true' effect of each experiment is drawn
        around the agent's prior (shrunk toward zero), so some win, some lose, some are null."""
        rng = random.Random(seed)
        say = log_fn or (lambda *_: None)
        for funnel in list(self.funnels.values()):
            analysis = self.analyze(funnel.id)
            say(f"\n== {funnel.name} ({funnel.id}): {analysis.overall_conversion:.1%} end-to-end, biggest leak {analysis.biggest_leak.from_step}->{analysis.biggest_leak.to_step} ({analysis.biggest_leak.users_lost:,} lost)")
            exps = self.propose(funnel.id, n=per_funnel)
            for e in exps:
                say(f"  #{e.ranking.rank:<2} {e.proposal.name:<48} step={e.proposal.target_step:<12} lift≈{e.proposal.expected_lift:+.0%} score={e.ranking.priority_score:>7.1f}")
            # ship the top N, respecting concurrency by running them in waves
            queue = exps[:ship_top]
            for e in exps[ship_top:]:
                self.skip(e.id, "below cut line")
            t = datetime(2026, 6, 1, tzinfo=timezone.utc)
            step_rate = {d.from_step: d.step_rate for d in analysis.dropoffs}
            entered = {d.from_step: d.entered for d in analysis.dropoffs}
            while queue:
                wave, queue = queue[: self.settings.max_concurrent], queue[self.settings.max_concurrent :]
                for e in wave:
                    self.generate(e.id)
                    self.ship(e.id, at=t)
                # advance day by day until each resolves
                day = 0
                active = {e.id for e in wave}
                base = {e.id: step_rate.get(e.proposal.target_step, 0.3) for e in wave}
                # true effect: with prob=confidence the idea works (shrunk toward 60% of the prior);
                # otherwise it's a null or mildly harmful change.
                true = {e.id: rng.gauss(e.proposal.expected_lift * 0.6, 0.05) if rng.random() < e.proposal.confidence else rng.gauss(-0.015, 0.04) for e in wave}
                daily = {e.id: int(entered.get(e.proposal.target_step, 1000) / funnel.window_days / len(wave)) for e in wave}
                acc = {e.id: [0, 0, 0, 0] for e in wave}
                while active and day < self.settings.max_days:
                    day += 1
                    for eid in list(active):
                        n = daily[eid]
                        c_users, t_users = n, n
                        c_conv = _binom(rng, c_users, base[eid])
                        t_conv = _binom(rng, t_users, min(0.99, base[eid] * (1 + true[eid])))
                        a = acc[eid]; a[0] += c_users; a[1] += c_conv; a[2] += t_users; a[3] += t_conv
                        exp = self.observe(eid, ArmResult(name="control", users=a[0], conversions=a[1]),
                                           ArmResult(name="treatment", users=a[2], conversions=a[3]), now=t + timedelta(days=day))
                        if exp.status != ExperimentStatus.RUNNING:
                            active.discard(eid)
                            say(f"  -> {exp.status.value.upper():<13} {exp.proposal.name:<48} {exp.stats.relative_lift:+.1%} (p={exp.stats.p_value:.3f}, {day}d, true {true[eid]:+.1%})")
                t += timedelta(days=day)
        return self.summary()

    # --- helpers -----------------------------------------------------------

    def _get(self, exp_id: str) -> Experiment:
        exp = self.store.get(exp_id)
        if not exp:
            raise KeyError(exp_id)
        return exp
