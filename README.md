# Revenue Recovery

AI-assisted revenue recovery: canonicalize payment failure events from any
provider, decide a compliant recovery action, execute it, and measure what
was actually recovered against a holdout.

## Stack

Python 3.11+, FastAPI, PostgreSQL 15, SQLAlchemy 2.0 + Alembic, Pydantic v2.
One Postgres, one API process, one worker process. No new infrastructure
after day one.

## Local setup

```bash
python -m venv .venv
.venv/Scripts/activate
pip install -r requirements.txt
cp .env.example .env
alembic upgrade head
uvicorn app.api:app --reload
```

## Running the demo

```bash
make demo                    # or, with no `make` installed: python scripts/demo.py
make demo-verify             # runs the whole chain twice from empty, confirms the report matches exactly
```
No Razorpay account, no API keys, no network access needed -- `seed/generate.py` is a complete, self-contained substitute for a real provider connection. A full run (~60-90s) drops the database, migrates, seeds authored config, generates and delivers an 800-customer/30-day synthetic backlog over real HTTP, drains the queue, sweeps for abandonment/overdue, runs one recovery batch, simulates outcomes back through real ingestion, attributes them, and prints the Gate C batch report.

Each stage also runs standalone, in this order, if you want to inspect the ledger between steps:
```bash
python -m seed.reference          # authored config: business, taxonomy, mappings, templates, bounds
python -m seed.generate           # 800-customer, 30-day loss-event backlog, delivered over real HTTP
python -m app.risk.sweeps         # checkout-abandonment + invoice-overdue detection
python -m app.recovery.run --live # policy -> bounds -> channels, one batch of up to 500
python -m seed.simulate_world     # response-model outcomes, delivered back through real ingestion
python -m app.recovery.attribution
python -m app.recovery.report     # prints the Gate C batch report
```
`uvicorn app.api:app --reload` then serves the drilldown page at `/demo`.

## Status

Building against Implementation Plan v2 + its `schema.sql` and
`data-generation.md` companions. All four gates are done: **Gate A** (Day
5, canonical events + loss ledger with correct categories), **Gate B**
(Day 7, policy engine + projection/lineage, schema frozen), **Gate C**
(Day 10, a measured recovered-revenue number with holdout + per-category
attribution, computed end to end from an empty database), and **Gate D**
(Day 12, `make demo` runs twice from empty and produces byte-identical
reports). Day 6 (a live Razorpay webhook) is the one thing formally
abandoned, by the plan's own rule -- see below.

- **Days 1-2** (contracts, schema): `EventType`/`FailureReason`/
  `LossCategory`/`FaultAttribution`/`FAILURE_TAXONOMY`, `Money` with a
  currency-exponent table, `canonical/events.py`; `schema.sql`'s 28 tables
  applied as the initial migration.
- **Day 3** (ingestion + get-data-in), reworked to match
  `data-generation.md`'s cardinal rule -- the generator POSTs to the real
  endpoints and never touches a canonical table directly.
  `POST /v1/webhooks/{provider}` (HMAC-SHA256 over raw bytes, 5-minute
  freshness window) and `POST /v1/imports` both feed the same idempotent
  `raw_events` insert. `seed/generate.py` delivers an 800-customer,
  30-day backlog (payments, orders, checkouts, invoices, subscriptions,
  disputes, refunds) through `TestClient` -- real ASGI requests, real
  HMAC signing, no server process needed. Deliberate sad paths: 1%
  malformed payloads, 3% duplicate delivery, ~4% high-value tail,
  consent/DND/opt-out/hard-bounce simulation.
- **Day 4** (normalization): all six stages (structural -> typing ->
  units -> semantic -> validation -> upsert) write real rows into
  `payments`/`orders`/`checkout_sessions`/`invoices`/`subscriptions`/
  `disputes`, one `revenue_events` row per event. An unrecognized raw
  value dead-letters at the semantic stage rather than passing through
  silently, same discipline as the `transforms.py` whitelist.
