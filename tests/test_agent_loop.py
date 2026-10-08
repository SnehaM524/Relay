from datetime import datetime, timedelta, timezone

import pytest

from autopilot.models import ArmResult, ExperimentStatus


def test_full_loop_ship_observe_readout(agent):
    exps = agent.propose("selfserve-signup", n=2)
    e = agent.ship(exps[0].id, at=datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert e.status == ExperimentStatus.RUNNING and e.artifact is not None
    # day 3: strong signal but too early
    e = agent.observe(e.id, ArmResult(name="c", users=6000, conversions=2900), ArmResult(name="t", users=6000, conversions=3300),
                      now=e.started_at + timedelta(days=3))
    assert e.status == ExperimentStatus.RUNNING and e.stats.decision == "keep_running"
    # day 10: significant
    e = agent.observe(e.id, ArmResult(name="c", users=20000, conversions=9700), ArmResult(name="t", users=20000, conversions=10900),
                      now=e.started_at + timedelta(days=10))
    assert e.status == ExperimentStatus.WON
    assert e.readout_md and e.readout_md.startswith("# ") and "SHIP" in e.readout_md
    assert f"{e.stats.relative_lift:+.1%}" in e.readout_md
    assert e.ended_at is not None


def test_concurrency_limit(agent, settings):
    exps = agent.propose("selfserve-signup", n=settings.max_concurrent + 1)
    for e in exps[: settings.max_concurrent]:
        agent.ship(e.id)
    with pytest.raises(RuntimeError):
        agent.ship(exps[-1].id)


def test_kill_path_readout(agent):
    exps = agent.propose("trial-checkout", n=1)
    e = agent.ship(exps[0].id, at=datetime(2026, 6, 1, tzinfo=timezone.utc))
    e = agent.observe(e.id, ArmResult(name="c", users=15000, conversions=3300), ArmResult(name="t", users=15000, conversions=2900),
                      now=e.started_at + timedelta(days=12))
    assert e.status == ExperimentStatus.LOST
    assert "KILL" in e.readout_md and "Roll back" in e.readout_md


def test_summary_weights_funnels_by_volume(agent):
    summ = agent.simulate(seed=1, per_funnel=11, ship_top=7)
    assert summ.funnels == 2
    assert summ.proposed == 22 and summ.shipped == 14
    assert summ.won + summ.lost + summ.inconclusive == summ.shipped
    assert 0 < summ.combined_lift < 1.0
    assert all(w["funnel_lift"] <= w["step_lift"] + 1e-9 for w in summ.winners)


def test_simulation_is_deterministic(settings):
    from autopilot.agent import Agent

    a, b = Agent(settings), Agent(settings)
    a.load(); b.load()
    sa, sb = a.simulate(seed=5), b.simulate(seed=5)
    assert sa.model_dump() == sb.model_dump()
