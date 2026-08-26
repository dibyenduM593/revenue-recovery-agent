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
docker compose up -d
cp .env.example .env
alembic upgrade head
uvicorn app.api:app --reload
```

## Status

Building against Implementation Plan v2 (revised 25 Aug 2026, `schema.sql`
companion). Day 1 (vocabulary: `EventType`, `FailureReason`,
`LossCategory`, `FaultAttribution`, `FAILURE_TAXONOMY`; `Money` with a
currency-exponent table; `canonical/events.py`) and Day 2 (`schema.sql`,
28 tables, applied as the initial Alembic migration) are done and verified
against real Postgres. Day 3 (ingestion + queue) is done: `POST /v1/webhooks/{provider}` verifies
HMAC-SHA256 over the raw request body before any JSON parsing, checks a
5-minute freshness window on the payload's own `created_at`, and stores
through the same idempotent `raw_events` insert as `POST /v1/imports`.
Currently wired for Razorpay only (`RAZORPAY_WEBHOOK_SECRET` in `.env`);
an unconfigured provider gets a 404. The SKIP LOCKED worker and
normalization stages 1-3 (structural, typing, units) carry over from the
v1 build and are re-verified against the new schema: the generator's
1036-event backlog drains with zero dead letters, the dead-letter path
itself is separately confirmed with an injected malformed event, and the
webhook endpoint is confirmed idempotent (same payload posted 3x -> one
row), signature/timestamp rejection both return the right status codes.

Not yet built: the semantic + validation normalization stages and the
upsert into payments/revenue_events, and everything from the loss ledger
(`revenue_at_risk`) onward. See the build plan for the full day sequence.
