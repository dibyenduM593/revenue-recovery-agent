"""Create the demo business row that a generated event stream references.

raw_events.business_id is a foreign key: a business has to exist before any
of its events can be imported, exactly like a real merchant has to finish
onboarding before their webhooks mean anything. Run this once per seed
before uploading that seed's events.jsonl.
"""

import argparse
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import Business
from seed.generator import GeneratorConfig, business_id_for


def ensure_business(session: Session, business_id, name: str) -> None:
    stmt = (
        pg_insert(Business)
        .values(business_id=business_id, name=name, created_at=datetime.now(timezone.utc))
        .on_conflict_do_nothing()
    )
    session.execute(stmt)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the demo business row for a seed.")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    cfg = GeneratorConfig(seed=args.seed)
    business_id = business_id_for(cfg)

    session = SessionLocal()
    try:
        ensure_business(session, business_id, cfg.business_name)
        session.commit()
    finally:
        session.close()

    print(f"business ready: {business_id}")


if __name__ == "__main__":
    main()
