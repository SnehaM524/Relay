from relay.models import SDR, Enrichment, Lead, Score, Tier
from relay.routing import Roster, Router
from relay.store import LeadStore

from .conftest import ROOT


def make_router(open_counts: dict[str, int] | None = None, sdr_overrides: dict[str, dict] | None = None) -> Router:
    store = LeadStore(":memory:")
    roster = Roster.from_file(ROOT / "config" / "sdrs.yaml")
    if sdr_overrides:
        for sid, patch in sdr_overrides.items():
            roster.sdrs[sid] = roster.sdrs[sid].model_copy(update=patch)
    router = Router.from_file(ROOT / "config" / "routing.yaml", roster, store)
    if open_counts:
        router.store.open_leads_by_sdr = lambda: open_counts  # type: ignore[method-assign]
    return router


def lead(**kw) -> Lead:
    base = dict(hubspot_contact_id="1", email="x@corp.com", country="US")
    base.update(kw)
    return Lead(**base)


def score(tier: Tier, total: float = 80) -> Score:
    return Score(total=total, tier=tier)


def test_strategic_domain_goes_to_named_sdr():
    r = make_router()
    d = r.route(lead(email="a@acme.com"), Enrichment(employee_count=10), score(Tier.B))
    assert d.sdr.id == "sdr-ent-1"
    assert d.strategy == "named"


def test_tier_a_enterprise_least_loaded():
    r = make_router(open_counts={"sdr-ent-1": 20, "sdr-ent-2": 3})
    d = r.route(lead(), Enrichment(employee_count=900), score(Tier.A))
    assert d.team == "enterprise"
    assert d.sdr.id == "sdr-ent-2"


def test_tier_a_midmarket_territory_match():
    r = make_router()
    d = r.route(lead(country="GB"), Enrichment(employee_count=150), score(Tier.A))
    assert d.team == "midmarket"
    assert d.sdr.id == "sdr-mm-3"  # only MM rep covering GB


def test_territory_round_robin_rotates_within_territory():
    r = make_router()
    picks = [r.route(lead(country="US"), Enrichment(employee_count=100), score(Tier.B)).sdr.id for _ in range(4)]
    assert set(picks) == {"sdr-mm-1", "sdr-mm-2"}
    assert picks[0] != picks[1]


def test_smb_weighted_round_robin():
    r = make_router()
    picks = [r.route(lead(), Enrichment(employee_count=20), score(Tier.C)).sdr.id for _ in range(10)]
    counts = {k: picks.count(k) for k in set(picks)}
    assert counts["sdr-smb-3"] < counts["sdr-smb-1"]  # ramping rep (weight 0.5) gets fewer
    assert counts["sdr-smb-1"] == counts["sdr-smb-2"]


def test_capacity_falls_back_to_next_team():
    r = make_router(open_counts={"sdr-ent-1": 25, "sdr-ent-2": 25})
    d = r.route(lead(), Enrichment(employee_count=900), score(Tier.A))
    assert d.team == "midmarket"
    assert d.fallback is True


def test_everything_full_returns_unrouted():
    full = {s: 999 for s in ["sdr-ent-1", "sdr-ent-2", "sdr-mm-1", "sdr-mm-2", "sdr-mm-3", "sdr-smb-1", "sdr-smb-2", "sdr-smb-3"]}
    r = make_router(open_counts=full)
    d = r.route(lead(), Enrichment(employee_count=900), score(Tier.A))
    assert d.sdr is None and d.fallback


def test_disqualified_not_routed():
    r = make_router()
    d = r.route(lead(), Enrichment(), Score(total=0, tier=Tier.D, disqualify_reasons=["free email"]))
    assert d.sdr is None and d.strategy == "disqualified"


def test_inactive_sdr_skipped():
    r = make_router(sdr_overrides={"sdr-mm-3": {"active": False}})
    d = r.route(lead(country="GB"), Enrichment(employee_count=150), score(Tier.A))
    assert d.sdr.id != "sdr-mm-3"
    assert d.team == "midmarket"  # falls to any MM rep via round robin


def test_exclude_for_reassignment():
    r = make_router()
    d1 = r.route(lead(email="a@acme.com"), Enrichment(employee_count=10), score(Tier.B))
    d2 = r.route(lead(email="a@acme.com"), Enrichment(employee_count=10), score(Tier.B), exclude={d1.sdr.id})
    assert d2.sdr and d2.sdr.id != d1.sdr.id
