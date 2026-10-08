"""Harmonic.ai enrichment — company funding, growth, and momentum signals.

Harmonic exposes a REST + GraphQL API. Relay uses the REST company lookup by
domain (`GET /companies?website_domain=...`) and reads:

  funding stage, total funding, last funding date, headcount growth (6mo),
  Harmonic's own momentum score if present.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from ..models import Enrichment, Lead
from .base import EnrichmentProvider

log = logging.getLogger(__name__)

STAGE_MAP = {
    "SEED": "Seed", "PRE_SEED": "Seed", "SERIES_A": "Series A", "SERIES_B": "Series B",
    "SERIES_C": "Series C", "SERIES_D": "Series D", "SERIES_E": "Series E", "SERIES_F": "Series E",
    "IPO": "Public", "PUBLIC": "Public", "PRIVATE_EQUITY": "Private Equity", "ACQUIRED": "Acquired",
}


def _parse_dt(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_harmonic_payload(payload: dict[str, Any]) -> Enrichment:
    funding = payload.get("funding") or {}
    traction = payload.get("traction_metrics") or {}
    headcount = traction.get("headcount") or {}
    growth = headcount.get("180d_ago", {}) or {}
    growth_pct = growth.get("percent_change")
    stage_raw = (funding.get("funding_stage") or payload.get("stage") or "").upper()
    return Enrichment(
        funding_stage=STAGE_MAP.get(stage_raw, stage_raw.title() if stage_raw else None),
        total_funding_usd=funding.get("funding_total"),
        last_funding_date=_parse_dt(funding.get("last_funding_at")),
        headcount_growth_6mo_pct=float(growth_pct) if growth_pct is not None else None,
        employee_count=headcount.get("latest_metric_value") or payload.get("headcount"),
        industry=(payload.get("tags_v2") or [{}])[0].get("display_value") if payload.get("tags_v2") else None,
        harmonic_score=payload.get("harmonic_score"),
        raw={"harmonic": payload},
        providers_used=["harmonic"],
    )


class HarmonicClient(EnrichmentProvider):
    name = "harmonic"

    def __init__(self, api_key: str, base_url: str = "https://api.harmonic.ai", timeout: float = 8.0):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def enrich(self, lead: Lead) -> Enrichment | None:
        domain = lead.inferred_domain()
        if not domain:
            return None
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(
                f"{self.base_url}/companies",
                params={"website_domain": domain},
                headers={"apikey": self.api_key},
            )
            if r.status_code == 404:
                return None
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                if not data:
                    return None
                data = data[0]
            return parse_harmonic_payload(data)


class MockHarmonicClient(EnrichmentProvider):
    name = "harmonic"

    FIXTURES: dict[str, dict[str, Any]] = {
        "acme.com": {
            "funding": {"funding_stage": "SERIES_C", "funding_total": 145_000_000, "last_funding_at": "2026-03-02T00:00:00Z"},
            "traction_metrics": {"headcount": {"latest_metric_value": 1250, "180d_ago": {"percent_change": 34.5}}},
            "harmonic_score": 0.91,
        },
        "midco.io": {
            "funding": {"funding_stage": "SERIES_A", "funding_total": 18_000_000, "last_funding_at": "2025-11-10T00:00:00Z"},
            "traction_metrics": {"headcount": {"latest_metric_value": 185, "180d_ago": {"percent_change": 12.0}}},
        },
        "tinyshop.co": {
            "funding": {"funding_stage": "SEED", "funding_total": 1_200_000},
            "traction_metrics": {"headcount": {"latest_metric_value": 12, "180d_ago": {"percent_change": 0.0}}},
        },
    }

    def __init__(self, fixtures: dict[str, dict[str, Any]] | None = None, fail: bool = False):
        self.fixtures = fixtures or self.FIXTURES
        self.fail = fail
        self.calls: list[str] = []

    async def enrich(self, lead: Lead) -> Enrichment | None:
        self.calls.append(lead.email)
        if self.fail:
            raise RuntimeError("harmonic unavailable")
        domain = lead.inferred_domain() or ""
        if domain in self.fixtures:
            return parse_harmonic_payload(self.fixtures[domain])
        return None
