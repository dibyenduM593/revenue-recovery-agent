import uuid
from datetime import datetime, time

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def uuid_pk(name: str) -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class Business(Base):
    __tablename__ = "businesses"

    business_id: Mapped[uuid.UUID] = uuid_pk("business_id")
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Customer(Base):
    __tablename__ = "customers"

    customer_id: Mapped[uuid.UUID] = uuid_pk("customer_id")
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.business_id"), nullable=False)
    external_customer_id: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_customers_business", "business_id"),)


class Order(Base):
    __tablename__ = "orders"

    order_id: Mapped[uuid.UUID] = uuid_pk("order_id")
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.business_id"), nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.customer_id"))
    external_order_id: Mapped[str | None] = mapped_column(Text)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_orders_business", "business_id"),)


class CheckoutSession(Base):
    __tablename__ = "checkout_sessions"

    checkout_session_id: Mapped[uuid.UUID] = uuid_pk("checkout_session_id")
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.business_id"), nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.customer_id"))
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.order_id"))
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    abandoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_checkout_sessions_business", "business_id"),
        Index("ix_checkout_sessions_business_status", "business_id", "status"),
    )


class Subscription(Base):
    __tablename__ = "subscriptions"

    subscription_id: Mapped[uuid.UUID] = uuid_pk("subscription_id")
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.business_id"), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.customer_id"), nullable=False)
    external_subscription_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    mandate_status: Mapped[str] = mapped_column(Text, nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_subscriptions_business", "business_id"),)


class Invoice(Base):
    __tablename__ = "invoices"

    invoice_id: Mapped[uuid.UUID] = uuid_pk("invoice_id")
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.business_id"), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.customer_id"), nullable=False)
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("subscriptions.subscription_id"))
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_invoices_business", "business_id"),
        Index("ix_invoices_business_status", "business_id", "status"),
    )


class RawEvent(Base):
    __tablename__ = "raw_events"

    raw_event_id: Mapped[uuid.UUID] = uuid_pk("raw_event_id")
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.business_id"), nullable=False)
    source_provider: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    external_event_id: Mapped[str | None] = mapped_column(Text)
    payload_hash: Mapped[str] = mapped_column(Text, nullable=False)
    event_type_raw: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    headers: Mapped[dict | None] = mapped_column(JSONB)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingestion_method: Mapped[str] = mapped_column(Text, nullable=False)
    processing_status: Mapped[str] = mapped_column(Text, nullable=False, default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index(
            "ux_raw_events_external_id",
            "business_id",
            "source_provider",
            "external_event_id",
            unique=True,
            postgresql_where=text("external_event_id IS NOT NULL"),
        ),
        UniqueConstraint("business_id", "source_provider", "payload_hash", name="ux_raw_events_payload_hash"),
        Index("ix_raw_events_status_queue", "processing_status", "business_id"),
    )


class SourceMapping(Base):
    __tablename__ = "source_mappings"

    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.business_id"), primary_key=True)
    source_provider: Mapped[str] = mapped_column(Text, primary_key=True)
    webhook_secret_ref: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FieldMapping(Base):
    __tablename__ = "field_mappings"

    mapping_id: Mapped[uuid.UUID] = uuid_pk("mapping_id")
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.business_id"), nullable=False)
    source_provider: Mapped[str] = mapped_column(Text, nullable=False)
    source_field: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_field: Mapped[str] = mapped_column(Text, nullable=False)
    transform: Mapped[str] = mapped_column(Text, nullable=False, default="identity")
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_field_mappings_lookup", "business_id", "source_provider", "source_field"),
    )


class FailureTaxonomyEntry(Base):
    __tablename__ = "failure_taxonomy"

    taxonomy_id: Mapped[uuid.UUID] = uuid_pk("taxonomy_id")
    source_provider: Mapped[str] = mapped_column(Text, nullable=False)
    provider_code_raw: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("source_provider", "provider_code_raw", name="ux_failure_taxonomy_code"),
    )


class DeadLetterEvent(Base):
    __tablename__ = "dead_letter_events"

    dead_letter_id: Mapped[uuid.UUID] = uuid_pk("dead_letter_id")
    raw_event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("raw_events.raw_event_id"), nullable=False)
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.business_id"), nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (Index("ix_dead_letter_events_business", "business_id"),)


class Payment(Base):
    __tablename__ = "payments"

    payment_id: Mapped[uuid.UUID] = uuid_pk("payment_id")
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.business_id"), nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.customer_id"))
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.order_id"))
    payment_intent_id: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    parent_payment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payments.payment_id"))
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    payment_status: Mapped[str] = mapped_column(Text, nullable=False)
    payment_method: Mapped[str | None] = mapped_column(Text)
    failure_code_raw: Mapped[str | None] = mapped_column(Text)
    failure_code_canonical: Mapped[str | None] = mapped_column(Text)
    initiated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("raw_events.raw_event_id"), nullable=False)
    mapping_version: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_payments_business_intent_attempt", "business_id", "payment_intent_id", "attempt_number"),
    )


class RevenueEvent(Base):
    __tablename__ = "revenue_events"

    revenue_event_id: Mapped[uuid.UUID] = uuid_pk("revenue_event_id")
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.business_id"), nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.customer_id"))
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("raw_events.raw_event_id"), nullable=False)
    mapping_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_revenue_events_business_type", "business_id", "event_type"),
        Index("ix_revenue_events_entity", "business_id", "entity_type", "entity_id"),
    )


class CustomerContactability(Base):
    __tablename__ = "customer_contactability"

    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.business_id"), primary_key=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.customer_id"), primary_key=True)
    channel: Mapped[str] = mapped_column(Text, primary_key=True)
    opted_in: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    opted_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dnd_registered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    quiet_hours_start: Mapped[time | None] = mapped_column(Time)
    quiet_hours_end: Mapped[time | None] = mapped_column(Time)
    timezone: Mapped[str] = mapped_column(Text, nullable=False, default="Asia/Kolkata")
    max_contacts_per_week: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    last_contacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class RecoveryAttempt(Base):
    __tablename__ = "recovery_attempts"

    attempt_id: Mapped[uuid.UUID] = uuid_pk("attempt_id")
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.business_id"), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.customer_id"), nullable=False)
    correlation_id: Mapped[str] = mapped_column(Text, nullable=False)
    target_entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    at_risk_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    strategy: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str | None] = mapped_column(Text)
    cohort: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suppressed_reason: Mapped[str | None] = mapped_column(Text)
    cost_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    __table_args__ = (
        Index("ix_recovery_attempts_business_correlation", "business_id", "correlation_id"),
        Index("ix_recovery_attempts_business_target", "business_id", "target_entity_type", "target_entity_id"),
    )


class RecoveryOutcome(Base):
    __tablename__ = "recovery_outcomes"

    outcome_id: Mapped[uuid.UUID] = uuid_pk("outcome_id")
    attempt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("recovery_attempts.attempt_id"), nullable=False)
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.business_id"), nullable=False)
    outcome_status: Mapped[str] = mapped_column(Text, nullable=False)
    recovered_amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str | None] = mapped_column(CHAR(3))
    matched_revenue_event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("revenue_events.revenue_event_id"))
    attribution_method: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_recovery_outcomes_business", "business_id"),)
