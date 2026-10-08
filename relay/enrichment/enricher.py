"""Runs all enrichment providers concurrently and merges results.

Merge policy: providers are ordered by priority (first wins for scalar
fields); list fields are unioned; `raw` is merged by provider name. A provider
failing or timing out never blocks the pipeline — the lead is scored with
whatever we have, and the failure is recorded on the lead.
"""

from __future__ import annotations

import asyncio
import logging

from ..models import Enrichment, Lead
from .base import EnrichmentProvider

log = logging.getLogger(__name__)


class Enricher:
    def __init__(self, providers: list[EnrichmentProvider], timeout_s: float = 10.0):
        self.providers = providers
        self.timeout_s = timeout_s

    async def enrich(self, lead: Lead) -> tuple[Enrichment, list[str]]:
        errors: list[str] = []

        async def run(p: EnrichmentProvider) -> Enrichment | None:
            try:
                return await asyncio.wait_for(p.enrich(lead), timeout=self.timeout_s)
            except asyncio.TimeoutError:
                errors.append(f"{p.name}: timeout after {self.timeout_s}s")
            except Exception as exc:  # noqa: BLE001 - never let a vendor break routing
                errors.append(f"{p.name}: {exc!s}")
            return None

        results = await asyncio.gather(*(run(p) for p in self.providers))
        merged = Enrichment()
        for part in results:
            if part is None:
                continue
            merged = merge(merged, part)
        return merged, errors


def merge(base: Enrichment, incoming: Enrichment) -> Enrichment:
    data = base.model_dump()
    for key, val in incoming.model_dump().items():
        if key == "raw":
            data["raw"] = {**data["raw"], **val}
        elif key == "providers_used":
            data["providers_used"] = list(dict.fromkeys(data["providers_used"] + val))
        elif key == "tech_stack":
            data["tech_stack"] = list(dict.fromkeys(data["tech_stack"] + val))
        elif key == "is_free_email":
            data["is_free_email"] = data["is_free_email"] or val
        elif data.get(key) in (None, "", []) and val not in (None, "", []):
            data[key] = val
    return Enrichment.model_validate(data)
