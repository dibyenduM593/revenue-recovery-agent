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
.venv/Scripts/activate        # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
docker compose up -d --wait   # Postgres 15 on localhost:5432, matching .env.example
alembic upgrade head
python -m seed.reference      # authored config: business, taxonomy, mappings, templates, bounds
uvicorn app.api:app --reload
```
Already have a Postgres 15 instance running elsewhere? Skip `docker compose`
and point `DATABASE_URL` in `.env` at it instead. The target database needs
to exist before `alembic upgrade head` runs.

Open `http://localhost:8000/dashboard`: click **Populate fresh data** to
generate a fresh synthetic backlog, then **Launch recovery actions** to run
the pipeline end to end and watch the KPIs update live as simulated
customer replies come back.

## Running the demo

Same Postgres as above, nothing else from Local setup is required first.
This path manages its own migration and reference seed from an empty
database.

```bash
make demo                    # or, with no `make` installed: python scripts/demo.py
make demo-verify             # runs the whole chain twice from empty, confirms the report matches exactly
```
No Razorpay account, no API keys, no network access needed: `seed/generate.py`
is a complete, self-contained substitute for a real provider connection. A
full run (~60-90s) drops the database, migrates, seeds authored config,
generates and delivers an 800-customer/30-day synthetic backlog over real
HTTP, drains the queue, sweeps for abandonment/overdue, runs one recovery
batch, simulates outcomes back through real ingestion, attributes them, and
prints the Gate C batch report.

Each stage also runs standalone, in this order, if you want to inspect the
ledger between steps:
```bash
python -m seed.reference          # authored config: business, taxonomy, mappings, templates, bounds
python -m seed.generate           # 800-customer, 30-day loss-event backlog, delivered over real HTTP
python -m app.risk.sweeps         # checkout-abandonment + invoice-overdue detection
python -m app.recovery.run --live # policy -> bounds -> channels, one batch of up to 500
python -m seed.simulate_world     # response-model outcomes, delivered back through real ingestion
python -m app.recovery.attribution
python -m app.recovery.report     # prints the Gate C batch report
```
`uvicorn app.api:app --reload` also serves `/dashboard` (the interactive
control panel above).

## Status

All four gates are done:

- **Gate A**: canonical events + loss ledger with correct categories.
- **Gate B**: policy engine + projection/lineage, schema frozen.
- **Gate C**: a measured recovered-revenue number with holdout +
  per-category attribution, computed end to end from an empty database.
- **Gate D**: `make demo` runs twice from empty and produces
  byte-identical reports.

A live Razorpay webhook connection is the one thing formally abandoned
(no public tunnel stayed up reliably enough to finish it). Nothing about
the submission depends on it: `seed/generate.py` is already the
self-contained substitute for a real provider connection, so running the
demo needs no Razorpay account, tunnel, or external network access at all.

Key pieces:

- **Contracts and schema**: `EventType`/`FailureReason`/`LossCategory`/
  `FaultAttribution`/`FAILURE_TAXONOMY`, `Money` with a currency-exponent
  table, `canonical/events.py`; `schema.sql`'s 28 tables applied as the
  initial migration.
- **Ingestion**: the generator POSTs to the real endpoints and never
  touches a canonical table directly. `POST /v1/webhooks/{provider}`
  (HMAC-SHA256 over raw bytes, 5-minute freshness window) and
  `POST /v1/imports` both feed the same idempotent `raw_events` insert.
  Deliberate sad paths: malformed payloads, duplicate delivery, a
  high-value tail, consent/DND/opt-out/hard-bounce simulation.
- **Normalization**: six stages (structural, typing, units, semantic,
  validation, upsert) write real rows into `payments`/`orders`/
  `checkout_sessions`/`invoices`/`subscriptions`/`disputes`. An
  unrecognized raw value dead-letters at the semantic stage rather than
  passing through silently.
- **Gate A, risk ledger**: `app/risk/detect.py` (event-driven) +
  `app/risk/sweeps.py` (checkout abandonment, invoice overdue) populate
  `revenue_at_risk`. Retries collapse onto one record instead of opening
  a new one per failure, and a stale record auto-closes the moment the
  same intent succeeds through any later path.
