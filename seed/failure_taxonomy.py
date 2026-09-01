"""Seed failure_taxonomy from app.canonical.vocabulary.FAILURE_TAXONOMY.

The Python dict is the source of truth (see FAILURE_TAXONOMY's docstring
for the caveat on how confident each category assignment is); this just
mirrors it into the DB table so payments.failure_code_canonical's foreign
key has something to reference. Day 1 built the dict but never actually
ran this -- the gap only surfaces once something tries to insert a
payments row with a canonical failure reason, which is Day 4's job.
"""

import argparse

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.canonical.vocabulary import FAILURE_TAXONOMY
from app.db import SessionLocal
from app.models import FailureTaxonomyEntry


def _rows() -> list[dict]:
    return [
        dict(
            failure_reason=reason.value,
            loss_category=entry.loss_category.value,
            fault_attribution=entry.fault_attribution.value,
            claimable=entry.claimable,
            retryable=entry.retryable,
            max_attempts=entry.max_attempts,
            backoff_policy=entry.backoff.value if entry.backoff else None,
            default_action=entry.default_action.value,
            escalation_action=entry.escalation_action.value if entry.escalation_action else None,
            attribution_window_seconds=entry.attribution_window_seconds,
            notes=entry.notes,
        )
        for reason, entry in FAILURE_TAXONOMY.items()
    ]


def seed_failure_taxonomy(session: Session) -> int:
    rows = _rows()
    insert_stmt = pg_insert(FailureTaxonomyEntry).values(rows)
    update_columns = [c.name for c in FailureTaxonomyEntry.__table__.columns if c.name != "failure_reason"]
    stmt = insert_stmt.on_conflict_do_update(
        index_elements=["failure_reason"],
        set_={col: getattr(insert_stmt.excluded, col) for col in update_columns},
    )
    session.execute(stmt)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed failure_taxonomy from the vocabulary module.")
    parser.parse_args()

    session = SessionLocal()
    try:
        n = seed_failure_taxonomy(session)
        session.commit()
    finally:
        session.close()

    print(f"seeded {n} failure_taxonomy rows")


if __name__ == "__main__":
    main()
