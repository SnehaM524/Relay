from relay.models import Enrichment, Lead, Tier
from relay.scoring import ICPScorer

from .conftest import ROOT

scorer = ICPScorer.from_file(ROOT / "config" / "icp.yaml")


def lead(**kw) -> Lead:
    base = dict(hubspot_contact_id="1", email="x@corp.com")
    base.update(kw)
    return Lead(**base)


def test_free_email_disqualifies():
    s = scorer.score(lead(email="a@gmail.com", source="demo_request"), Enrichment(employee_count=5000, seniority="C-Level"))
    assert s.tier == Tier.D
    assert any("free email" in r for r in s.disqualify_reasons)


def test_tiny_company_disqualifies():
    s = scorer.score(lead(), Enrichment(employee_count=2))
    assert s.tier == Tier.D


def test_student_title_disqualifies():
    s = scorer.score(lead(title="PhD Student"), Enrichment(employee_count=1000))
    assert s.tier == Tier.D


def test_blocked_country_disqualifies():
    s = scorer.score(lead(country="KP"), Enrichment(employee_count=1000))
    assert s.tier == Tier.D


def test_tier_a_enterprise_vp_demo():
    e = Enrichment(employee_count=800, industry="Software", funding_stage="Series B", seniority="VP",
                   department="Sales", tech_stack=["Salesforce"], headcount_growth_6mo_pct=20)
    s = scorer.score(lead(source="demo_request", hubspot_properties={"hs_lead_score": "85"}), e)
    assert s.tier == Tier.A
    assert s.total >= 70
    rules = {b.rule for b in s.breakdown}
    assert {"employee_count", "industry", "funding_stage", "seniority", "high_intent_source", "hubspot_lead_score"} <= rules


def test_tier_c_small_marketing_manager():
    e = Enrichment(employee_count=12, industry="E-commerce", seniority="Manager", department="Marketing")
    s = scorer.score(lead(source="content_download"), e)
    assert s.tier == Tier.C


def test_tech_stack_cap():
    e = Enrichment(employee_count=100, tech_stack=["Salesforce", "HubSpot", "Outreach", "Salesloft", "Gong", "Snowflake"])
    s = scorer.score(lead(), e)
    ts = next(b for b in s.breakdown if b.rule == "tech_stack")
    assert ts.points == 15  # capped


def test_missing_enrichment_still_scores():
    s = scorer.score(lead(source="demo_request", hubspot_properties={"hs_lead_score": "90"}), None)
    assert s.tier in (Tier.B, Tier.C)
    assert s.total == 30  # 20 source + 10 lead score


def test_bucket_negative_growth_penalizes():
    e = Enrichment(employee_count=100, headcount_growth_6mo_pct=-20)
    s = scorer.score(lead(), e)
    assert any(b.rule == "headcount_growth" and b.points < 0 for b in s.breakdown)
