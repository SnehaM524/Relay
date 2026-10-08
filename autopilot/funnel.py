"""Funnel loading and leak analysis."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from .models import Funnel, FunnelAnalysis, FunnelStep, StepDropoff

# Rough B2B SaaS benchmarks for step-to-step rates, used only to annotate the analysis.
BENCHMARKS: dict[str, tuple[float, float]] = {
    "landing->signup_form": (0.20, 0.35),
    "signup_form->email_verify": (0.55, 0.75),
    "email_verify->onboarding": (0.75, 0.90),
    "onboarding->activation": (0.50, 0.70),
    "pricing->checkout": (0.15, 0.30),
    "checkout->activation": (0.45, 0.65),
}


def load_funnels(path: str | Path) -> list[Funnel]:
    p = Path(path)
    if p.suffix == ".json":
        return [Funnel.model_validate(f) for f in json.loads(p.read_text())]
    if p.suffix == ".csv":
        # columns: funnel_id, funnel_name, product, step, users
        by_id: dict[str, dict] = {}
        with open(p) as f:
            for row in csv.DictReader(f):
                fid = row["funnel_id"]
                entry = by_id.setdefault(fid, {"id": fid, "name": row.get("funnel_name", fid), "product": row.get("product", ""), "steps": []})
                entry["steps"].append(FunnelStep(name=row["step"], users=int(row["users"])))
        return [Funnel.model_validate(v) for v in by_id.values()]
    raise ValueError(f"unsupported funnel file: {p}")


def analyze(funnel: Funnel) -> FunnelAnalysis:
    steps = funnel.steps
    total_lost = steps[0].users - steps[-1].users
    drops: list[StepDropoff] = []
    notes: list[str] = []
    for a, b in zip(steps, steps[1:]):
        rate = b.users / a.users if a.users else 0.0
        lost = a.users - b.users
        drops.append(StepDropoff(
            from_step=a.name, to_step=b.name, entered=a.users, continued=b.users,
            step_rate=round(rate, 4), dropoff_rate=round(1 - rate, 4), users_lost=lost,
            share_of_total_loss=round(lost / total_lost, 4) if total_lost else 0.0,
        ))
        key = f"{a.name}->{b.name}"
        if key in BENCHMARKS:
            lo, hi = BENCHMARKS[key]
            if rate < lo:
                notes.append(f"{key}: {rate:.0%} is below the typical {lo:.0%}-{hi:.0%} range: strongest candidate for intervention")
            elif rate > hi:
                notes.append(f"{key}: {rate:.0%} is above the typical {lo:.0%}-{hi:.0%} range: likely not the bottleneck")
    biggest = max(drops, key=lambda d: d.users_lost)
    return FunnelAnalysis(
        funnel_id=funnel.id,
        overall_conversion=round(funnel.conversion, 4),
        total_lost=total_lost,
        dropoffs=drops,
        biggest_leak=biggest,
        benchmark_notes=notes,
    )


def step_type(step_name: str) -> str:
    """Map a step name to a playbook step type."""
    n = step_name.lower()
    for t in ("landing", "signup_form", "email_verify", "onboarding", "pricing", "checkout", "activation", "invite"):
        if t in n:
            return t
    if "verify" in n or "confirm" in n:
        return "email_verify"
    if "form" in n or "signup" in n or "register" in n:
        return "signup_form"
    if "pay" in n or "card" in n or "billing" in n:
        return "checkout"
    if "home" in n or "hero" in n:
        return "landing"
    return "onboarding"
