"""Core data models shared across the pipeline."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _check_email(v: str) -> str:
    v = (v or "").strip().lower()
    if "@" not in v or v.startswith("@") or v.endswith("@") or " " in v:
        raise ValueError(f"invalid email: {v!r}")
    return v


class LeadStage(str, Enum):
    RECEIVED = "received"
    ENRICHED = "enriched"
    SCORED = "scored"
    ROUTED = "routed"
    HANDED_OFF = "handed_off"
    ACCEPTED = "accepted"
    CONTACTED = "contacted"
    REASSIGNED = "reassigned"
    DISQUALIFIED = "disqualified"
    FAILED = "failed"


class Tier(str, Enum):
    """ICP tier. A = hot, B = fit, C = nurture, D = disqualify."""

    A = "A"
    B = "B"
    C = "C"
    D = "D"


class Lead(BaseModel):
    """Raw lead as received from HubSpot."""

    hubspot_contact_id: str
    email: str
    first_name: str | None = None
    last_name: str | None = None
    company: str | None = None
    domain: str | None = None
    title: str | None = None
    country: str | None = None
    source: str | None = None
    hubspot_properties: dict[str, Any] = Field(default_factory=dict)
    received_at: datetime = Field(default_factory=utcnow)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return _check_email(v)

    @property
    def full_name(self) -> str:
        return " ".join(p for p in [self.first_name, self.last_name] if p) or self.email

    def inferred_domain(self) -> str | None:
        if self.domain:
            return self.domain.lower()
        if "@" in self.email:
            dom = self.email.split("@", 1)[1].lower()
            return dom
        return None


class Enrichment(BaseModel):
    """Merged enrichment data from Clay and Harmonic."""

    employee_count: int | None = None
    industry: str | None = None
    funding_stage: str | None = None
    total_funding_usd: float | None = None
    last_funding_date: datetime | None = None
    headcount_growth_6mo_pct: float | None = None
    tech_stack: list[str] = Field(default_factory=list)
    seniority: str | None = None
    department: str | None = None
    linkedin_url: str | None = None
    hq_country: str | None = None
    is_free_email: bool = False
    harmonic_score: float | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
    providers_used: list[str] = Field(default_factory=list)


class ScoreBreakdown(BaseModel):
    rule: str
    points: float
    reason: str


class Score(BaseModel):
    total: float
    tier: Tier
    breakdown: list[ScoreBreakdown] = Field(default_factory=list)
    disqualify_reasons: list[str] = Field(default_factory=list)

    @property
    def disqualified(self) -> bool:
        return self.tier == Tier.D


class SDR(BaseModel):
    id: str
    name: str
    email: str
    slack_user_id: str
    salesforce_user_id: str | None = None
    team: str
    territories: list[str] = Field(default_factory=list)
    segments: list[str] = Field(default_factory=list)
    active: bool = True
    capacity: int = 50
    open_leads: int = 0
    working_hours: dict[str, Any] | None = None
    weight: float = 1.0


class RoutingDecision(BaseModel):
    sdr: SDR | None
    team: str
    strategy: str
    reason: str
    fallback: bool = False


class SLAState(BaseModel):
    lead_id: str
    sdr_id: str
    tier: Tier
    handed_off_at: datetime
    due_at: datetime
    accepted_at: datetime | None = None
    contacted_at: datetime | None = None
    escalated_at: datetime | None = None
    reassigned_at: datetime | None = None
    breached: bool = False

    @property
    def response_seconds(self) -> float | None:
        if self.contacted_at is None:
            return None
        return (self.contacted_at - self.handed_off_at).total_seconds()


class LeadRecord(BaseModel):
    """Full state of a lead as it moves through the pipeline."""

    id: str
    lead: Lead
    stage: LeadStage = LeadStage.RECEIVED
    enrichment: Enrichment | None = None
    score: Score | None = None
    routing: RoutingDecision | None = None
    sla: SLAState | None = None
    salesforce_lead_id: str | None = None
    slack_message_ts: str | None = None
    slack_channel: str | None = None
    errors: list[str] = Field(default_factory=list)
    history: list[dict[str, Any]] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=utcnow)

    def transition(self, stage: LeadStage, note: str | None = None) -> None:
        self.stage = stage
        self.updated_at = utcnow()
        self.history.append({"stage": stage.value, "at": self.updated_at.isoformat(), "note": note})
