"""CLI: `relay serve`, `relay simulate`, `relay score`, `relay stats`."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
from datetime import datetime, timedelta, timezone

from .models import Lead
from .pipeline import Pipeline
from .settings import Settings


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


async def _simulate(pipe: Pipeline, n: int, seed: int) -> None:
    """Push n synthetic MQLs through the mock pipeline and simulate SDR response."""
    rng = random.Random(seed)
    domains = ["acme.com", "midco.io", "tinyshop.co", "solo.dev", "gmail.com", "newco.ai", "bigbank.com"]
    titles = ["VP Sales", "Director of RevOps", "Marketing Manager", "Student", "CRO", "SDR", "Head of Growth"]
    sources = ["demo_request", "pricing_page", "webinar", "content_download", "free_trial"]
    countries = ["US", "GB", "DE", "CA", "MX"]
    recs = []
    for i in range(n):
        d = rng.choice(domains)
        lead = Lead(hubspot_contact_id=str(5000 + i), email=f"lead{i}@{d}", first_name=f"Lead{i}", last_name="Sim",
                    company=d.split(".")[0].title(), domain=d, title=rng.choice(titles), country=rng.choice(countries),
                    source=rng.choice(sources), hubspot_properties={"hs_lead_score": str(rng.randint(0, 100))})
        recs.append(await pipe.ingest_lead(lead))
    # simulate SDR responses with a realistic distribution (most quick, a few slow)
    for rec in recs:
        if rec.sla:
            secs = rng.lognormvariate(5.2, 0.6)  # median ~3 min
            await pipe.contacted(rec.id, rec.sla.handed_off_at + timedelta(seconds=secs))
    _print(pipe.stats())


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="relay")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the webhook server")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8080)
    sim = sub.add_parser("simulate", help="run synthetic leads through the mock pipeline")
    sim.add_argument("-n", type=int, default=25)
    sim.add_argument("--seed", type=int, default=7)
    sc = sub.add_parser("score", help="score a single lead from JSON on stdin or args")
    sc.add_argument("--email", required=True)
    sc.add_argument("--title", default=None)
    sc.add_argument("--source", default=None)
    sc.add_argument("--country", default=None)
    sub.add_parser("stats", help="print response-time stats")
    sub.add_parser("sweep", help="run one SLA sweep")
    args = p.parse_args(argv)

    settings = Settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("relay.api:app", host=args.host, port=args.port, reload=settings.env == "dev")
        return

    pipe = Pipeline.build(settings)
    if args.cmd == "simulate":
        asyncio.run(_simulate(pipe, args.n, args.seed))
    elif args.cmd == "score":
        lead = Lead(hubspot_contact_id="cli", email=args.email, title=args.title, source=args.source, country=args.country)
        rec = asyncio.run(pipe.ingest_lead(lead))
        _print({"id": rec.id, "stage": rec.stage, "score": rec.score.model_dump() if rec.score else None,
                "routing": {"team": rec.routing.team, "sdr": rec.routing.sdr.name if rec.routing.sdr else None, "reason": rec.routing.reason} if rec.routing else None,
                "sla_due": rec.sla.due_at if rec.sla else None, "errors": rec.errors})
    elif args.cmd == "stats":
        _print(pipe.stats())
    elif args.cmd == "sweep":
        _print(asyncio.run(pipe.sweep_sla()))


if __name__ == "__main__":
    main()
