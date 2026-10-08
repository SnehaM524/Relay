from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Enrichment, Lead


class EnrichmentProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def enrich(self, lead: Lead) -> Enrichment | None:
        """Return partial enrichment for the lead, or None if the provider had nothing."""
