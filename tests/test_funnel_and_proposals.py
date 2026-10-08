import json

from autopilot.funnel import analyze, step_type
from autopilot.generator import flag_config, pascal
from autopilot.llm import parse_json
from autopilot.models import ExperimentStatus


def test_analysis_finds_biggest_leak(agent):
    an = agent.analyze("selfserve-signup")
    assert an.biggest_leak.from_step == "landing"
    assert abs(sum(d.share_of_total_loss for d in an.dropoffs) - 1.0) < 0.01
    assert any("signup_form->email_verify" in n for n in an.benchmark_notes)


def test_step_type_mapping():
    assert step_type("signup_form") == "signup_form"
    assert step_type("Confirm email") == "email_verify"
    assert step_type("Payment details") == "checkout"
    assert step_type("hero") == "landing"


def test_proposals_are_ranked_and_persisted(agent):
    exps = agent.propose("selfserve-signup", n=8)
    assert len(exps) == 8
    ranks = [e.ranking.rank for e in exps]
    assert ranks == sorted(ranks)
    scores = [e.ranking.priority_score for e in exps]
    assert scores == sorted(scores, reverse=True)
    assert all(e.status == ExperimentStatus.PROPOSED for e in exps)
    assert len(agent.store.list("selfserve-signup")) == 8
    # a second round excludes what's already proposed
    more = agent.propose("selfserve-signup", n=8)
    assert not {e.proposal.key for e in more} & {e.proposal.key for e in exps}


def test_ranking_prefers_downstream_high_confidence(agent):
    exps = agent.propose("selfserve-signup", n=11)
    top = exps[0].proposal
    assert top.key in ("reduce-form-fields", "google-sso", "template-first-onboarding")
    assert top.target_step != "landing"  # landing ideas decay through 4 downstream steps
    # marginal decay: a landing-page lift should translate to less than its step lift at the funnel level
    landing = next(e for e in exps if e.proposal.target_step == "landing")
    assert landing.ranking.expected_funnel_lift < landing.proposal.expected_lift


def test_funnel_lift_model_accounts_for_decay(agent):
    from autopilot.proposer import funnel_lift_from_step_lift

    f = agent.funnels["selfserve-signup"]
    an = analyze(f)
    no_decay, _, _ = funnel_lift_from_step_lift(f, an, "landing", 0.10, marginal_decay=1.0)
    decay, _, _ = funnel_lift_from_step_lift(f, an, "landing", 0.10, marginal_decay=0.7)
    last, _, _ = funnel_lift_from_step_lift(f, an, "onboarding", 0.10, marginal_decay=0.7)
    assert abs(no_decay - 0.10) < 1e-3  # with no decay a step lift carries fully through (rates rounded to 4dp)
    assert decay < no_decay
    assert abs(last - 0.10) < 1e-3  # last transition has no downstream steps to decay through


def test_generate_writes_react_and_flag(agent, tmp_path):
    exps = agent.propose("selfserve-signup", n=3)
    e = agent.generate(exps[0].id, write_to=tmp_path)
    art = e.artifact
    assert art.component_name == pascal(exps[0].proposal.key)
    assert "export default function" in art.react_code
    assert "experiment_exposure" in art.react_code
    assert art.flag_key == f"exp-{exps[0].proposal.key}"
    assert (tmp_path / f"src/experiments/{art.component_name}/Variant.tsx").exists()
    assert (tmp_path / f"flags/{art.flag_key}.json").exists()
    assert json.loads((tmp_path / f"flags/{art.flag_key}.json").read_text())["key"] == art.flag_key
    assert e.status == ExperimentStatus.GENERATED


def test_flag_config_all_providers(agent):
    exps = agent.propose("trial-checkout", n=1)
    for prov in ("launchdarkly", "statsig", "growthbook", "posthog"):
        cfg = flag_config(prov, "exp-x", exps[0])
        assert cfg and json.dumps(cfg)


def test_parse_json_handles_fences():
    assert parse_json('```json\n[{"a": 1}]\n```') == [{"a": 1}]
    assert parse_json('Here you go: {"a": 1}') == {"a": 1}
