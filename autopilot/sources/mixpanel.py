"""Mixpanel: pull a saved funnel's step counts, and per-variant conversion for a
running experiment (segmented by the flag property).

Uses the Query API (`/api/2.0/funnels`) with a service account.
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx

from ..models import ArmResult, Funnel, FunnelStep


class MixpanelSource:
    def __init__(self, project_id: str, service_account: str, secret: str, region: str = "us", timeout: float = 20.0):
        self.project_id = project_id
        self.auth = (service_account, secret)
        self.base = "https://mixpanel.com/api/2.0" if region == "us" else f"https://{region}.mixpanel.com/api/2.0"
        self.timeout = timeout

    def funnel(self, funnel_id: int, name: str, product: str, days: int = 28, step_names: list[str] | None = None) -> Funnel:
        to, frm = date.today(), date.today() - timedelta(days=days)
        with httpx.Client(timeout=self.timeout, auth=self.auth) as c:
            r = c.get(f"{self.base}/funnels", params={"project_id": self.project_id, "funnel_id": funnel_id,
                                                      "from_date": frm.isoformat(), "to_date": to.isoformat(), "unit": "day"})
            r.raise_for_status()
            data = r.json()["data"]
        # sum step counts across days
        totals: list[int] = []
        labels: list[str] = []
        for _day, payload in data.items():
            for i, step in enumerate(payload["steps"]):
                if i >= len(totals):
                    totals.append(0); labels.append(step.get("event", f"step_{i}"))
                totals[i] += int(step.get("count", 0))
        names = step_names or labels
        return Funnel(id=f"mixpanel-{funnel_id}", name=name, product=product, window_days=days,
                      steps=[FunnelStep(name=n, users=u) for n, u in zip(names, totals)])

    def experiment_arms(self, funnel_id: int, flag_property: str, days: int = 28) -> tuple[ArmResult, ArmResult]:
        to, frm = date.today(), date.today() - timedelta(days=days)
        with httpx.Client(timeout=self.timeout, auth=self.auth) as c:
            r = c.get(f"{self.base}/funnels", params={"project_id": self.project_id, "funnel_id": funnel_id,
                                                      "from_date": frm.isoformat(), "to_date": to.isoformat(),
                                                      "on": f'properties["{flag_property}"]'})
            r.raise_for_status()
            data = r.json()["data"]
        arms: dict[str, list[int]] = {}
        for _day, by_variant in data.items():
            for variant, payload in by_variant.items():
                steps = payload["steps"]
                a = arms.setdefault(variant, [0, 0])
                a[0] += int(steps[0]["count"]); a[1] += int(steps[-1]["count"])
        ctrl = arms.get("control", arms.get("false", [0, 0]))
        trt = arms.get("treatment", arms.get("true", [0, 0]))
        return ArmResult(name="control", users=ctrl[0], conversions=ctrl[1]), ArmResult(name="treatment", users=trt[0], conversions=trt[1])
