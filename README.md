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

Day 1: repo scaffold, failure taxonomy (`app/canonical/vocabulary.py`),
`Money` type (`app/canonical/money.py`), FastAPI skeleton, Alembic wired to
`app.db.Base`. Schema, ingest, normalization, and the recovery layer land in
the days that follow — see the build plan for the full sequence.