- **Day 5 / Gate A**: `app/risk/detect.py` (event-driven) +
  `app/risk/sweeps.py` (checkout abandonment, invoice overdue) populate
  `revenue_at_risk`. Every attempt against one payment intent resolves to
  the same at-risk entity, so retries collapse onto one record instead of
  opening a new one per failure. A stale at-risk record auto-closes the
  moment the same intent succeeds through any later path
  (`risk.detect.on_payment_succeeded`), so Day 8 never wastes a decision
  on a loss that already resolved itself.
- **Day 6**: abandoned per the plan's own rule ("not working after 4
  hours -> capture one real payload, build a replay endpoint, move on.
  Architecture is identical; only the narration changes."). A live test
  webhook needs a public tunnel, which repeatedly died across session
  restarts rather than any Razorpay-side problem. Nothing about the
  submission depends on this: `seed/generate.py` already IS the
  self-contained substitute for a real provider connection -- anyone
  running the demo needs no Razorpay account, no tunnel, no external
  network access at all. If a real payload is ever captured later, any
  drift becomes a `field_mappings`/`value_mappings` correction, not an
  architecture change.
- **Day 7 / Gate B**: `app/policy.py` is the pure decision engine
  (`decide(failure_reason, attempt_number, entity_type, mandate_state,
  value_minor) -> Action`, 12 unit tests). `GET /v1/at-risk/summary` and
  `GET /v1/at-risk/{id}/lineage` for projection and evidence-chain lookup.
- **Day 8**: `app/bounds.py`, the H1-H10 hard-bounds + policy-bounds
  chokepoint (`execute_action()`) -- the only path to a channel dispatch.
  Only genuine per-entity compliance facts (consent, DND, hard-bounce,
  open dispute, refund recorded, revoked mandate) close a loss out
  permanently; temporary conditions (quiet hours, contact cap, dry_run,
  business recovery toggle) leave it open for a later batch. A suppressed
  decision always writes a `recovery_attempts` row with a readable
  `suppressed_reason`.
- **Day 9**: `app/channels.py` -- `SimulatedChannel` (SMS/WhatsApp/voice,
  clearly labelled, randomized latency/failure) and `EmailChannel` (the
  one real pipe, fails closed if SMTP isn't configured rather than
  fake-sending). Recovery tokens minted per TOKEN-attributed attempt,
  `/r/{token}` redirect records clicks. Cohort assignment is a
  deterministic hash -> stable 20% holdout.
