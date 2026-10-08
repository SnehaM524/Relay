"""PostHog: funnel query via the Query API (HogQL funnels) and experiment arms
via the flag's `$feature/<key>` property breakdown."""

from __future__ import annotations

import httpx

from ..models import ArmResult, Funnel, FunnelStep


class PostHogSource:
    def __init__(self, api_key: str, project_id: str, host: str = "https://us.posthog.com", timeout: float = 20.0):
        self.api_key, self.project_id, self.host, self.timeout = api_key, project_id, host.rstrip("/"), timeout

    def _query(self, query: dict) -> dict:
        with httpx.Client(timeout=self.timeout) as c:
            r = c.post(f"{self.host}/api/projects/{self.project_id}/query/", json={"query": query},
                       headers={"Authorization": f"Bearer {self.api_key}"})
            r.raise_for_status()
            return r.json()

    def funnel(self, events: list[str], name: str, product: str, days: int = 28, breakdown: str | None = None) -> Funnel | dict[str, Funnel]:
        q = {"kind": "FunnelsQuery", "dateRange": {"date_from": f"-{days}d"},
             "series": [{"kind": "EventsNode", "event": e} for e in events],
             "funnelsFilter": {"funnelWindowInterval": 14, "funnelWindowIntervalUnit": "day"}}
        if breakdown:
            q["breakdownFilter"] = {"breakdown": breakdown, "breakdown_type": "event"}
        res = self._query(q)["results"]
        if not breakdown:
            return Funnel(id=f"posthog-{'-'.join(events)}", name=name, product=product, window_days=days,
                          steps=[FunnelStep(name=s["name"], users=int(s["count"])) for s in res])
        out: dict[str, Funnel] = {}
        for series in res:
            label = str(series[0].get("breakdown_value", ["?"])[0])
            out[label] = Funnel(id=f"posthog-{label}", name=f"{name} [{label}]", product=product, window_days=days,
                                steps=[FunnelStep(name=s["name"], users=int(s["count"])) for s in series])
        return out

    def experiment_arms(self, events: list[str], flag_key: str, days: int = 28) -> tuple[ArmResult, ArmResult]:
        by = self.funnel(events, "exp", "", days, breakdown=f"$feature/{flag_key}")
        assert isinstance(by, dict)
        def arm(label: str, alt: str) -> ArmResult:
            f = by.get(label) or by.get(alt)
            if not f:
                return ArmResult(name=label, users=0, conversions=0)
            return ArmResult(name=label, users=f.steps[0].users, conversions=f.steps[-1].users)
        return arm("control", "false"), arm("treatment", "test")
