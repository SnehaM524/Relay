"""Pipeline orchestration: one function per lead, idempotent, resumable.

    receive -> enrich -> score -> route -> salesforce upsert -> slack handoff -> SLA start

Every stage persists the record, so a crash mid-way can be replayed with
`Pipeline.resume(lead_id)`.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .crm.hubspot import HubSpotClient, MockHubSpotClient
from .crm.salesforce import MockSalesforceClient, SalesforceClient
from .enrichment import ClayClient, Enricher, HarmonicClient, MockClayClient, MockHarmonicClient
from .handoff.slack import MockSlackClient, SlackClient, SlackHandoff
from .models import Lead, LeadRecord, LeadStage, Tier
from .routing import Roster, Router
from .scoring import ICPScorer
from .settings import Settings
from .sla import SLATracker
from .store import LeadStore

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, *, settings: Settings, store: LeadStore, hubspot, salesforce, enricher: Enricher,
                 scorer: ICPScorer, router: Router, roster: Roster, slack: SlackHandoff, sla: SLATracker):
        self.settings = settings
        self.store = store
        self.hubspot = hubspot
        self.salesforce = salesforce
        self.enricher = enricher
        self.scorer = scorer
        self.router = router
        self.roster = roster
        self.slack = slack
        self.sla = sla
        self.metrics: dict[str, Any] = {"processed": 0, "disqualified": 0, "unrouted": 0, "errors": 0, "stage_ms": {}}

    # --- construction ------------------------------------------------------

    @classmethod
    def build(cls, settings: Settings | None = None) -> "Pipeline":
        s = settings or Settings()
        cfg = Path(s.config_dir)
        store = LeadStore(s.database_url)
        roster = Roster.from_file(cfg / "sdrs.yaml")
        router = Router.from_file(cfg / "routing.yaml", roster, store)
        scorer = ICPScorer.from_file(cfg / "icp.yaml")
        sla = SLATracker(s.sla_minutes, s.sla_reassign_after_breaches)

        if s.mock_integrations:
            hubspot, salesforce = MockHubSpotClient(), MockSalesforceClient()
            providers = [MockClayClient(), MockHarmonicClient()]
            slack_client: SlackClient | MockSlackClient = MockSlackClient()
        else:
            hubspot = HubSpotClient(s.hubspot_access_token or "")
            salesforce = SalesforceClient(s.sf_instance_url or "", s.sf_client_id or "", s.sf_client_secret or "",
                                          s.sf_username or "", s.sf_password or "", s.sf_security_token or "", s.sf_api_version)
            providers = []
            if s.clay_api_key:
                providers.append(ClayClient(s.clay_api_key, webhook_url=s.clay_webhook_url, timeout=s.clay_timeout_s))
            if s.harmonic_api_key:
                providers.append(HarmonicClient(s.harmonic_api_key, s.harmonic_base_url, s.harmonic_timeout_s))
            slack_client = SlackClient(s.slack_bot_token or "", s.slack_signing_secret)

        enricher = Enricher(providers, timeout_s=max(s.clay_timeout_s, s.harmonic_timeout_s) + 2)
        slack = SlackHandoff(slack_client, s.slack_handoff_channel, s.slack_escalation_channel)
        return cls(settings=s, store=store, hubspot=hubspot, salesforce=salesforce, enricher=enricher,
                   scorer=scorer, router=router, roster=roster, slack=slack, sla=sla)

    # --- intake ------------------------------------------------------------

    async def ingest_hubspot_contact(self, contact_id: str, event_key: str | None = None) -> LeadRecord | None:
        """Entry point for the HubSpot webhook. Idempotent on event_key (or contact id + day)."""
        key = event_key or f"hs:{contact_id}:{datetime.now(timezone.utc):%Y-%m-%d}"
        lead_id = str(uuid.uuid4())
        if not self.store.claim(key, lead_id):
            log.info("duplicate webhook for contact %s (key=%s); skipping", contact_id, key)
            return None
        lead = await self.hubspot.fetch_lead(contact_id)
        rec = LeadRecord(id=lead_id, lead=lead)
        rec.transition(LeadStage.RECEIVED, f"hubspot contact {contact_id}")
        self.store.save(rec)
        return await self.process(rec)

    async def ingest_lead(self, lead: Lead) -> LeadRecord:
        """Direct intake (CLI / tests / other sources)."""
        rec = LeadRecord(id=str(uuid.uuid4()), lead=lead)
        rec.transition(LeadStage.RECEIVED, "direct")
        self.store.save(rec)
        return await self.process(rec)

    # --- core --------------------------------------------------------------

    async def process(self, rec: LeadRecord) -> LeadRecord:
        t0 = time.perf_counter()
        try:
            if rec.stage == LeadStage.RECEIVED:
                await self._enrich(rec)
            if rec.stage == LeadStage.ENRICHED:
                self._score(rec)
            if rec.stage == LeadStage.SCORED:
                await self._route(rec)
            if rec.stage == LeadStage.ROUTED:
                await self._handoff(rec)
            self.metrics["processed"] += 1
        except Exception as exc:  # noqa: BLE001
            log.exception("pipeline failed for lead %s", rec.id)
            rec.errors.append(f"{rec.stage.value}: {exc!s}")
            rec.transition(LeadStage.FAILED, str(exc))
            self.metrics["errors"] += 1
        finally:
            self.store.save(rec)
            self.metrics["stage_ms"]["total"] = round((time.perf_counter() - t0) * 1000, 1)
        return rec

    async def resume(self, lead_id: str) -> LeadRecord | None:
        rec = self.store.get(lead_id)
        if not rec:
            return None
        if rec.stage == LeadStage.FAILED:
            # roll back to the last successful stage
            last_ok = next((h["stage"] for h in reversed(rec.history) if h["stage"] not in ("failed",)), "received")
            rec.stage = LeadStage(last_ok)
        return await self.process(rec)

    async def _enrich(self, rec: LeadRecord) -> None:
        t = time.perf_counter()
        enrichment, errors = await self.enricher.enrich(rec.lead)
        rec.enrichment = enrichment
        rec.errors.extend(errors)
        rec.transition(LeadStage.ENRICHED, f"providers={enrichment.providers_used} errors={len(errors)}")
        self.store.save(rec)
        self.metrics["stage_ms"]["enrich"] = round((time.perf_counter() - t) * 1000, 1)

    def _score(self, rec: LeadRecord) -> None:
        rec.score = self.scorer.score(rec.lead, rec.enrichment)
        rec.transition(LeadStage.SCORED, f"tier={rec.score.tier.value} score={rec.score.total}")
        self.store.save(rec)

    async def _route(self, rec: LeadRecord) -> None:
        assert rec.score
        decision = self.router.route(rec.lead, rec.enrichment, rec.score)
        rec.routing = decision
        if rec.score.tier == Tier.D:
            self.metrics["disqualified"] += 1
            rec.transition(LeadStage.DISQUALIFIED, decision.reason)
            await self._writeback_hubspot(rec)
            return
        if decision.sdr is None:
            self.metrics["unrouted"] += 1
            rec.transition(LeadStage.FAILED, f"unrouted: {decision.reason}")
            try:
                await self.slack.client.post_message(self.slack.escalation_channel,
                                                     f":warning: Unrouted {rec.score.tier.value}-tier lead {rec.lead.full_name} ({rec.lead.inferred_domain()}): {decision.reason}")
            except Exception as exc:  # noqa: BLE001
                rec.errors.append(f"slack: {exc!s}")
            return
        rec.transition(LeadStage.ROUTED, decision.reason)
        self.store.save(rec)

    async def _handoff(self, rec: LeadRecord) -> None:
        t = time.perf_counter()
        self.sla.start(rec)
        # Salesforce first so the Slack card can deep-link to it. Non-fatal if SF is slow.
        try:
            rec.salesforce_lead_id = await asyncio.wait_for(self.salesforce.upsert_lead(rec), timeout=10)
        except Exception as exc:  # noqa: BLE001
            rec.errors.append(f"salesforce: {exc!s}")
        channel, ts = await self.slack.send(rec, self.sla.minutes_for(rec.score.tier))
        rec.slack_channel, rec.slack_message_ts = channel, ts
        rec.transition(LeadStage.HANDED_OFF, f"-> {rec.routing.sdr.name}; due {rec.sla.due_at.isoformat()}")
        self.store.save(rec)
        await self._writeback_hubspot(rec)
        self.metrics["stage_ms"]["handoff"] = round((time.perf_counter() - t) * 1000, 1)

    async def _writeback_hubspot(self, rec: LeadRecord) -> None:
        props = {
            "relay_score": rec.score.total if rec.score else None,
            "relay_tier": rec.score.tier.value if rec.score else None,
            "relay_owner": rec.routing.sdr.email if rec.routing and rec.routing.sdr else None,
            "relay_status": rec.stage.value,
            "hubspot_owner_id": None,
        }
        try:
            await self.hubspot.update_contact(rec.lead.hubspot_contact_id, {k: v for k, v in props.items() if v is not None})
        except Exception as exc:  # noqa: BLE001
            rec.errors.append(f"hubspot writeback: {exc!s}")

    # --- SDR actions -------------------------------------------------------

    async def accept(self, lead_id: str) -> LeadRecord | None:
        rec = self.store.get(lead_id)
        if not rec:
            return None
        self.sla.accept(rec)
        self.store.save(rec)
        await self.slack.mark(rec, f":white_check_mark: Accepted by {rec.routing.sdr.name}")
        return rec

    async def contacted(self, lead_id: str, at: datetime | None = None) -> LeadRecord | None:
        rec = self.store.get(lead_id)
        if not rec or not rec.sla:
            return None
        secs = self.sla.contacted(rec, at)
        self.store.save(rec)
        await self.slack.mark(rec, f":telephone_receiver: Contacted in {secs/60:.1f} min" + (" (SLA breached)" if rec.sla.breached else ""))
        if rec.salesforce_lead_id:
            try:
                await self.salesforce.update_lead(rec.salesforce_lead_id, {
                    "Relay_First_Response_At__c": rec.sla.contacted_at.isoformat(),
                    "Relay_SLA_Breached__c": rec.sla.breached,
                    "Status": "Working - Contacted",
                })
            except Exception as exc:  # noqa: BLE001
                rec.errors.append(f"salesforce: {exc!s}")
                self.store.save(rec)
        return rec

    async def reassign(self, lead_id: str, reason: str = "manual") -> LeadRecord | None:
        rec = self.store.get(lead_id)
        if not rec or not rec.routing or not rec.routing.sdr or not rec.score:
            return None
        old = rec.routing.sdr
        decision = self.router.route(rec.lead, rec.enrichment, rec.score, exclude={old.id})
        if not decision.sdr:
            rec.errors.append(f"reassign failed: {decision.reason}")
            self.store.save(rec)
            return rec
        rec.routing = decision
        rec.history.append({"stage": "reassigned", "at": datetime.now(timezone.utc).isoformat(), "note": f"sla breach / {reason}: {old.name} -> {decision.sdr.name}"})
        self.sla.start(rec)
        rec.sla.reassigned_at = rec.sla.handed_off_at
        rec.stage = LeadStage.HANDED_OFF
        channel, ts = await self.slack.reassigned(rec, old.name, self.sla.minutes_for(rec.score.tier))
        rec.slack_channel, rec.slack_message_ts = channel, ts
        if rec.salesforce_lead_id and decision.sdr.salesforce_user_id:
            try:
                await self.salesforce.update_lead(rec.salesforce_lead_id, {"OwnerId": decision.sdr.salesforce_user_id, "Relay_Routing_Reason__c": decision.reason[:255]})
            except Exception as exc:  # noqa: BLE001
                rec.errors.append(f"salesforce: {exc!s}")
        self.store.save(rec)
        return rec

    # --- SLA sweep ---------------------------------------------------------

    async def sweep_sla(self, now: datetime | None = None) -> dict[str, int]:
        now = now or datetime.now(timezone.utc)
        counts = {"escalated": 0, "reassigned": 0}
        for rec in self.store.overdue(now):
            action = self.sla.check(rec, now)
            if action == "escalate":
                rec.history.append({"stage": rec.stage.value, "at": now.isoformat(), "note": "sla breach: escalated"})
                await self.slack.escalate(rec, self.sla.minutes_overdue(rec, now))
                if rec.salesforce_lead_id:
                    try:
                        await self.salesforce.update_lead(rec.salesforce_lead_id, {"Relay_SLA_Breached__c": True})
                    except Exception as exc:  # noqa: BLE001
                        rec.errors.append(f"salesforce: {exc!s}")
                self.store.save(rec)
                counts["escalated"] += 1
            elif action == "reassign":
                rec.history.append({"stage": rec.stage.value, "at": now.isoformat(), "note": "sla breach: reassigning"})
                self.store.save(rec)
                await self.reassign(rec.id, reason="sla breach")
                counts["reassigned"] += 1
        return counts

    async def run_sla_loop(self, interval_s: int | None = None, stop: asyncio.Event | None = None) -> None:
        interval = interval_s or self.settings.sla_check_interval_s
        while not (stop and stop.is_set()):
            try:
                c = await self.sweep_sla()
                if c["escalated"] or c["reassigned"]:
                    log.info("sla sweep: %s", c)
            except Exception:  # noqa: BLE001
                log.exception("sla sweep failed")
            await asyncio.sleep(interval)

    # --- stats -------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        rt = self.store.response_times()
        by_stage: dict[str, int] = {}
        for rec in self.store.list(limit=10_000):
            by_stage[rec.stage.value] = by_stage.get(rec.stage.value, 0) + 1
        return {"response_time": self.sla.summarize(rt), "by_stage": by_stage, "open_by_sdr": self.store.open_leads_by_sdr(), **self.metrics}