- **Day 10 / Gate C**: `seed/simulate_world.py` (the response model,
  `p_recover = clamp(base_propensity * action_fit * timing_fit *
  channel_fit, 0, 0.95)`, outcomes delivered back through real ingestion,
  never written directly) and `app/recovery/attribution.py` (TOKEN /
  PAYMENT_INTENT / CHECKOUT / INVOICE / SUBSCRIPTION matcher, using each
  attempt's own decided-at window throughout, never `now()`) feed
  `app/recovery/report.py`'s batch report.

- **Day 11**: `app/explain.py` -- four evidence-bundle builders (`ATTEMPT`,
  `AT_RISK`, `BATCH`, `SUPPRESSION`), each DB-only and deterministic, each
  carrying an explicit `verifiable_numbers` allowlist. The narrative tries
  an LLM call (Claude Opus 5) when `ANTHROPIC_API_KEY` is set; either way
  (no key, an API error, or a verification failure) it falls back to a
  deterministic template -- fails closed the same way `EmailChannel` does,
  never blocking the demo on an LLM being reachable. `verify_numbers()` is
  the actual safety mechanism: every number the narrative states must be a
  literal member of the bundle's own allowlist, checked via whole-word
  tokenization (a scanning regex was tried first and immediately flagged
  its own entity-id substrings as unverified figures -- caught during
  verification, fixed, now has 6 unit tests). Cached to
  `recovery_explanations`, one row per (scope, id) -- generated once, never
  regenerated at view time. `app/demo.py` is the one server-rendered page:
  `GET /demo` (batch report + at-risk table) -> `GET /demo/at-risk/{id}`
  (full story, narrative, attempts) -> `GET /demo/attempts/{id}` (per-
  attempt or per-suppression explanation) -> `GET /demo/at-risk/{id}/raw`
  (the provider's actual raw bytes). Verified live against the real
  populated ledger (918 at-risk records, 1102 attempts, 3 batches): all
  four scopes render, all number-verify clean, all pages 200, unknown ids
  404 cleanly.

- **Day 12 / Gate D**: `scripts/demo.py` chains every stage above into
  one command (`make demo`, or directly with no `make` required). `make
  demo-verify` runs the full chain twice from an empty database and diffs
  the two batch reports byte-for-byte -- the actual Gate D acceptance
  check, not just a claim. It failed the first time this was run: two
  identical-seed runs produced different `actions_executed` counts and a
  different batch report, despite identical generated data (`927 items ·
  ₹66,22,738 at risk` matched exactly in both). Root cause: every internal
  id (`payment_id`, `checkout_id`, `customer_id`, ...) was a random
  `uuid.uuid4()`, so `app/recovery/batch.py`'s `ORDER BY detected_at LIMIT
  500` had no stable way to break ties among the many `revenue_at_risk`
  rows a single sweep call opens with the exact same timestamp -- a
  different arbitrary subset of 500 landed in the batch each run. Fixed
  by switching internal ids to `uuid5`, deterministically derived from
  each provider's own id (the same pattern `business_id_for()` already
  used for the business itself, applied consistently instead of as a
  one-off), plus explicit secondary `ORDER BY` on the now-deterministic
  `entity_id` everywhere a query result feeds sequential RNG consumption
  or a `LIMIT`. Verified: two full runs, byte-identical report, ~130s
  total for both.

Several judgment calls in Days 8-10 were made without a literal spec to
point to (the H1-H10 list, the STRONG/WEAK attribution boundary, the NET
lift formula, `action_fit` multipliers beyond the handful
`data-generation.md` gives verbatim) -- each is flagged inline in the
relevant module's docstring, in the same spirit as Day 1's loss-category
taxonomy.

## Architecture: the deterministic/LLM boundary

Everything that produces a number in the batch report -- ingestion,
normalization, risk detection, policy, bounds, channel dispatch,
attribution -- is deterministic, seeded, and covered by the determinism
check above. The **only** place an LLM enters the system is
`app/explain.py`'s narrative text (Day 11), and even there it is
constrained twice over: every number it's allowed to state comes from an
explicit `verifiable_numbers` allowlist built before the model ever sees
the bundle, and everything it writes is checked word-by-word against that
allowlist after the fact. No API key configured, an API error, or a
failed check all land on the same deterministic template -- the LLM can
make the demo's prose nicer; it can never make a number in the ledger
wrong, and it can never block the demo from running.

## What's simulated vs. real

- **Real**: HMAC verification, the normalize pipeline, risk detection,
  the H1-H10 bounds chokepoint, the holdout split, attribution, one email
  channel (sends via SMTP if configured, fails closed otherwise).
- **Simulated, clearly labelled as such**: SMS/WhatsApp/voice delivery
  (`SimulatedChannel`, no real DLT/Meta-registered sender), Razorpay
  Payment Links (a placeholder URL shaped like one -- no live Razorpay API
  credentials in this build), and the world simulator's customer response
  model (`p_recover = clamp(base_propensity * action_fit * timing_fit *
  channel_fit, 0, 0.95)`, `seed/simulate_world.py`).
- **Never built, Day 6 abandoned by design**: a real Razorpay webhook
  connection. See the Day 6 entry above.

## Non-goals

Said out loud, per the plan: Kafka, S3, a vector DB, SDK-based tracking,
multi-gateway failover, a React SPA, auth, real SMS/WhatsApp at scale,
and fraud-rule tuning (Razorpay exposes no such API to anyone -- a
platform ceiling, not an access gap this build failed to close). Three
detection capabilities are explicitly out of scope because Razorpay's API
doesn't expose what they'd need:

- **A2, settlement-freeze detection** -- no `settlement.delayed` webhook exists.
- **A3, reconciliation leakage** -- no API surface to detect money collected but never reconciled.
- **A4, checkout-friction cause analysis** -- would need session-replay-level instrumentation this build doesn't have.
