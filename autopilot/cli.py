"""CLI.

  autopilot analyze [--funnel ID]           leak analysis per funnel
  autopilot propose --funnel ID [-n 8]      ranked experiment proposals
  autopilot generate EXP_ID [--out ./out]   React variant + flag config to disk
  autopilot ship EXP_ID                     mark running (50/50)
  autopilot observe EXP_ID --control U,C --treatment U,C [--days D]
  autopilot readout EXP_ID                  print the readout
  autopilot summary                         program-level results
  autopilot simulate [--seed 1]             full offline run across all funnels
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .agent import Agent
from .models import ArmResult
from .settings import Settings


def _p(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _arm(name: str, spec: str) -> ArmResult:
    u, c = (int(x) for x in spec.split(","))
    return ArmResult(name=name, users=u, conversions=c)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="autopilot")
    ap.add_argument("--funnels", default=None, help="path to funnels.json / .csv")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyze"); a.add_argument("--funnel")
    p = sub.add_parser("propose"); p.add_argument("--funnel", required=True); p.add_argument("-n", type=int, default=8)
    g = sub.add_parser("generate"); g.add_argument("exp_id"); g.add_argument("--out", default=None)
    s = sub.add_parser("ship"); s.add_argument("exp_id")
    o = sub.add_parser("observe"); o.add_argument("exp_id"); o.add_argument("--control", required=True); o.add_argument("--treatment", required=True); o.add_argument("--days", type=int, default=None)
    r = sub.add_parser("readout"); r.add_argument("exp_id")
    sub.add_parser("summary")
    sim = sub.add_parser("simulate"); sim.add_argument("--seed", type=int, default=1); sim.add_argument("--per-funnel", type=int, default=11); sim.add_argument("--ship-top", type=int, default=7)
    args = ap.parse_args(argv)

    settings = Settings()
    logging.basicConfig(level="WARNING")
    agent = Agent(settings)
    agent.load(args.funnels)

    if args.cmd == "analyze":
        for f in agent.funnels.values():
            if args.funnel and f.id != args.funnel:
                continue
            an = agent.analyze(f.id)
            print(f"\n{f.name}: {an.overall_conversion:.1%} end-to-end, {an.total_lost:,} lost")
            for d in an.dropoffs:
                bar = "█" * int(d.share_of_total_loss * 40)
                print(f"  {d.from_step:>13} -> {d.to_step:<13} {d.step_rate:6.1%}  lost {d.users_lost:>7,}  {bar}")
            for n in an.benchmark_notes:
                print(f"  • {n}")
    elif args.cmd == "propose":
        for e in agent.propose(args.funnel, n=args.n):
            r = e.ranking
            print(f"#{r.rank:<2} {e.proposal.name:<48} {e.proposal.target_step:<13} lift≈{e.proposal.expected_lift:+.0%}  conf {e.proposal.confidence:.0%}  {e.proposal.effort}  +{r.expected_users_gained:,.0f} users/mo  score {r.priority_score:,.0f}")
            print(f"    {e.proposal.hypothesis}")
    elif args.cmd == "generate":
        out = Path(args.out or settings.output_dir)
        e = agent.generate(args.exp_id, write_to=out)
        print(f"wrote {len(e.artifact.files)} files to {out}/ (flag {e.artifact.flag_key})")
        for path in e.artifact.files:
            print(f"  {path}")
    elif args.cmd == "ship":
        e = agent.ship(args.exp_id); print(f"{e.id} running since {e.started_at:%Y-%m-%d}")
    elif args.cmd == "observe":
        from datetime import timedelta
        e = agent._get(args.exp_id)
        now = (e.started_at + timedelta(days=args.days)) if args.days is not None and e.started_at else None
        e = agent.observe(args.exp_id, _arm("control", args.control), _arm("treatment", args.treatment), now=now)
        _p(e.stats.model_dump())
        if e.readout_md:
            print("\n" + e.readout_md)
    elif args.cmd == "readout":
        print(agent._get(args.exp_id).readout_md or "(not finished yet)")
    elif args.cmd == "summary":
        _p(agent.summary().model_dump())
    elif args.cmd == "simulate":
        summ = agent.simulate(seed=args.seed, per_funnel=args.per_funnel, ship_top=args.ship_top, log_fn=print)
        print("\n== Program summary")
        print(f"  funnels {summ.funnels} · proposed {summ.proposed} · shipped {summ.shipped} · won {summ.won} · lost {summ.lost} · inconclusive {summ.inconclusive}")
        print(f"  combined lift on signup conversion from winners: {summ.combined_lift:+.1%}")
        for w in summ.winners:
            print(f"    ✓ {w['name']:<48} step {w['step_lift']:+.1%} → funnel {w['funnel_lift']:+.1%}")


if __name__ == "__main__":
    main()
