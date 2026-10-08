"""Routing: pick a team from config/routing.yaml rules, then pick an SDR.

Capacity-aware, territory-aware, weighted round robin, with fallback teams so a
lead always lands somewhere (or is flagged for manual triage if every team is
full).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .models import SDR, Enrichment, Lead, RoutingDecision, Score, Tier
from .store import LeadStore


class Roster:
    def __init__(self, sdrs: list[SDR]):
        self.sdrs = {s.id: s for s in sdrs}

    @classmethod
    def from_file(cls, path: str | Path) -> "Roster":
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls([SDR.model_validate(s) for s in data["sdrs"]])

    def get(self, sdr_id: str) -> SDR | None:
        return self.sdrs.get(sdr_id)

    def team(self, team: str) -> list[SDR]:
        return [s for s in self.sdrs.values() if s.team == team]

    def with_load(self, open_counts: dict[str, int]) -> "Roster":
        copies = []
        for s in self.sdrs.values():
            c = s.model_copy()
            c.open_leads = open_counts.get(s.id, 0)
            copies.append(c)
        return Roster(copies)


def _in_working_hours(sdr: SDR, now: datetime) -> bool:
    wh = sdr.working_hours
    if not wh:
        return True
    try:
        from zoneinfo import ZoneInfo

        local = now.astimezone(ZoneInfo(wh.get("tz", "UTC")))
    except Exception:  # noqa: BLE001
        local = now
    days = wh.get("days", [0, 1, 2, 3, 4])
    if local.weekday() not in days:
        return False
    start, end = wh.get("start", 0), wh.get("end", 24)
    return start <= local.hour < end


class Router:
    def __init__(self, config: dict[str, Any], roster: Roster, store: LeadStore):
        self.cfg = config
        self.teams: dict[str, dict[str, Any]] = config.get("teams", {})
        self.rules: list[dict[str, Any]] = config.get("rules", [])
        self.roster = roster
        self.store = store

    @classmethod
    def from_file(cls, path: str | Path, roster: Roster, store: LeadStore) -> "Router":
        with open(path) as f:
            return cls(yaml.safe_load(f), roster, store)

    # --- public ------------------------------------------------------------

    def route(self, lead: Lead, enrichment: Enrichment | None, score: Score, now: datetime | None = None,
              exclude: set[str] | None = None) -> RoutingDecision:
        now = now or datetime.now(timezone.utc)
        exclude = exclude or set()
        if score.tier == Tier.D:
            return RoutingDecision(sdr=None, team="none", strategy="disqualified", reason="; ".join(score.disqualify_reasons) or "below C threshold")

        rule = self._match_rule(lead, enrichment, score)
        if rule is None:
            return RoutingDecision(sdr=None, team="none", strategy="unrouted", reason="no routing rule matched", fallback=True)

        loaded = self.roster.with_load(self.store.open_leads_by_sdr())
        team = rule["team"]
        strategy = rule.get("strategy") or self.teams.get(team, {}).get("strategy", "round_robin")

        # Named SDR override
        if strategy == "named":
            sdr = loaded.get(rule["sdr"])
            if sdr and self._eligible(sdr, now, exclude):
                return RoutingDecision(sdr=sdr, team=sdr.team, strategy="named", reason=f"rule '{rule['name']}' -> {sdr.name}")
            strategy = self.teams.get(team, {}).get("strategy", "round_robin")

        visited: list[str] = []
        fallback = False
        while team and team not in visited:
            visited.append(team)
            team_strategy = strategy if not fallback else self.teams.get(team, {}).get("strategy", "round_robin")
            candidates = [s for s in loaded.team(team) if self._eligible(s, now, exclude)]
            sdr = self._pick(team, team_strategy, candidates, lead, enrichment)
            if sdr:
                why = f"rule '{rule['name']}' -> team {team} via {team_strategy}"
                if fallback:
                    why += f" (fallback from {visited[0]})"
                return RoutingDecision(sdr=sdr, team=team, strategy=team_strategy, reason=why, fallback=fallback)
            team = self.teams.get(team, {}).get("fallback_team")
            fallback = True

        return RoutingDecision(sdr=None, team=rule["team"], strategy=strategy, reason="all teams at capacity or offline", fallback=True)

    # --- internals ---------------------------------------------------------

    def _match_rule(self, lead: Lead, e: Enrichment | None, score: Score) -> dict[str, Any] | None:
        e = e or Enrichment()
        domain = lead.inferred_domain() or ""
        country = (lead.country or e.hq_country or "").upper()
        for rule in self.rules:
            m = rule.get("match", {}) or {}
            if "tier" in m and score.tier.value not in [str(t) for t in m["tier"]]:
                continue
            if "domains" in m and domain not in {d.lower() for d in m["domains"]}:
                continue
            if "countries" in m and country not in {c.upper() for c in m["countries"]}:
                continue
            if "source" in m and (lead.source or "") not in m["source"]:
                continue
            if "min_employees" in m and (e.employee_count or 0) < m["min_employees"]:
                continue
            if "max_employees" in m and (e.employee_count or 0) > m["max_employees"]:
                continue
            return rule
        return None

    @staticmethod
    def _eligible(sdr: SDR, now: datetime, exclude: set[str]) -> bool:
        return sdr.active and sdr.id not in exclude and sdr.open_leads < sdr.capacity and _in_working_hours(sdr, now)

    def _pick(self, team: str, strategy: str, candidates: list[SDR], lead: Lead, e: Enrichment | None) -> SDR | None:
        if not candidates:
            return None
        if strategy == "least_loaded":
            return min(candidates, key=lambda s: (s.open_leads / max(s.capacity, 1), s.id))
        if strategy == "territory":
            country = (lead.country or (e.hq_country if e else None) or "").upper()
            in_territory = [s for s in candidates if country and country in {t.upper() for t in s.territories}]
            pool = in_territory or candidates
            return self._round_robin(f"{team}:{country or 'any'}", pool)
        return self._round_robin(team, candidates)

    def _round_robin(self, key: str, candidates: list[SDR]) -> SDR:
        # Weighted: expand each SDR by weight (0.5 -> appears every other cycle)
        ordered = sorted(candidates, key=lambda s: s.id)
        expanded: list[SDR] = []
        for i in range(2):  # two-cycle window handles 0.5 weights cleanly
            for s in ordered:
                if s.weight >= 1.0 or (s.weight > 0 and i % max(1, round(1 / s.weight)) == 0):
                    expanded.append(s)
        cursor = self.store.next_cursor(key)
        return expanded[cursor % len(expanded)]
