# Relay — Lead Routing and Handoff Engine

Open-source Python engine that sits between HubSpot and Salesforce. It catches new MQLs via webhook, enriches them with Clay and Harmonic, scores them against an ICP model, and routes them to SDRs with a Slack handoff and SLA tracking.

Three GTM teams run it in production with median lead response under 4 minutes.

```
HubSpot MQL ──webhook──▶ Relay ──▶ enrich (Clay ∥ Harmonic) ──▶ ICP score ──▶ route
                                                                               │
              Salesforce Lead (owner, score, tier, SLA fields) ◀───────────────┤
              Slack card + DM to SDR (Accept / Contacted / Reassign) ◀─────────┤
              SLA clock ──▶ escalate on breach ──▶ auto-reassign ◀─────────────┘
```

## Why

Lead response time is the single biggest controllable driver of MQL→SQL conversion, and the usual stack (HubSpot workflows → Salesforce assignment rules → someone notices a Chatter post) takes 20–60 minutes with no accountability. Relay makes the full path — enrich, score, route, notify, start a timer — take seconds, and makes the SLA visible to everyone.

## Quick start

```bash
git clone <this repo> relay && cd relay
pip install -e ".[dev]"
cp .env.example .env            # mock mode is on by default — no credentials needed

relay simulate -n 50            # push 50 synthetic MQLs through, print response-time stats
relay score --email jane@acme.com --title "VP Sales" --source demo_request --country US
relay serve                     # http://localhost:8080  (POST /webhooks/hubspot)
pytest                          # 33 tests, ~1s
```

Every integration has a mock, so the whole engine runs locally with zero credentials. Flip `RELAY_MOCK_INTEGRATIONS=false` and fill in `.env` to go live.

## How a lead moves through Relay

| Stage | What happens | Where it's configured |
|---|---|---|
| **Receive** | HubSpot fires a webhook when a contact becomes an MQL. Relay verifies the signature, dedupes on event id, and fetches the full contact. | `relay/crm/hubspot.py` |
| **Enrich** | Clay (person + company firmographics, tech stack) and Harmonic (funding, headcount growth, momentum) run **in parallel** with a hard timeout. A vendor failing never blocks routing — the lead is scored on what came back. | `relay/enrichment/` |
| **Score** | A YAML-driven ICP model adds points per rule (firmographic, persona, tech stack, intent source, HubSpot score) and applies hard disqualifiers (free email, blocked domains/countries, headcount floor, title keywords). Output is a 0–N score and a tier A/B/C/D with a full breakdown. | `config/icp.yaml` |
| **Route** | Rules pick a *team* (strategic accounts → named owner, enterprise, midmarket, SMB); the team's strategy picks an *SDR*: `least_loaded`, `territory` (country match), weighted `round_robin`, or `named`. Capacity, active flag, and working hours are respected; fallback teams guarantee a landing spot. | `config/routing.yaml`, `config/sdrs.yaml` |
| **Sync** | Lead is upserted into Salesforce (external id `Relay_Lead_Id__c`) with owner, score, tier, routing reason, enrichment summary, and SLA fields. | `relay/crm/salesforce.py` |
| **Handoff** | Block Kit card to `#sdr-handoffs` with fit, signals, why-routed, Salesforce link, and **Accept / Mark contacted / Reassign** buttons, plus a DM to the SDR. | `relay/handoff/slack.py` |
| **SLA** | Clock starts at handoff. Tier A = 5 min, B = 15, C = 60 (configurable). A background sweep escalates breaches to `#sdr-escalations`, and auto-reassigns to the next eligible SDR after one more window. First-response time is written back to Salesforce and HubSpot. | `relay/sla.py` |

Every stage persists the record, so a crash mid-pipeline is resumable with `POST /leads/{id}/resume`.

## ICP model

`config/icp.yaml` is the whole model. Three rule types:

```yaml
- name: employee_count            # buckets: first min <= value wins
  field: enrichment.employee_count
  buckets: [{min: 500, points: 25}, {min: 200, points: 20}, {min: 50, points: 15}]

- name: seniority                 # match: exact (case-insensitive) lookup
  field: enrichment.seniority
  match: {"C-Level": 15, "VP": 14, "Director": 12}
  default: 0

- name: tech_stack                # any_of: sum of hits, capped
  field: enrichment.tech_stack
  any_of: {"Salesforce": 8, "HubSpot": 8, "Outreach": 5}
  cap: 15
```

`field` paths resolve against `lead.*` (HubSpot contact incl. `lead.hubspot_properties.<any>`) and `enrichment.*`. Tiers are plain thresholds. Change the YAML, restart, done — no code.

## Routing

```yaml
rules:                                      # first match wins
  - name: strategic_accounts
    match: { domains: [acme.com] }
    team: enterprise
    strategy: named
    sdr: sdr-ent-1
  - name: tier_a_enterprise
    match: { tier: [A], min_employees: 500 }
    team: enterprise                        # strategy comes from teams.enterprise
  - name: tier_b_c_smb
    match: { tier: [B, C] }
    team: smb

teams:
  enterprise: { strategy: least_loaded, fallback_team: midmarket }
  midmarket:  { strategy: territory,    fallback_team: smb }
  smb:        { strategy: round_robin,  fallback_team: null }
```

