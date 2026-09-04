"""outbound_dispatches queue + recovery_attempts.enqueued_at

Revision ID: d7b41e2a9c05
Revises: c3f9a5b7e1d2
Create Date: 2026-09-04 04:00:00.000000

Splits "we decided to send this" from "a provider accepted it". While
dispatch was synchronous those were one instant and one column
(executed_at) meant both; behind a queue they diverge, and every consumer
that asks "was this customer contacted?" means executed_at specifically.

outbound_dispatches is the outbound mirror of raw_events: same
claim-then-process shape app/worker.py already uses inbound, drained
highest-expected-recovery-first so provider rate limits cost the least
money. A row is terminal once the PROVIDER accepts -- never when the
customer responds, which can be 90 days later for an invoice.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d7b41e2a9c05"
down_revision: Union[str, None] = "c3f9a5b7e1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("recovery_attempts", sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=True))
    # Existing rows were dispatched synchronously: the moment they were
    # authorized IS the moment they were sent, so backfill rather than
    # leaving a NULL that reads as "never enqueued".
    op.execute("UPDATE recovery_attempts SET enqueued_at = executed_at WHERE executed_at IS NOT NULL")

    op.create_table(
        "outbound_dispatches",
        sa.Column("dispatch_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "attempt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("recovery_attempts.attempt_id"),
            nullable=False,
        ),
        sa.Column(
            "at_risk_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("revenue_at_risk.at_risk_id"),
            nullable=False,
        ),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("recovery_token", sa.Text(), nullable=True),
        sa.Column("priority", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivery_tries", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_delivery_tries", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("provider_message_id", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("cost_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING','IN_FLIGHT','SENT','FAILED','SUPPRESSED','EXPIRED','CANCELLED')",
            name="outbound_dispatches_status_check",
        ),
        sa.CheckConstraint(
            "channel IN ('SMS','EMAIL','WHATSAPP','VOICE')", name="outbound_dispatches_channel_check"
        ),
        sa.UniqueConstraint("idempotency_key", name="outbound_dispatches_idempotency_uq"),
    )
    op.create_index(
        "ix_outbound_dispatches_claim",
        "outbound_dispatches",
        ["business_id", "status", "scheduled_for"],
        postgresql_include=["priority"],
    )
    op.create_index("ix_outbound_dispatches_attempt", "outbound_dispatches", ["attempt_id"])


def downgrade() -> None:
    op.drop_index("ix_outbound_dispatches_attempt", table_name="outbound_dispatches")
    op.drop_index("ix_outbound_dispatches_claim", table_name="outbound_dispatches")
    op.drop_table("outbound_dispatches")
    op.drop_column("recovery_attempts", "enqueued_at")
