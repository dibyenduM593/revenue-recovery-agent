"""customer_recovery_profile table

Revision ID: c3f9a5b7e1d2
Revises: 00a224f640b2
Create Date: 2026-09-02 00:00:00.000000

One row per scored at-risk item: the predicted_score used to prioritise
the batch, snapshotted at scoring time (same discipline as
recovery_attempts.bounds_snapshot) so a later model swap doesn't rewrite
history. features is the exact input the score saw -- shared by training
export and runtime scoring via app/scoring/features.py.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c3f9a5b7e1d2"
down_revision: Union[str, None] = "00a224f640b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "customer_recovery_profile",
        sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.business_id"), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("customers.customer_id"), nullable=False),
        sa.Column("at_risk_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("revenue_at_risk.at_risk_id"), nullable=False),
        sa.Column("predicted_score", sa.REAL(), nullable=False),
        sa.Column("score_version", sa.Text(), nullable=False),
        sa.Column("features", postgresql.JSONB(), nullable=False),
        sa.Column("scored_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("business_id", "at_risk_id", name="customer_recovery_profile_pkey"),
    )
    op.create_index(
        "ix_customer_recovery_profile_customer",
        "customer_recovery_profile",
        ["business_id", "customer_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_customer_recovery_profile_customer", table_name="customer_recovery_profile")
    op.drop_table("customer_recovery_profile")
