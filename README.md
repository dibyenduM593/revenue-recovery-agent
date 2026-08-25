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
against real Postgres. Ingestion (`POST /v1/imports`, idempotency, the
SKIP LOCKED worker) and normalization stages 1-3 (structural, typing,
units) carry over from the v1 build and are re-verified against the new
schema: the generator's 1036-event backlog drains with zero dead letters,
and the dead-letter path itself is separately confirmed with an injected
malformed event.

Not yet built: `POST /v1/webhooks/{provider}` with HMAC verification, the
semantic + validation normalization stages and the upsert into
payments/revenue_events, and everything from the loss ledger
(`revenue_at_risk`) onward. See the build plan for the full day sequence.