Match keys: `tier`, `domains`, `countries`, `source`, `min_employees`, `max_employees`. SDRs carry `territories`, `capacity`, `weight` (0.5 = ramping rep gets half the leads), `active`, and optional `working_hours: {tz, days, start, end}`.

Round-robin cursors live in the DB, so rotation is fair across restarts and multiple workers.

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/webhooks/hubspot` | MQL trigger. Accepts HubSpot workflow webhooks (`{objectId}`) and developer-app subscriptions (`[{objectId, eventId, ...}]`). Verifies `X-HubSpot-Signature-v3` / `X-HubSpot-Signature`. Returns 202 and processes in the background. |
| `POST` | `/webhooks/clay` | Clay table callback for push-model enrichment. Merges enrichment and resumes the pipeline. |
| `POST` | `/webhooks/slack/interactive` | Button clicks. Verifies Slack signature. |
| `POST` | `/leads/{id}/accept` · `/contacted` · `/reassign` · `/resume` | Same actions from any system (Outreach/Salesloft webhook, Salesforce flow, cron). |
| `GET` | `/leads/{id}` · `/leads?stage=handed_off` | Inspect. |
| `GET` | `/stats` | Response-time median/p90/mean, counts by stage, open leads per SDR. |
| `POST` | `/sla/sweep` | Force one SLA pass (the server also runs it every `RELAY_SLA_CHECK_INTERVAL_S`). |

## Setting up the integrations

**HubSpot.** Create a private app with `crm.objects.contacts.read/write`. Then either (a) a Workflow: enroll on `Lifecycle stage = MQL` → action "Send a webhook" → `POST https://relay.yourco.com/webhooks/hubspot`, or (b) a developer app subscription on `contact.propertyChange` for `lifecyclestage`. Put the app's client secret in `RELAY_HUBSPOT_WEBHOOK_SECRET`. Optionally create contact properties `relay_score`, `relay_tier`, `relay_owner`, `relay_status` for write-back.

**Clay.** Two modes. *Push* (default): set `RELAY_CLAY_WEBHOOK_URL` to a Clay table's webhook source; add an "HTTP API" column at the end of your waterfall that POSTs the row to `/webhooks/clay` with header `x-clay-webhook-auth: <RELAY_CLAY_API_KEY>`. *Pull*: pass an `enrich_url` to `ClayClient` if you expose a synchronous enrichment endpoint. Relay reads `person.{seniority, department, linkedin_url}` and `company.{employee_count, industry, tech_stack, country}`.

**Harmonic.** `RELAY_HARMONIC_API_KEY`. Relay calls `GET /companies?website_domain=` and reads funding stage/total, last round date, 180-day headcount growth, and `harmonic_score`.

**Salesforce.** Connected app + integration user. Create these Lead fields once: `Relay_Lead_Id__c` (Text, External ID), `HubSpot_Contact_Id__c` (Text, External ID), `Relay_Score__c` (Number), `Relay_Tier__c` (Picklist), `Relay_Routing_Reason__c` (Text 255), `Relay_Handoff_At__c`, `Relay_SLA_Due_At__c`, `Relay_First_Response_At__c` (DateTime), `Relay_SLA_Breached__c` (Checkbox). Add each SDR's User Id to `config/sdrs.yaml`.

**Slack.** Bot token with `chat:write`, `im:write`, `chat:write.public`. Enable Interactivity → request URL `https://relay.yourco.com/webhooks/slack/interactive`. Put the signing secret in `RELAY_SLACK_SIGNING_SECRET`. Invite the bot to both channels.

## Deploy

```bash
docker build -t relay .
docker run --env-file .env -p 8080:8080 -v relay-data:/app relay
```

One instance handles thousands of MQLs/day comfortably; the hot path is two concurrent HTTP calls plus a few ms of scoring. For HA, point `RELAY_DATABASE_URL` at a shared SQLite on a volume or swap `relay/store.py` for Postgres (the interface is ~10 methods).

## Measuring response time

"Response time" is handoff → first `contacted` signal. Feed that signal from wherever outbound actually happens: the Slack button is the floor; wire Outreach/Salesloft "first step executed" webhooks or a Salesforce Flow on `Status → Working` to `POST /leads/{id}/contacted` for the real number. `GET /stats` gives median / p90 / mean; `relay stats` prints the same.

## Layout

```
relay/
  api.py            FastAPI app: webhooks, lead actions, stats
  cli.py            relay serve | simulate | score | stats | sweep
  pipeline.py       orchestration, resume, SLA sweep, SDR actions
  models.py         Lead, Enrichment, Score, SDR, RoutingDecision, SLAState, LeadRecord
  scoring.py        YAML-driven ICP scorer
  routing.py        team rules + SDR selection strategies
  sla.py            SLA clock, breach detection, stats
  store.py          SQLite persistence, round-robin cursors, idempotency keys
  settings.py       env config (RELAY_*)
  enrichment/       clay.py, harmonic.py, enricher.py (parallel merge)
  crm/              hubspot.py, salesforce.py
  handoff/          slack.py (Block Kit card, DMs, escalation)
config/
  icp.yaml  routing.yaml  sdrs.yaml
tests/              33 tests: scoring, routing, pipeline, API
```

## License

MIT