- **Gate B, policy engine**: `app/policy.py` is the pure decision engine
  (`decide(failure_reason, attempt_number, entity_type, mandate_state,
  value_minor) -> Action`, 12 unit tests).
- **Hard bounds chokepoint**: `app/bounds.py`, the H1-H10 hard-bounds +
  policy-bounds chokepoint (`execute_action()`), the only path to a
  channel dispatch. Only genuine compliance facts (consent, DND,
  hard-bounce, open dispute, revoked mandate) close a loss out
  permanently; temporary conditions (quiet hours, contact cap, dry_run)
  leave it open for a later batch.
- **Recovery channels**: `SimulatedChannel` (SMS/WhatsApp/voice, clearly
  labelled) and `EmailChannel` (the one real pipe, fails closed if SMTP
  isn't configured). Cohort assignment is a deterministic hash giving a
  stable 20% holdout.
- **Gate C, attribution and measured lift**: `seed/simulate_world.py`
  (the response model) and `app/recovery/attribution.py` (TOKEN /
  PAYMENT_INTENT / CHECKOUT / INVOICE / SUBSCRIPTION matcher) feed
  `app/recovery/report.py`'s batch report.
- **Explainability**: `app/explain.py` builds four evidence bundles
  (`ATTEMPT`, `AT_RISK`, `BATCH`, `SUPPRESSION`), each deterministic and
  carrying an explicit `verifiable_numbers` allowlist. An LLM call
  (Claude Opus 5) narrates it when `ANTHROPIC_API_KEY` is set; any
  failure (no key, an API error, a failed verification) falls back to a
  deterministic template. `verify_numbers()` checks every number the
  narrative states against the allowlist, word by word.
- **Gate D, deterministic end-to-end demo**: `scripts/demo.py` chains
  every stage into one command. `make demo-verify` runs the full chain
  twice from empty and diffs the two batch reports byte-for-byte. The
  first run of this check failed: every internal id was a random
  `uuid.uuid4()`, so a query with no stable tiebreaker picked a different
  arbitrary subset of records each run. Fixed by switching internal ids
  to `uuid5` (deterministically derived from each provider's own id) and
  adding explicit secondary ordering wherever a query result feeds a
  `LIMIT`. Verified with two full runs producing a byte-identical report.

A few judgment calls (the H1-H10 list, the STRONG/WEAK attribution
boundary, the NET lift formula) had no single authoritative source to
settle them; each is flagged inline in the relevant module's docstring.

## Architecture: the deterministic/LLM boundary

Everything that produces a number in the batch report (ingestion,
normalization, risk detection, policy, bounds, channel dispatch,
attribution) is deterministic, seeded, and covered by the determinism
check above. The **only** place an LLM enters the system is
`app/explain.py`'s narrative text, and even there it's constrained twice
over: every number it's allowed to state comes from an explicit allowlist
built before the model sees the bundle, and everything it writes is
checked word-by-word against that allowlist afterward. No API key, an API
error, or a failed check all land on the same deterministic template. The
LLM can make the prose nicer; it can never make a ledger number wrong,
and it can never block the demo from running.

## Outbound dispatch queue

`execute_action()` no longer calls a provider inline inside the batch
loop. A channel-bearing action now writes one row to `outbound_dispatches`
and returns; `app/dispatch_worker.py` drains that table separately,
highest predicted-value first, using the same ranking `batch.py` already
used to decide which candidates survive the batch cap.

`recovery_attempts` gained a second timestamp for this: `enqueued_at`
(authorized) and `executed_at` (actually sent). The gap between them is
where the worker re-checks every time- and consent-sensitive bound
against the current clock immediately before calling a provider, so an
item authorized just before quiet hours doesn't go out after they start.
The worker also enforces `max_sends_per_hour`, a column that existed but
was never actually checked by anything before this queue.

`RETRY_NOW`/`RETRY_SCHEDULED`/`OPS_ALERT` never touch a customer channel
and still resolve synchronously; only real customer-facing sends go
through the queue.

**Two channels, one decision, one nudge, and the call is structurally
unable to be credited with a recovery.** A live-demo call fires VOICE and
WHATSAPP together from a single decision, sharing one minted token. Only
the WhatsApp leg's `recovery_attempts` row carries that token, so the
VOICE attempt is attribution-ineligible by foreign key, not convention.

**Retries: up to 3 nudges per loss, about a day apart.**
`app/recovery/nudge_retry.py` runs after attribution: if a nudge's
attribution window closes with no match and fewer than 3 have been sent,
it fires another. On the 3rd unconverted miss it writes a terminal
`NOT_RECOVERED` outcome. If any retry converts, attribution writes
`RECOVERED` and the retry loop stops for that loss.

## What's simulated vs. real

- **Real**: HMAC verification, the normalize pipeline, risk detection,
  the H1-H10 bounds chokepoint, the holdout split, attribution, the
  outbound dispatch queue, one email channel, and, for exactly one
  allowlisted phone number (see "Live demo" below), a real Twilio voice
  call and WhatsApp message.
- **Simulated, clearly labelled as such**: SMS/WhatsApp/voice for every
  number not on `TWILIO_ALLOWLIST`, Razorpay Payment Links (a
  placeholder URL, no live credentials in this build), and the world
  simulator's customer response model.
- **Never built, abandoned by design**: a real Razorpay webhook
  connection.

## Known limitation: customer_type/segment/sector/business_model are fabricated

These four inputs to the recovery-likelihood model come from a
deterministic hash of the customer's email address, not anything actually
true about them. Harmless for the demo, since the synthetic generator
enforces the same value when deciding which events a customer gets, but
these fields have **no real-world equivalent**: no payment gateway,
Razorpay included, exposes a customer's B2C/B2B status, sector, or
business model on a payment/webhook payload. A real deployment would need
to source this from the merchant's own systems, or drop these fields and
retrain both models from scratch on a real feature set (the trained
booster's own JSON bakes in the current columns, so a schema change
without retraining raises `feature_names mismatch`, which the scorer
already handles by falling back to the fully-transparent baseline scorer
rather than serve a wrong prediction).

## Live demo (walk-up)

`app/live_demo.py` plants one real phone number's failed payment into the
real pipeline through `/v1/imports`, the same endpoint the synthetic
corpus uses, so normalization, risk detection, and scoring all run for it
exactly as they do for corpus data. Planting the payment fires the VOICE
+ WHATSAPP fan-out described above and drains the queue.

**A number reaches Twilio's real API only if it's in `TWILIO_ALLOWLIST`.**
For the demo this holds exactly one number, verified in the Twilio CLI
ahead of time.

**No money moves in this demo, and the WhatsApp message says so.** There
is no live Razorpay Payment Link API in this build, so the message asks
the recipient to reply **YES** or **NO** instead of asking for payment. A
**YES** is ingested as a real `payment.captured` webhook payload carrying
the nudge's token, through the same path any real recovery signal takes,
and gets credited as `RECOVERED` with the same confidence a real payment
link click would produce. A real deployment swaps this one stand-in for
a Razorpay Payment Link webhook and changes nothing else.

A planted row shows up everywhere in the dashboard a real loss would, but
the batch report excludes it from the treatment/holdout cohort totals and
the claimed lift number, since it's forced into the TREATMENT cohort
rather than assigned by the normal hash split.

## Non-goals

Deliberately cut: Kafka, S3, a vector DB, SDK-based tracking,
multi-gateway failover, a React SPA, auth, real SMS/WhatsApp at scale,
and fraud-rule tuning (Razorpay exposes no such API to anyone; a platform
ceiling, not a gap this build failed to close). Three detection
capabilities are out of scope because Razorpay's API doesn't expose what
they'd need:

- **A2, settlement-freeze detection**: no `settlement.delayed` webhook exists.
- **A3, reconciliation leakage**: no API surface to detect money collected but never reconciled.
- **A4, checkout-friction cause analysis**: would need session-replay-level instrumentation this build doesn't have.
