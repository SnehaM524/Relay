"""Clay enrichment.

Clay is normally used as a *push* system: you POST a record to a Clay table
webhook, Clay runs its waterfall (Apollo, Clearbit, LinkedIn, ...) and calls
your webhook back. Relay supports both:

  1. Synchronous pull via a Clay HTTP-API enrichment endpoint (if you have one
     exposed, e.g. a Clay "HTTP API" column returning JSON).
  2. Push: `ClayClient.push()` fires the record to the table webhook, and the
     `/webhooks/clay` endpoint in `relay.api` accepts the callback and resumes
     the pipeline.

The person-level fields Relay reads back from Clay:
  seniority, department, linkedin_url, title
Company-level:
  employee_count, industry, tech_stack, hq_country, is_free_email
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from ..models import Enrichment, Lead
from .base import EnrichmentProvider

log = logging.getLogger(__name__)

SENIORITY_MAP = {
    "c_suite": "C-Level", "cxo": "C-Level", "founder": "C-Level", "owner": "C-Level",
    "vp": "VP", "vice president": "VP",
    "director": "Director", "head": "Head",
    "manager": "Manager", "senior": "Senior IC", "entry": "IC", "ic": "IC",
}


def _norm_seniority(raw: str | None) -> str | None:
    if not raw:
        return None
    k = raw.strip().lower()
    return SENIORITY_MAP.get(k, raw.title())


def parse_clay_payload(payload: dict[str, Any]) -> Enrichment:
    """Map a Clay row (callback or API response) into Relay's Enrichment model."""
    person = payload.get("person") or payload
    company = payload.get("company") or payload
    tech = company.get("tech_stack") or company.get("technologies") or []
    if isinstance(tech, str):
        tech = [t.strip() for t in tech.split(",") if t.strip()]
    emp = company.get("employee_count") or company.get("employees") or company.get("headcount")
    try:
        emp = int(emp) if emp is not None else None
    except (TypeError, ValueError):
        emp = None
    return Enrichment(
        employee_count=emp,
        industry=company.get("industry"),
        tech_stack=list(tech),
        hq_country=company.get("country") or company.get("hq_country"),
        seniority=_norm_seniority(person.get("seniority")),
        department=person.get("department") or person.get("function"),
        linkedin_url=person.get("linkedin_url") or person.get("linkedin"),
        is_free_email=bool(person.get("is_free_email", False)),
        raw={"clay": payload},
        providers_used=["clay"],
    )


class ClayClient(EnrichmentProvider):
    name = "clay"

    def __init__(self, api_key: str, enrich_url: str | None = None, webhook_url: str | None = None, timeout: float = 8.0):
        self.api_key = api_key
        self.enrich_url = enrich_url
        self.webhook_url = webhook_url
        self.timeout = timeout

    async def enrich(self, lead: Lead) -> Enrichment | None:
        if not self.enrich_url:
            # No sync endpoint; push and let the callback resume the pipeline.
            await self.push(lead)
            return None
        body = {"email": lead.email, "domain": lead.inferred_domain(), "name": lead.full_name, "title": lead.title}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(self.enrich_url, json=body, headers={"Authorization": f"Bearer {self.api_key}"})
            r.raise_for_status()
            return parse_clay_payload(r.json())

    async def push(self, lead: Lead, callback_url: str | None = None) -> None:
        if not self.webhook_url:
            log.warning("Clay webhook_url not configured; skipping push for %s", lead.email)
            return
        body = {
            "relay_contact_id": lead.hubspot_contact_id,
            "email": lead.email,
            "domain": lead.inferred_domain(),
            "first_name": lead.first_name,
            "last_name": lead.last_name,
            "company": lead.company,
            "title": lead.title,
            "callback_url": callback_url,
            "sent_at": datetime.utcnow().isoformat(),
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(self.webhook_url, json=body, headers={"x-clay-webhook-auth": self.api_key})
            r.raise_for_status()


class MockClayClient(EnrichmentProvider):
    """Deterministic fake used in tests and `RELAY_MOCK_INTEGRATIONS=true`."""

    name = "clay"

    FIXTURES: dict[str, dict[str, Any]] = {
        "acme.com": {
            "person": {"seniority": "vp", "department": "Sales", "linkedin_url": "https://linkedin.com/in/jane-acme"},
            "company": {"employee_count": 1200, "industry": "Software", "country": "US",
                        "tech_stack": ["Salesforce", "Outreach", "Gong", "Snowflake"]},
        },
        "midco.io": {
            "person": {"seniority": "director", "department": "Revenue Operations"},
            "company": {"employee_count": 180, "industry": "SaaS", "country": "GB", "tech_stack": ["HubSpot", "Salesloft"]},
        },
        "tinyshop.co": {
            "person": {"seniority": "manager", "department": "Marketing"},
            "company": {"employee_count": 12, "industry": "E-commerce", "country": "US", "tech_stack": ["HubSpot"]},
        },
        "solo.dev": {
            "person": {"seniority": "ic", "department": "Engineering"},
            "company": {"employee_count": 2, "industry": "Software", "country": "US", "tech_stack": []},
        },
    }

    def __init__(self, fixtures: dict[str, dict[str, Any]] | None = None, fail: bool = False):
        self.fixtures = fixtures or self.FIXTURES
        self.fail = fail
        self.calls: list[str] = []

    async def enrich(self, lead: Lead) -> Enrichment | None:
        self.calls.append(lead.email)
        if self.fail:
            raise RuntimeError("clay unavailable")
        domain = lead.inferred_domain() or ""
        if domain in self.fixtures:
            return parse_clay_payload(self.fixtures[domain])
        return Enrichment(is_free_email=domain in {"gmail.com", "yahoo.com"}, providers_used=["clay"])
