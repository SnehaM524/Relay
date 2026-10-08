"""ICP scoring. Driven entirely by config/icp.yaml.

Rule types:
  buckets: [{min, points}]   -> first bucket whose min <= value wins (list sorted desc by min)
  match:   {value: points}   -> exact (case-insensitive) match, else `default`
  any_of:  {value: points}   -> sum of points for every list element present, capped at `cap`
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import Enrichment, Lead, Score, ScoreBreakdown, Tier

FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com",
    "proton.me", "protonmail.com", "live.com", "msn.com", "me.com", "mail.com", "ymail.com",
    "googlemail.com", "yandex.com", "gmx.com", "qq.com", "163.com",
}


def _resolve(path: str, lead: Lead, enrichment: Enrichment) -> Any:
    root, _, rest = path.partition(".")
    obj: Any = {"lead": lead, "enrichment": enrichment}[root]
    for part in rest.split(".") if rest else []:
        if obj is None:
            return None
        if isinstance(obj, dict):
            obj = obj.get(part)
        else:
            obj = getattr(obj, part, None)
    return obj


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class ICPScorer:
    def __init__(self, config: dict[str, Any]):
        self.cfg = config
        self.tiers: dict[str, float] = {k: float(v) for k, v in config.get("tiers", {}).items()}
        self.dq = config.get("disqualifiers", {}) or {}
        self.rules: list[dict[str, Any]] = config.get("rules", []) or []
        for r in self.rules:
            if "buckets" in r:
                r["buckets"] = sorted(r["buckets"], key=lambda b: b["min"], reverse=True)

    @classmethod
    def from_file(cls, path: str | Path) -> "ICPScorer":
        with open(path) as f:
            return cls(yaml.safe_load(f))

    # --- public ------------------------------------------------------------

    def score(self, lead: Lead, enrichment: Enrichment | None) -> Score:
        enrichment = enrichment or Enrichment()
        dq = self._disqualify(lead, enrichment)
        if dq:
            return Score(total=0.0, tier=Tier.D, breakdown=[], disqualify_reasons=dq)

        breakdown: list[ScoreBreakdown] = []
        for rule in self.rules:
            pts, reason = self._apply(rule, lead, enrichment)
            if pts != 0:
                breakdown.append(ScoreBreakdown(rule=rule["name"], points=pts, reason=reason))
        total = round(sum(b.points for b in breakdown), 2)
        return Score(total=total, tier=self._tier(total), breakdown=breakdown)

    # --- internals ---------------------------------------------------------

    def _tier(self, total: float) -> Tier:
        for t in ("A", "B", "C"):
            if t in self.tiers and total >= self.tiers[t]:
                return Tier(t)
        return Tier.D

    def _disqualify(self, lead: Lead, e: Enrichment) -> list[str]:
        reasons: list[str] = []
        domain = lead.inferred_domain() or ""
        if self.dq.get("free_email") and (e.is_free_email or domain in FREE_EMAIL_DOMAINS):
            reasons.append(f"free email domain: {domain}")
        if domain in {d.lower() for d in self.dq.get("blocked_domains", [])}:
            reasons.append(f"blocked domain: {domain}")
        country = (lead.country or e.hq_country or "").upper()
        if country and country in {c.upper() for c in self.dq.get("blocked_countries", [])}:
            reasons.append(f"blocked country: {country}")
        min_emp = self.dq.get("min_employee_count")
        if min_emp is not None and e.employee_count is not None and e.employee_count < min_emp:
            reasons.append(f"employee count {e.employee_count} < {min_emp}")
        title = (lead.title or "").lower()
        for kw in self.dq.get("title_keywords", []):
            if kw.lower() in title:
                reasons.append(f"title contains '{kw}'")
                break
        return reasons

    def _apply(self, rule: dict[str, Any], lead: Lead, e: Enrichment) -> tuple[float, str]:
        value = _resolve(rule["field"], lead, e)
        if "buckets" in rule:
            num = _to_float(value)
            if num is None:
                return 0.0, "missing"
            for b in rule["buckets"]:
                if num >= b["min"]:
                    return float(b["points"]), f"{rule['field']}={num:g} >= {b['min']}"
            return 0.0, f"{rule['field']}={num:g} below all buckets"
        if "match" in rule:
            if value is None:
                return float(rule.get("default", 0)), "missing"
            table = {str(k).lower(): float(v) for k, v in rule["match"].items()}
            key = str(value).lower()
            if key in table:
                return table[key], f"{rule['field']}={value}"
            return float(rule.get("default", 0)), f"{rule['field']}={value} (no match)"
        if "any_of" in rule:
            items = {str(v).lower() for v in (value or [])}
            table = {str(k).lower(): float(v) for k, v in rule["any_of"].items()}
            hits = [k for k in table if k in items]
            pts = sum(table[k] for k in hits)
            cap = rule.get("cap")
            if cap is not None:
                pts = min(pts, float(cap))
            return pts, f"matched {hits}" if hits else "no overlap"
        return 0.0, "unknown rule type"
