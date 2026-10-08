import asyncio
import json
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from relay.api import create_app
from relay.crm.hubspot import parse_hubspot_webhook
from relay.pipeline import Pipeline


@pytest.fixture
def client(settings):
    pipe = Pipeline.build(settings)
    app = create_app(pipe, settings, run_sla_loop=False)
    with TestClient(app) as c:
        yield c, pipe


def test_parse_hubspot_shapes():
    assert parse_hubspot_webhook({"objectId": 123}) == ["123"]
    assert parse_hubspot_webhook([{"objectId": 1, "eventId": 9}, {"objectId": 2}]) == ["1", "2"]
    assert parse_hubspot_webhook({"vid": 7}) == ["7"]


def test_hubspot_webhook_routes_lead(client):
    c, pipe = client
    r = c.post("/webhooks/hubspot", json=[{"objectId": 1001, "eventId": 555, "propertyName": "lifecyclestage"}])
    assert r.status_code == 202
    recs = c.get("/leads", params={"stage": "handed_off"}).json()
    assert len(recs) == 1 and recs[0]["score"]["tier"] == "A"


def test_slack_interactive_contacted(client):
    c, pipe = client
    c.post("/webhooks/hubspot", json={"objectId": 1002})
    rec = c.get("/leads", params={"stage": "handed_off"}).json()[0]
    payload = {"type": "block_actions", "actions": [{"action_id": "lead_contacted", "value": rec["id"]}]}
    r = c.post("/webhooks/slack/interactive", content=urlencode({"payload": json.dumps(payload)}),
               headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.json()["stage"] == "contacted"
    assert c.get("/stats").json()["response_time"]["count"] == 1


def test_clay_callback_resumes_pipeline(client, settings):
    c, pipe = client
    # park a lead at RECEIVED (push-model enrichment: no sync providers)
    from relay.enrichment import Enricher
    from relay.models import Lead, LeadRecord, LeadStage

    rec = LeadRecord(id="L1", lead=Lead(hubspot_contact_id="4242", email="cfo@newco.ai", country="US", source="demo_request"))
    rec.transition(LeadStage.RECEIVED)
    pipe.store.save(rec)
    r = c.post("/webhooks/clay", json={"relay_contact_id": "4242",
                                       "person": {"seniority": "c_suite", "department": "Revenue Operations"},
                                       "company": {"employee_count": 600, "industry": "Fintech", "country": "US", "tech_stack": ["Salesforce"]}})
    assert r.status_code == 202
    out = c.get("/leads/L1").json()
    assert out["stage"] == "handed_off"
    assert out["score"]["tier"] == "A"
    assert out["routing"]["team"] == "enterprise"


def test_healthz(client):
    c, _ = client
    assert c.get("/healthz").json()["ok"] is True
