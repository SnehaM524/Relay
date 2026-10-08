"""SLA tracking: start the clock on handoff, record accept/contact, escalate on
breach, and reassign after N breaches.

Response time = handoff -> first "contacted" signal. The signal can come from
the Slack button, from a Salesforce status change (poll or Platform Event), or
from any outbound-activity webhook (Outreach/Salesloft) hitting
`POST /leads/{id}/contacted`.
"""

from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta, timezone

from .models import LeadRecord, LeadStage, SLAState, Tier

log = logging.getLogger(__name__)


class SLATracker:
    def __init__(self, minutes_by_tier: dict[str, int], reassign_after_breaches: int = 1):
        self.minutes = minutes_by_tier
        self.reassign_after = reassign_after_breaches

    def minutes_for(self, tier: Tier) -> int:
        return int(self.minutes.get(tier.value, self.minutes.get("C", 60)))

    def start(self, rec: LeadRecord, now: datetime | None = None) -> SLAState:
        assert rec.routing and rec.routing.sdr and rec.score
        now = now or datetime.now(timezone.utc)
        rec.sla = SLAState(
            lead_id=rec.id,
            sdr_id=rec.routing.sdr.id,
            tier=rec.score.tier,
            handed_off_at=now,
            due_at=now + timedelta(minutes=self.minutes_for(rec.score.tier)),
        )
        return rec.sla

    def accept(self, rec: LeadRecord, now: datetime | None = None) -> None:
        if rec.sla and rec.sla.accepted_at is None:
            rec.sla.accepted_at = now or datetime.now(timezone.utc)
            rec.transition(LeadStage.ACCEPTED, "accepted by SDR")

    def contacted(self, rec: LeadRecord, now: datetime | None = None) -> float | None:
        if not rec.sla or rec.sla.contacted_at is not None:
            return rec.sla.response_seconds if rec.sla else None
        now = now or datetime.now(timezone.utc)
        rec.sla.contacted_at = now
        if now > rec.sla.due_at:
            rec.sla.breached = True
        rec.transition(LeadStage.CONTACTED, f"first response in {rec.sla.response_seconds:.0f}s")
        return rec.sla.response_seconds

    def check(self, rec: LeadRecord, now: datetime | None = None) -> str | None:
        """Return 'escalate', 'reassign', or None."""
        if not rec.sla or rec.sla.contacted_at is not None or rec.stage not in (LeadStage.HANDED_OFF, LeadStage.ACCEPTED):
            return None
        now = now or datetime.now(timezone.utc)
        if now < rec.sla.due_at:
            return None
        if not rec.sla.breached:
            rec.sla.breached = True
            rec.sla.escalated_at = now
            return "escalate"
        breaches = sum(1 for h in rec.history if h.get("note", "").startswith("sla breach"))
        # one grace window after escalation before reassignment
        if now >= rec.sla.escalated_at + timedelta(minutes=self.minutes_for(rec.sla.tier)) and breaches < self.reassign_after + 1:
            return "reassign"
        return None

    def minutes_overdue(self, rec: LeadRecord, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return max(0.0, (now - rec.sla.due_at).total_seconds() / 60) if rec.sla else 0.0

    @staticmethod
    def summarize(response_seconds: list[float]) -> dict[str, float | int]:
        if not response_seconds:
            return {"count": 0, "median_s": 0.0, "p90_s": 0.0, "mean_s": 0.0}
        s = sorted(response_seconds)
        p90 = s[min(len(s) - 1, int(round(0.9 * (len(s) - 1))))]
        return {
            "count": len(s),
            "median_s": round(statistics.median(s), 1),
            "p90_s": round(p90, 1),
            "mean_s": round(statistics.fmean(s), 1),
        }
