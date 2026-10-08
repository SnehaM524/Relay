"""Salesforce: upsert Lead with owner, score, tier, enrichment, and SLA fields.

Auth: OAuth2 username-password flow (simple, works for integration users). Swap
`_token()` for JWT bearer if your org requires it.

Custom fields Relay expects on Lead (create once via Setup):
  Relay_Score__c (Number), Relay_Tier__c (Picklist A/B/C/D),
  Relay_Routing_Reason__c (Text 255), Relay_Handoff_At__c (DateTime),
  Relay_SLA_Due_At__c (DateTime), Relay_First_Response_At__c (DateTime),
  Relay_SLA_Breached__c (Checkbox), Relay_Lead_Id__c (Text, External ID),
  HubSpot_Contact_Id__c (Text, External ID)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from ..models import LeadRecord

log = logging.getLogger(__name__)


def _dt(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def lead_to_sf_fields(rec: LeadRecord) -> dict[str, Any]:
    lead, e, s, r = rec.lead, rec.enrichment, rec.score, rec.routing
    fields: dict[str, Any] = {
        "Email": lead.email,
        "FirstName": lead.first_name,
        "LastName": lead.last_name or lead.email.split("@")[0],
        "Company": lead.company or lead.inferred_domain() or "Unknown",
        "Title": lead.title,
        "Website": lead.inferred_domain(),
        "LeadSource": lead.source,
        "Country": lead.country,
        "HubSpot_Contact_Id__c": lead.hubspot_contact_id,
        "Relay_Lead_Id__c": rec.id,
    }
    if e:
        fields.update({
            "NumberOfEmployees": e.employee_count,
            "Industry": e.industry,
            "Description": " | ".join(p for p in [
                f"Funding: {e.funding_stage}" if e.funding_stage else None,
                f"Headcount growth 6mo: {e.headcount_growth_6mo_pct:+.0f}%" if e.headcount_growth_6mo_pct is not None else None,
                f"Stack: {', '.join(e.tech_stack)}" if e.tech_stack else None,
                f"Enriched by: {', '.join(e.providers_used)}" if e.providers_used else None,
            ] if p) or None,
        })
    if s:
        fields.update({"Relay_Score__c": s.total, "Relay_Tier__c": s.tier.value})
    if r:
        fields["Relay_Routing_Reason__c"] = r.reason[:255]
        if r.sdr and r.sdr.salesforce_user_id:
            fields["OwnerId"] = r.sdr.salesforce_user_id
    if rec.sla:
        fields.update({
            "Relay_Handoff_At__c": _dt(rec.sla.handed_off_at),
            "Relay_SLA_Due_At__c": _dt(rec.sla.due_at),
            "Relay_First_Response_At__c": _dt(rec.sla.contacted_at),
            "Relay_SLA_Breached__c": rec.sla.breached,
        })
    return {k: v for k, v in fields.items() if v is not None}


class SalesforceClient:
    def __init__(self, instance_url: str, client_id: str, client_secret: str, username: str,
                 password: str, security_token: str = "", api_version: str = "v60.0", timeout: float = 10.0):
        self.instance_url = instance_url.rstrip("/")
        self.client_id, self.client_secret = client_id, client_secret
        self.username, self.password, self.security_token = username, password, security_token
        self.api_version = api_version
        self.timeout = timeout
        self._access_token: str | None = None

    async def _token(self) -> str:
        if self._access_token:
            return self._access_token
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(f"{self.instance_url}/services/oauth2/token", data={
                "grant_type": "password", "client_id": self.client_id, "client_secret": self.client_secret,
                "username": self.username, "password": self.password + self.security_token,
            })
            r.raise_for_status()
            data = r.json()
            self._access_token = data["access_token"]
            self.instance_url = data.get("instance_url", self.instance_url)
            return self._access_token

    async def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        token = await self._token()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.request(method, f"{self.instance_url}/services/data/{self.api_version}{path}",
                                     headers={"Authorization": f"Bearer {token}"}, **kw)
            if r.status_code == 401:  # token expired; refresh once
                self._access_token = None
                token = await self._token()
                r = await client.request(method, f"{self.instance_url}/services/data/{self.api_version}{path}",
                                         headers={"Authorization": f"Bearer {token}"}, **kw)
            r.raise_for_status()
            return r

    async def upsert_lead(self, rec: LeadRecord) -> str:
        """Upsert on Relay_Lead_Id__c external id; returns Salesforce Lead Id."""
        fields = lead_to_sf_fields(rec)
        ext = fields.pop("Relay_Lead_Id__c")
        r = await self._request("PATCH", f"/sobjects/Lead/Relay_Lead_Id__c/{ext}", json=fields)
        if r.status_code == 204:  # updated existing; fetch id
            q = await self._request("GET", "/query", params={"q": f"SELECT Id FROM Lead WHERE Relay_Lead_Id__c='{ext}' LIMIT 1"})
            return q.json()["records"][0]["Id"]
        return r.json()["id"]

    async def update_lead(self, sf_id: str, fields: dict[str, Any]) -> None:
        await self._request("PATCH", f"/sobjects/Lead/{sf_id}", json=fields)

    async def list_sdr_users(self, profile_name: str = "SDR") -> list[dict[str, Any]]:
        q = f"SELECT Id, Name, Email, IsActive FROM User WHERE Profile.Name='{profile_name}' AND IsActive=true"
        r = await self._request("GET", "/query", params={"q": q})
        return r.json().get("records", [])


class MockSalesforceClient:
    def __init__(self):
        self.leads: dict[str, dict[str, Any]] = {}
        self._n = 0

    async def upsert_lead(self, rec: LeadRecord) -> str:
        fields = lead_to_sf_fields(rec)
        existing = next((k for k, v in self.leads.items() if v.get("Relay_Lead_Id__c") == rec.id), None)
        if existing:
            self.leads[existing].update(fields)
            return existing
        self._n += 1
        sf_id = f"00Q{self._n:012d}"
        self.leads[sf_id] = fields
        return sf_id

    async def update_lead(self, sf_id: str, fields: dict[str, Any]) -> None:
        self.leads.setdefault(sf_id, {}).update(fields)

    async def list_sdr_users(self, profile_name: str = "SDR") -> list[dict[str, Any]]:
        return []
