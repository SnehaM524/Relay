"""HTTP surface.

  POST /webhooks/hubspot            HubSpot MQL trigger (workflow webhook or subscription)
  POST /webhooks/clay               Clay table callback (push-model enrichment)
  POST /webhooks/slack/interactive  Slack button clicks (accept / contacted / reassign)
  POST /leads/{id}/contacted        Any system can mark first response (Outreach, SF, etc.)
  POST /leads/{id}/accept
  POST /leads/{id}/reassign
  GET  /leads/{id}
  GET  /leads?stage=handed_off
  GET  /stats
  GET  /healthz
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import parse_qs

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request

from .crm.hubspot import parse_hubspot_webhook, verify_hubspot_signature
from .enrichment.clay import parse_clay_payload
from .enrichment.enricher import merge
from .models import LeadStage
from .pipeline import Pipeline
from .settings import Settings

log = logging.getLogger(__name__)


def create_app(pipeline: Pipeline | None = None, settings: Settings | None = None, run_sla_loop: bool = True) -> FastAPI:
    settings = settings or Settings()
    pipe = pipeline or Pipeline.build(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = asyncio.Event()
        task = asyncio.create_task(pipe.run_sla_loop(stop=stop)) if run_sla_loop else None
        app.state.pipeline = pipe
        yield
        stop.set()
        if task:
            task.cancel()

    app = FastAPI(title="Relay", version="0.1.0", lifespan=lifespan)
    app.state.pipeline = pipe

    # --- webhooks ----------------------------------------------------------

    @app.post("/webhooks/hubspot", status_code=202)
    async def hubspot_webhook(request: Request, background: BackgroundTasks):
        body = await request.body()
        if not verify_hubspot_signature(settings.hubspot_webhook_secret, request.method, str(request.url), body, dict(request.headers)):
            raise HTTPException(401, "bad signature")
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            raise HTTPException(400, "invalid json")
        contact_ids = parse_hubspot_webhook(payload)
        if not contact_ids:
            raise HTTPException(400, "no contact id in payload")
        for cid in contact_ids:
            event_key = None
            if isinstance(payload, list):
                ev = next((e for e in payload if str(e.get("objectId")) == cid), {})
                if ev.get("eventId"):
                    event_key = f"hs-event:{ev['eventId']}"
            background.add_task(pipe.ingest_hubspot_contact, cid, event_key)
        return {"accepted": contact_ids}

    @app.post("/webhooks/clay", status_code=202)
    async def clay_webhook(request: Request, background: BackgroundTasks):
        if settings.clay_api_key and request.headers.get("x-clay-webhook-auth") != settings.clay_api_key:
            raise HTTPException(401, "bad clay auth")
        payload = await request.json()
        contact_id = str(payload.get("relay_contact_id") or payload.get("hubspot_contact_id") or "")
        rec = pipe.store.get_by_hubspot_id(contact_id) if contact_id else None
        if not rec:
            raise HTTPException(404, f"no lead for contact {contact_id}")
        rec.enrichment = merge(rec.enrichment or parse_clay_payload({}), parse_clay_payload(payload))
        if rec.stage in (LeadStage.RECEIVED, LeadStage.ENRICHED):
            rec.transition(LeadStage.ENRICHED, "clay callback")
            pipe.store.save(rec)
            background.add_task(pipe.process, rec)
        else:
            pipe.store.save(rec)
        return {"lead_id": rec.id, "stage": rec.stage.value}

    @app.post("/webhooks/slack/interactive")
    async def slack_interactive(request: Request):
        body = await request.body()
        ok = pipe.slack.client.verify_signature(body, request.headers.get("x-slack-request-timestamp", "0"), request.headers.get("x-slack-signature", ""))
        if not ok:
            raise HTTPException(401, "bad slack signature")
        form = parse_qs(body.decode())
        payload = json.loads(form.get("payload", ["{}"])[0])
        actions = payload.get("actions", [])
        if not actions:
            return {"ok": True}
        action = actions[0]
        lead_id = action.get("value")
        handler = {"lead_accept": pipe.accept, "lead_contacted": pipe.contacted, "lead_reassign": pipe.reassign}.get(action.get("action_id"))
        if handler is None or not lead_id:
            return {"ok": True}
        rec = await handler(lead_id)
        return {"ok": True, "stage": rec.stage.value if rec else None}

    # --- lead actions ------------------------------------------------------

    @app.post("/leads/{lead_id}/accept")
    async def accept(lead_id: str):
        rec = await pipe.accept(lead_id)
        if not rec:
            raise HTTPException(404)
        return rec

    @app.post("/leads/{lead_id}/contacted")
    async def contacted(lead_id: str, at: datetime | None = None):
        rec = await pipe.contacted(lead_id, at)
        if not rec:
            raise HTTPException(404)
        return rec

    @app.post("/leads/{lead_id}/reassign")
    async def reassign(lead_id: str, reason: str = "manual"):
        rec = await pipe.reassign(lead_id, reason)
        if not rec:
            raise HTTPException(404)
        return rec

    @app.post("/leads/{lead_id}/resume")
    async def resume(lead_id: str):
        rec = await pipe.resume(lead_id)
        if not rec:
            raise HTTPException(404)
        return rec

    @app.get("/leads/{lead_id}")
    async def get_lead(lead_id: str):
        rec = pipe.store.get(lead_id)
        if not rec:
            raise HTTPException(404)
        return rec

    @app.get("/leads")
    async def list_leads(stage: list[str] | None = Query(default=None), limit: int = 100):
        stages = [LeadStage(s) for s in stage] if stage else None
        return pipe.store.list(stages, limit)

    @app.post("/sla/sweep")
    async def sweep():
        return await pipe.sweep_sla()

    @app.get("/stats")
    async def stats():
        return pipe.stats()

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "mock": settings.mock_integrations}

    return app


app = create_app()
