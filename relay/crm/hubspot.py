"""HubSpot: webhook intake + contact fetch + write-back.

Trigger: a HubSpot *workflow* (Contact enrolls when lifecycle stage becomes
"marketingqualifiedlead") with a "Send webhook" action pointed at
`POST /webhooks/hubspot`. Relay also accepts the native "contact.propertyChange"
subscription format from the HubSpot developer app. Both carry the contact id;
Relay then fetches the full contact so the webhook payload shape never matters.

Signature: HubSpot v3 — `X-HubSpot-Signature-v3` = base64(HMAC-SHA256(client_secret,
method + uri + body + timestamp)). Workflow webhooks use v1/v2 ("X-HubSpot-Signature"
= sha256(client_secret + body)). Both are checked.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from typing import Any

import httpx

from ..models import Lead

log = logging.getLogger(__name__)

CONTACT_PROPERTIES = [
    "email", "firstname", "lastname", "company", "website", "jobtitle", "country",
    "hs_lead_score", "lifecyclestage", "hs_analytics_source", "recent_conversion_event_name",
    "hs_latest_source", "hs_latest_source_data_1",
]

SOURCE_MAP = {
    "demo request": "demo_request", "request demo": "demo_request", "book a demo": "demo_request",
    "contact sales": "contact_sales", "talk to sales": "contact_sales",
    "pricing": "pricing_page", "free trial": "free_trial", "trial signup": "free_trial",
    "webinar": "webinar", "ebook": "content_download", "whitepaper": "content_download", "guide": "content_download",
}


def normalize_source(raw: str | None) -> str | None:
    if not raw:
        return None
    r = raw.lower()
    for k, v in SOURCE_MAP.items():
        if k in r:
            return v
    return r.replace(" ", "_")[:40]


def verify_hubspot_signature(client_secret: str | None, method: str, uri: str, body: bytes, headers: dict[str, str]) -> bool:
    if not client_secret:
        return True
    h = {k.lower(): v for k, v in headers.items()}
    v3 = h.get("x-hubspot-signature-v3")
    ts = h.get("x-hubspot-request-timestamp")
    if v3 and ts:
        if abs(time.time() * 1000 - int(ts)) > 5 * 60 * 1000:
            return False
        base = f"{method.upper()}{uri}{body.decode()}{ts}"
        digest = base64.b64encode(hmac.new(client_secret.encode(), base.encode(), hashlib.sha256).digest()).decode()
        return hmac.compare_digest(digest, v3)
    v1 = h.get("x-hubspot-signature")
    if v1:
        digest = hashlib.sha256((client_secret + body.decode()).encode()).hexdigest()
        return hmac.compare_digest(digest, v1)
    return False


def parse_hubspot_webhook(payload: Any) -> list[str]:
    """Return the contact ids referenced by any supported HubSpot payload shape."""
    ids: list[str] = []
    if isinstance(payload, list):  # developer-app subscription: [{objectId, propertyName, ...}]
        for ev in payload:
            if "objectId" in ev:
                ids.append(str(ev["objectId"]))
    elif isinstance(payload, dict):
        if "objectId" in payload:  # workflow webhook
            ids.append(str(payload["objectId"]))
        elif "vid" in payload:
            ids.append(str(payload["vid"]))
        elif "contactId" in payload:
            ids.append(str(payload["contactId"]))
        elif "properties" in payload and "hs_object_id" in payload["properties"]:
            ids.append(str(payload["properties"]["hs_object_id"]))
    return list(dict.fromkeys(ids))


def lead_from_contact(contact: dict[str, Any]) -> Lead:
    p = {k: (v.get("value") if isinstance(v, dict) else v) for k, v in (contact.get("properties") or {}).items()}
    cid = str(contact.get("id") or contact.get("vid") or p.get("hs_object_id"))
    website = p.get("website") or ""
    domain = website.replace("https://", "").replace("http://", "").split("/")[0].removeprefix("www.") or None
    source = normalize_source(p.get("recent_conversion_event_name") or p.get("hs_latest_source_data_1") or p.get("hs_analytics_source"))
    return Lead(
        hubspot_contact_id=cid,
        email=p["email"],
        first_name=p.get("firstname"),
        last_name=p.get("lastname"),
        company=p.get("company"),
        domain=domain,
        title=p.get("jobtitle"),
        country=_country_code(p.get("country")),
        source=source,
        hubspot_properties=p,
    )


_COUNTRY_CODES = {"united states": "US", "usa": "US", "united kingdom": "GB", "uk": "GB", "germany": "DE", "france": "FR",
                  "canada": "CA", "netherlands": "NL", "mexico": "MX", "brazil": "BR", "ireland": "IE", "sweden": "SE", "india": "IN"}


def _country_code(v: str | None) -> str | None:
    if not v:
        return None
    v = v.strip()
    if len(v) == 2:
        return v.upper()
    return _COUNTRY_CODES.get(v.lower(), v[:2].upper())


class HubSpotClient:
    def __init__(self, access_token: str, timeout: float = 8.0):
        self.token = access_token
        self.timeout = timeout
        self.base = "https://api.hubapi.com"

    async def get_contact(self, contact_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(f"{self.base}/crm/v3/objects/contacts/{contact_id}",
                                 params={"properties": ",".join(CONTACT_PROPERTIES)},
                                 headers={"Authorization": f"Bearer {self.token}"})
            r.raise_for_status()
            return r.json()

    async def fetch_lead(self, contact_id: str) -> Lead:
        return lead_from_contact(await self.get_contact(contact_id))

    async def update_contact(self, contact_id: str, properties: dict[str, Any]) -> None:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.patch(f"{self.base}/crm/v3/objects/contacts/{contact_id}",
                                   json={"properties": properties},
                                   headers={"Authorization": f"Bearer {self.token}"})
            r.raise_for_status()


class MockHubSpotClient:
    CONTACTS: dict[str, dict[str, Any]] = {
        "1001": {"id": "1001", "properties": {"email": "jane@acme.com", "firstname": "Jane", "lastname": "Rivera", "company": "Acme Corp",
                                             "website": "https://www.acme.com", "jobtitle": "VP of Sales", "country": "United States",
                                             "hs_lead_score": "88", "recent_conversion_event_name": "Request a demo"}},
        "1002": {"id": "1002", "properties": {"email": "omar@midco.io", "firstname": "Omar", "lastname": "Haddad", "company": "MidCo",
                                             "website": "midco.io", "jobtitle": "Director of RevOps", "country": "GB",
                                             "hs_lead_score": "61", "recent_conversion_event_name": "Pricing page"}},
        "1003": {"id": "1003", "properties": {"email": "lee@tinyshop.co", "firstname": "Lee", "lastname": "Park", "company": "TinyShop",
                                             "website": "tinyshop.co", "jobtitle": "Marketing Manager", "country": "US",
                                             "hs_lead_score": "35", "recent_conversion_event_name": "Ebook: GTM Playbook"}},
        "1004": {"id": "1004", "properties": {"email": "someone@gmail.com", "firstname": "Sam", "lastname": "Doe",
                                             "jobtitle": "Student", "hs_lead_score": "90", "recent_conversion_event_name": "Request a demo"}},
        "1005": {"id": "1005", "properties": {"email": "dev@solo.dev", "firstname": "Dev", "lastname": "Solo", "company": "Solo Dev",
                                             "website": "solo.dev", "jobtitle": "Engineer", "country": "US", "hs_lead_score": "10"}},
    }

    def __init__(self):
        self.updates: list[tuple[str, dict[str, Any]]] = []

    async def get_contact(self, contact_id: str) -> dict[str, Any]:
        if contact_id not in self.CONTACTS:
            raise KeyError(f"contact {contact_id} not found")
        return self.CONTACTS[contact_id]

    async def fetch_lead(self, contact_id: str) -> Lead:
        return lead_from_contact(await self.get_contact(contact_id))

    async def update_contact(self, contact_id: str, properties: dict[str, Any]) -> None:
        self.updates.append((contact_id, properties))
