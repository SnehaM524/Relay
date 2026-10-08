"""Slack handoff: post a Block Kit card to the handoff channel, DM the SDR,
and handle Accept / Contacted / Reassign button clicks via interactivity.

Interactive payloads arrive at `/webhooks/slack/interactive` (see relay.api);
the `action_id` and `value` (lead id) drive SLA transitions.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any

import httpx

from ..models import LeadRecord, Tier

log = logging.getLogger(__name__)

TIER_EMOJI = {Tier.A: ":fire:", Tier.B: ":large_green_circle:", Tier.C: ":large_yellow_circle:", Tier.D: ":no_entry:"}


def build_handoff_blocks(rec: LeadRecord, sla_minutes: int) -> list[dict[str, Any]]:
    lead, e, s, r = rec.lead, rec.enrichment, rec.score, rec.routing
    assert s and r and r.sdr
    tier_label = f"{TIER_EMOJI[s.tier]} *Tier {s.tier.value}* · score {s.total:g}"
    company_line = " · ".join(
        p for p in [
            lead.company or lead.inferred_domain(),
            f"{e.employee_count:,} employees" if e and e.employee_count else None,
            e.industry if e else None,
            e.funding_stage if e else None,
        ] if p
    )
    top = sorted(s.breakdown, key=lambda b: -b.points)[:4]
    signals = [f"{b.rule.replace('_', ' ').capitalize()} +{b.points:g}" for b in top]
    sf_link = f"<https://your-instance.lightning.force.com/{rec.salesforce_lead_id}|Open in Salesforce>" if rec.salesforce_lead_id else "_Salesforce sync pending_"
    return [
        {"type": "header", "text": {"type": "plain_text", "text": f"New {s.tier.value}-tier lead: {lead.full_name}"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Owner*\n<@{r.sdr.slack_user_id}>"},
            {"type": "mrkdwn", "text": f"*SLA*\n{sla_minutes} min"},
            {"type": "mrkdwn", "text": f"*Fit*\n{tier_label}"},
            {"type": "mrkdwn", "text": f"*Source*\n{lead.source or 'unknown'}"},
        ]},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{lead.title or 'Unknown title'}* at {company_line}\n{lead.email}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "*Why routed:* " + r.reason}]},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "*Signals:* " + (" · ".join(signals) if signals else "—")}]},
        {"type": "section", "text": {"type": "mrkdwn", "text": sf_link}},
        {"type": "actions", "elements": [
            {"type": "button", "style": "primary", "text": {"type": "plain_text", "text": "Accept"}, "action_id": "lead_accept", "value": rec.id},
            {"type": "button", "text": {"type": "plain_text", "text": "Mark contacted"}, "action_id": "lead_contacted", "value": rec.id},
            {"type": "button", "style": "danger", "text": {"type": "plain_text", "text": "Reassign"}, "action_id": "lead_reassign", "value": rec.id},
        ]},
    ]


class SlackClient:
    def __init__(self, bot_token: str, signing_secret: str | None = None, timeout: float = 5.0):
        self.bot_token = bot_token
        self.signing_secret = signing_secret
        self.timeout = timeout
        self.base = "https://slack.com/api"

    async def _call(self, method: str, **payload: Any) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(f"{self.base}/{method}", json=payload,
                                  headers={"Authorization": f"Bearer {self.bot_token}", "Content-Type": "application/json; charset=utf-8"})
            r.raise_for_status()
            data = r.json()
            if not data.get("ok"):
                raise RuntimeError(f"slack {method} failed: {data.get('error')}")
            return data

    async def post_message(self, channel: str, text: str, blocks: list[dict[str, Any]] | None = None, thread_ts: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"channel": channel, "text": text}
        if blocks:
            payload["blocks"] = blocks
        if thread_ts:
            payload["thread_ts"] = thread_ts
        return await self._call("chat.postMessage", **payload)

    async def update_message(self, channel: str, ts: str, text: str, blocks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"channel": channel, "ts": ts, "text": text}
        if blocks is not None:
            payload["blocks"] = blocks
        return await self._call("chat.update", **payload)

    async def dm(self, user_id: str, text: str, blocks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        opened = await self._call("conversations.open", users=user_id)
        return await self.post_message(opened["channel"]["id"], text, blocks)

    def verify_signature(self, body: bytes, timestamp: str, signature: str) -> bool:
        if not self.signing_secret:
            return True
        if abs(time.time() - int(timestamp)) > 60 * 5:
            return False
        base = f"v0:{timestamp}:{body.decode()}"
        digest = hmac.new(self.signing_secret.encode(), base.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(f"v0={digest}", signature)


class MockSlackClient:
    def __init__(self):
        self.messages: list[dict[str, Any]] = []
        self.dms: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self._ts = 1000

    async def post_message(self, channel: str, text: str, blocks=None, thread_ts=None) -> dict[str, Any]:
        self._ts += 1
        ts = f"{self._ts}.000100"
        self.messages.append({"channel": channel, "text": text, "blocks": blocks, "ts": ts, "thread_ts": thread_ts})
        return {"ok": True, "ts": ts, "channel": channel}

    async def update_message(self, channel: str, ts: str, text: str, blocks=None) -> dict[str, Any]:
        self.updates.append({"channel": channel, "ts": ts, "text": text, "blocks": blocks})
        return {"ok": True}

    async def dm(self, user_id: str, text: str, blocks=None) -> dict[str, Any]:
        self.dms.append({"user": user_id, "text": text, "blocks": blocks})
        return {"ok": True, "ts": "dm.1"}

    def verify_signature(self, body: bytes, timestamp: str, signature: str) -> bool:
        return True


class SlackHandoff:
    def __init__(self, client: SlackClient | MockSlackClient, handoff_channel: str, escalation_channel: str):
        self.client = client
        self.handoff_channel = handoff_channel
        self.escalation_channel = escalation_channel

    async def send(self, rec: LeadRecord, sla_minutes: int) -> tuple[str, str]:
        assert rec.routing and rec.routing.sdr
        blocks = build_handoff_blocks(rec, sla_minutes)
        text = f"New {rec.score.tier.value}-tier lead {rec.lead.full_name} ({rec.lead.company or rec.lead.inferred_domain()}) -> {rec.routing.sdr.name}. SLA {sla_minutes} min."
        posted = await self.client.post_message(self.handoff_channel, text, blocks)
        await self.client.dm(rec.routing.sdr.slack_user_id, f":bell: You've been assigned *{rec.lead.full_name}* — respond within {sla_minutes} min.", blocks)
        return posted["channel"], posted["ts"]

    async def mark(self, rec: LeadRecord, status: str) -> None:
        if not rec.slack_channel or not rec.slack_message_ts:
            return
        await self.client.post_message(rec.slack_channel, status, thread_ts=rec.slack_message_ts)

    async def escalate(self, rec: LeadRecord, minutes_overdue: float, manager_user_id: str | None = None) -> None:
        assert rec.routing and rec.routing.sdr
        who = f"<@{manager_user_id}> " if manager_user_id else ""
        text = (f":rotating_light: {who}SLA breach: *{rec.lead.full_name}* ({rec.lead.company or rec.lead.inferred_domain()}) "
                f"assigned to <@{rec.routing.sdr.slack_user_id}> is {minutes_overdue:.0f} min overdue.")
        await self.client.post_message(self.escalation_channel, text)
        await self.mark(rec, f":rotating_light: SLA breached — {minutes_overdue:.0f} min overdue")

    async def reassigned(self, rec: LeadRecord, old_sdr_name: str, sla_minutes: int) -> tuple[str, str]:
        await self.mark(rec, f":arrows_counterclockwise: Reassigned from {old_sdr_name} to {rec.routing.sdr.name}")
        return await self.send(rec, sla_minutes)
