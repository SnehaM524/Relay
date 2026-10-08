from autopilot.models import ArmResult
from autopilot.stats import achieved_power, evaluate, required_sample_per_arm


def arm(name, users, conv):
    return ArmResult(name=name, users=users, conversions=conv)


def test_sample_size_is_sane():
    # 20% baseline, detect +10% relative (22%) at 80% power -> ~6k per arm
    n = required_sample_per_arm(0.20, 0.10)
    assert 5000 < n < 8000
    # smaller effect needs more sample
    assert required_sample_per_arm(0.20, 0.05) > n


def test_clear_winner_ships_after_min_days():
    r = evaluate(arm("c", 20000, 4000), arm("t", 20000, 4600), days_running=10)
    assert r.relative_lift > 0.14
    assert r.p_value < 0.001
    assert r.significant and r.decision == "ship"
    assert r.ci_low > 0


def test_not_before_min_days():
    r = evaluate(arm("c", 20000, 4000), arm("t", 20000, 4600), days_running=3, min_days=7)
    assert not r.significant and r.decision == "keep_running"


def test_sequential_boundary_is_stricter_early():
    # p ~ 0.02 with a small fraction of the required sample should NOT be significant early
    r = evaluate(arm("c", 1500, 300), arm("t", 1500, 355), days_running=8, mde_relative=0.05)
    assert r.p_value < 0.05
    assert r.alpha_threshold < 0.05
    assert not r.significant and r.decision == "keep_running"


def test_final_look_uses_full_alpha():
    r = evaluate(arm("c", 1500, 300), arm("t", 1500, 355), days_running=28, max_days=28, mde_relative=0.05)
    assert r.significant and r.decision == "ship"


def test_loser_is_killed():
    r = evaluate(arm("c", 20000, 4000), arm("t", 20000, 3500), days_running=10)
    assert r.decision == "kill" and r.relative_lift < 0


def test_null_result_inconclusive_at_max_days():
    r = evaluate(arm("c", 3000, 600), arm("t", 3000, 605), days_running=28, max_days=28)
    assert r.decision == "inconclusive"


def test_power_increases_with_n():
    assert achieved_power(0.2, 0.22, 2000) < achieved_power(0.2, 0.22, 10000)
