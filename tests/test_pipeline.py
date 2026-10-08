from datetime import timedelta

import pytest

from relay.enrichment import Enricher, MockClayClient, MockHarmonicClient
from relay.models import Lead, LeadStage, Tier


async def test_end_to_end_tier_a(pipeline):
    rec = await pipeline.ingest_hubspot_contact("1001")
    assert rec.stage == LeadStage.HANDED_OFF
    assert rec.score.tier == Tier.A
    assert rec.enrichment.providers_used == ["clay", "harmonic"]
    assert rec.enrichment.funding_stage == "Series C"
    assert rec.routing.sdr.id == "sdr-ent-1"
    assert rec.salesforce_lead_id and rec.salesforce_lead_id.startswith("00Q")
    assert rec.sla.due_at - rec.sla.handed_off_at == timedelta(minutes=5)
    # Slack: channel post + DM
    slack = pipeline.slack.client
    assert len(slack.messages) == 1 and len(slack.dms) == 1
    assert slack.dms[0]["user"] == "U01ENT001"
    assert any(b.get("type") == "actions" for b in slack.messages[0]["blocks"])
    # Salesforce fields
    sf = pipeline.salesforce.leads[rec.salesforce_lead_id]
    assert sf["Relay_Tier__c"] == "A" and sf["OwnerId"] == "005ENT00000001"
    # HubSpot writeback
    assert pipeline.hubspot.updates[-1][1]["relay_tier"] == "A"


async def test_disqualified_free_email(pipeline):
    rec = await pipeline.ingest_hubspot_contact("1004")
    assert rec.stage == LeadStage.DISQUALIFIED
    assert rec.routing.sdr is None
    assert pipeline.slack.client.messages == []
    assert pipeline.hubspot.updates[-1][1]["relay_status"] == "disqualified"


async def test_duplicate_webhook_is_idempotent(pipeline):
    a = await pipeline.ingest_hubspot_contact("1002", event_key="evt-1")
    b = await pipeline.ingest_hubspot_contact("1002", event_key="evt-1")
    assert a and b is None
    assert len(pipeline.slack.client.messages) == 1


async def test_enrichment_failure_does_not_block(settings):
    from relay.pipeline import Pipeline

    pipe = Pipeline.build(settings)
    pipe.enricher = Enricher([MockClayClient(fail=True), MockHarmonicClient()], timeout_s=2)
    rec = await pipe.ingest_hubspot_contact("1001")
    assert rec.stage == LeadStage.HANDED_OFF
    assert any("clay" in e for e in rec.errors)
    assert rec.enrichment.providers_used == ["harmonic"]


async def test_accept_and_contacted_records_response_time(pipeline):
    rec = await pipeline.ingest_hubspot_contact("1002")
    await pipeline.accept(rec.id)
    t = rec.sla.handed_off_at + timedelta(seconds=150)
    rec2 = await pipeline.contacted(rec.id, t)
    assert rec2.stage == LeadStage.CONTACTED
    assert rec2.sla.response_seconds == 150
    assert rec2.sla.breached is False
    stats = pipeline.stats()
    assert stats["response_time"]["median_s"] == 150
    sf = pipeline.salesforce.leads[rec2.salesforce_lead_id]
    assert sf["Status"] == "Working - Contacted"


async def test_sla_breach_escalates_then_reassigns(pipeline):
    rec = await pipeline.ingest_hubspot_contact("1002")  # GB midmarket -> sdr-mm-3
    first_sdr = rec.routing.sdr.id
    assert first_sdr == "sdr-mm-3"
    t0 = rec.sla.handed_off_at
    window = rec.sla.due_at - t0
    # before due: nothing
    assert await pipeline.sweep_sla(t0 + window - timedelta(minutes=1)) == {"escalated": 0, "reassigned": 0}
    # after due: escalate
    assert await pipeline.sweep_sla(t0 + window + timedelta(minutes=1)) == {"escalated": 1, "reassigned": 0}
    esc = [m for m in pipeline.slack.client.messages if m["channel"] == pipeline.slack.escalation_channel]
    assert esc and "SLA breach" in esc[0]["text"]
    # second window elapses: reassign
    assert await pipeline.sweep_sla(t0 + 2 * window + timedelta(minutes=2)) == {"escalated": 0, "reassigned": 1}
    rec2 = pipeline.store.get(rec.id)
    assert rec2.routing.sdr.id != first_sdr
    assert rec2.stage == LeadStage.HANDED_OFF
    assert rec2.sla.reassigned_at is not None
    assert pipeline.salesforce.leads[rec2.salesforce_lead_id]["OwnerId"] == rec2.routing.sdr.salesforce_user_id


async def test_late_contact_marks_breach(pipeline):
    rec = await pipeline.ingest_hubspot_contact("1001")
    rec2 = await pipeline.contacted(rec.id, rec.sla.handed_off_at + timedelta(minutes=9))
    assert rec2.sla.breached is True


async def test_resume_after_failure(pipeline):
    lead = Lead(hubspot_contact_id="9", email="z@midco.io", country="GB", source="pricing_page")
    # make slack blow up once
    orig = pipeline.slack.send

    async def boom(*a, **k):
        raise RuntimeError("slack down")

    pipeline.slack.send = boom
    rec = await pipeline.ingest_lead(lead)
    assert rec.stage == LeadStage.FAILED
    pipeline.slack.send = orig
    rec2 = await pipeline.resume(rec.id)
    assert rec2.stage == LeadStage.HANDED_OFF


async def test_median_under_four_minutes_simulation(pipeline):
    """Sanity check on the stats path with a realistic response distribution."""
    import random

    rng = random.Random(1)
    for i in range(30):
        rec = await pipeline.ingest_lead(Lead(hubspot_contact_id=str(i), email=f"p{i}@midco.io", country="US", source="demo_request"))
        if rec.sla:
            await pipeline.contacted(rec.id, rec.sla.handed_off_at + timedelta(seconds=rng.lognormvariate(5.0, 0.5)))
    s = pipeline.stats()["response_time"]
    assert s["count"] == 30
    assert s["median_s"] < 240
