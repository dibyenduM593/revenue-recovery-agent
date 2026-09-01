"""customers business_id+email_normalized unique constraint

Revision ID: 00a224f640b2
Revises: 5fdfe1626481
Create Date: 2026-08-26 00:00:00.000000

Day 4's normalize upsert stage does a get-or-create-by-email lookup
against `customers` from concurrent workers; without a unique constraint
that has to be select-then-insert, which races. Replaces the plain index
on (business_id, email_normalized) with a unique constraint (which is
also an index, so nothing is lost) so the upsert can use
ON CONFLICT DO NOTHING instead.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "00a224f640b2"
down_revision: Union[str, None] = "5fdfe1626481"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS customers_business_id_email_normalized_idx")
    op.create_unique_constraint(
        "customers_business_email_uq", "customers", ["business_id", "email_normalized"]
    )


def downgrade() -> None:
    op.drop_constraint("customers_business_email_uq", "customers", type_="unique")
    op.execute("CREATE INDEX ON customers (business_id, email_normalized)")
