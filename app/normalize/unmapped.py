"""Tracks payload fields no field_mapping covers.

Feeds a review queue (unmapped_fields.triaged) a human works through to
decide whether a field is worth mapping -- never auto-added to the
schema, per schema.sql's comment on the table. A null value is skipped:
most optional fields are null on most events (error_code is null on
every successful payment, for instance), and flagging that as "unmapped"
would just be noise that buries the fields worth a human's attention.
"""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import FieldMapping, UnmappedField


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    leaves: dict[str, Any] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else key
            leaves.update(_flatten(value, path))
    else:
        leaves[prefix] = obj
    return leaves


def track_unmapped_fields(session: Session, provider: str, payload: dict, mappings: list[FieldMapping]) -> int:
    known_paths = {m.source_field for m in mappings}
    now = datetime.now(timezone.utc)

    count = 0
    for path, value in _flatten(payload).items():
        if value is None or path in known_paths:
            continue
        insert_stmt = pg_insert(UnmappedField).values(
            provider=provider,
            source_path=path,
            first_seen=now,
            last_seen=now,
            occurrences=1,
            sample_value=value,
        )
        stmt = insert_stmt.on_conflict_do_update(
            index_elements=["provider", "source_path"],
            set_={
                "last_seen": insert_stmt.excluded.last_seen,
                "occurrences": UnmappedField.occurrences + 1,
                "sample_value": insert_stmt.excluded.sample_value,
            },
        )
        session.execute(stmt)
        count += 1
    return count
