"""Core data models."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- Funnel ------------------------------------------------------------------


class FunnelStep(BaseModel):
    name: str
    users: int
    description: str | None = None


class Funnel(BaseModel):
    id: str
    name: str
    product: str
    steps: list[FunnelStep]
    window_days: int = 28
    daily_entrants: int | None = None  # for sample-size planning
    context: str | None = None  # free text about the product / audience

    @property
    def conversion(self) -> float:
        if not self.steps or self.steps[0].users == 0:
            return 0.0
        return self.steps[-1].users / self.steps[0].users


class StepDropoff(BaseModel):
    from_step: str
    to_step: str
    entered: int
    continued: int
    step_rate: float
    dropoff_rate: float
    users_lost: int
    share_of_total_loss: float


class FunnelAnalysis(BaseModel):
    funnel_id: str
    overall_conversion: float
    total_lost: int
    dropoffs: list[StepDropoff]
    biggest_leak: StepDropoff
    benchmark_notes: list[str] = Field(default_factory=list)


# --- Experiments -------------------------------------------------------------


class ExperimentStatus(str, Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    GENERATED = "generated"
    RUNNING = "running"
    WON = "won"
    LOST = "lost"
    INCONCLUSIVE = "inconclusive"
    SKIPPED = "skipped"


class ExperimentProposal(BaseModel):
    """What the agent proposes. Expected lift is relative (0.08 = +8% on the step rate)."""

    key: str  # slug, e.g. "social-proof-on-pricing"
    name: str
    hypothesis: str
    target_step: str  # the step whose rate we want to move
    change: str  # what we'd actually change, concretely
    category: str  # copy | layout | friction | social_proof | pricing | onboarding | trust
    expected_lift: float  # relative lift on target step rate
    confidence: float  # 0..1, how sure the agent is about the lift
    effort: str  # S | M | L
    rationale: str
    playbook_ref: str | None = None

    @property
    def effort_weight(self) -> float:
        return {"S": 1.0, "M": 0.7, "L": 0.4}.get(self.effort, 0.7)


class RankedProposal(BaseModel):
    proposal: ExperimentProposal
    reach: int  # users entering the target step per window
    expected_users_gained: float
    expected_funnel_lift: float  # relative lift on overall funnel conversion
    priority_score: float
    rank: int


class VariantArtifact(BaseModel):
    component_name: str
    react_code: str
    flag_key: str
    flag_config: dict[str, Any]
    files: dict[str, str] = Field(default_factory=dict)  # path -> content
    notes: str | None = None


class ArmResult(BaseModel):
    name: str
    users: int
    conversions: int

    @property
    def rate(self) -> float:
        return self.conversions / self.users if self.users else 0.0


class StatsResult(BaseModel):
    control: ArmResult
    treatment: ArmResult
    relative_lift: float
    absolute_lift: float
    p_value: float
    z: float
    ci_low: float  # 95% CI on relative lift
    ci_high: float
    significant: bool
    power: float
    required_per_arm: int
    days_running: int
    decision: str  # keep_running | ship | kill | inconclusive
    sequential_boundary_hit: bool = False
    alpha_threshold: float = 0.05  # the (sequentially adjusted) p-value threshold used for this look
    sample_fraction: float = 0.0  # observed per-arm n / required per-arm n


class Experiment(BaseModel):
    id: str
    funnel_id: str
    proposal: ExperimentProposal
    ranking: RankedProposal | None = None
    status: ExperimentStatus = ExperimentStatus.PROPOSED
    artifact: VariantArtifact | None = None
    stats: StatsResult | None = None
    readout_md: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    history: list[dict[str, Any]] = Field(default_factory=list)

    def transition(self, status: ExperimentStatus, note: str | None = None) -> None:
        self.status = status
        self.history.append({"status": status.value, "at": utcnow().isoformat(), "note": note})


class ProgramSummary(BaseModel):
    funnels: int
    proposed: int
    shipped: int
    won: int
    lost: int
    inconclusive: int
    running: int
    combined_lift: float  # compounded relative lift on signup conversion from winners
    winners: list[dict[str, Any]]
