"""SQLAlchemy models mirroring schema.sql exactly.

Column names, types, nullability, and constraints here are kept in lock
step with schema.sql -- that file is the source of truth (the Day 2
migration applies it verbatim via op.execute), this module is its ORM
mirror so app code gets typed attribute access instead of raw SQL.
"""

import uuid
from datetime import date, datetime, time

from sqlalchemy import (
    CHAR,
    SMALLINT,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))


# ---------------------------------------------------------------------
# 1. Tenancy and identity
# ---------------------------------------------------------------------
class Business(Base):
    __tablename__ = "businesses"

    business_id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    default_currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, default="INR")
    default_timezone: Mapped[str] = mapped_column(Text, nullable=False, default="Asia/Kolkata")
    recovery_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class Customer(Base):
    __tablename__ = "customers"

    customer_id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.business_id"), nullable=False)
    canonical_name: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    email_normalized: Mapped[str | None] = mapped_column(Text)
    phone_e164: Mapped[str | None] = mapped_column(Text)
    customer_type: Mapped[str | None] = mapped_column(Text)
    customer_segment: Mapped[str | None] = mapped_column(Text)
    locale: Mapped[str] = mapped_column(Text, nullable=False, default="en-IN")
    timezone: Mapped[str] = mapped_column(Text, nullable=False, default="Asia/Kolkata")
    lifetime_value_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    ap_contact_name: Mapped[str | None] = mapped_column(Text)
    ap_contact_email: Mapped[str | None] = mapped_column(Text)
    ap_contact_phone: Mapped[str | None] = mapped_column(Text)
    raw_event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    mapping_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("customer_type IN ('B2C','B2B')", name="customers_customer_type_check"),
        UniqueConstraint("business_id", "email_normalized", name="customers_business_email_uq"),
        Index("ix_customers_business_phone", "business_id", "phone_e164"),
    )


class CustomerContactability(Base):
    __tablename__ = "customer_contactability"

    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.business_id"), primary_key=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.customer_id"), primary_key=True)
    channel: Mapped[str] = mapped_column(Text, primary_key=True)
    opted_in: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    opted_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dnd_registered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    quiet_hours_start: Mapped[time] = mapped_column(Time, nullable=False, server_default=text("'21:00'"))
    quiet_hours_end: Mapped[time] = mapped_column(Time, nullable=False, server_default=text("'09:00'"))
    max_contacts_per_week: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    min_gap_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    last_contacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hard_bounced: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (CheckConstraint("channel IN ('SMS','EMAIL','WHATSAPP','VOICE')", name="contactability_channel_check"),)


class CustomerMerge(Base):
    __tablename__ = "customer_merges"

    merge_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    surviving_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    merged_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)
    merged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "method IN ('exact_email','exact_phone','source_mapping','manual')", name="customer_merges_method_check"
        ),
    )


class SourceMapping(Base):
    __tablename__ = "source_mappings"

    mapping_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    internal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    internal_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_provider: Mapped[str] = mapped_column(Text, nullable=False)
    source_object_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_object_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        UniqueConstraint(
            "business_id", "source_provider", "source_object_type", "source_object_id", name="source_mappings_unique"
        ),
    )


# ---------------------------------------------------------------------
# 2. Raw layer
# ---------------------------------------------------------------------
class RawEvent(Base):
    __tablename__ = "raw_events"

    raw_event_id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_provider: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    external_event_id: Mapped[str | None] = mapped_column(Text)
    payload_hash: Mapped[str] = mapped_column(Text, nullable=False)
    event_type_raw: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    headers: Mapped[dict | None] = mapped_column(JSONB)
    api_version: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    ingestion_method: Mapped[str] = mapped_column(Text, nullable=False)
    signature_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    processing_status: Mapped[str] = mapped_column(Text, nullable=False, default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "ingestion_method IN ('WEBHOOK','API_PULL','CSV','JSON')", name="raw_events_ingestion_method_check"
        ),
        CheckConstraint(
            "processing_status IN ('PENDING','PROCESSING','PROCESSED','DEAD')", name="raw_events_processing_status_check"
        ),
        Index(
            "raw_events_ext_id_uq",
            "business_id",
            "source_provider",
            "external_event_id",
            unique=True,
            postgresql_where=text("external_event_id IS NOT NULL"),
        ),
        UniqueConstraint("business_id", "source_provider", "payload_hash", name="raw_events_hash_uq"),
        Index(
            "raw_events_queue_idx",
            "processing_status",
            "next_attempt_at",
            postgresql_where=text("processing_status IN ('PENDING','PROCESSING')"),
        ),
    )


class DeadLetterEvent(Base):
    __tablename__ = "dead_letter_events"

    dead_letter_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    raw_event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("raw_events.raw_event_id"), nullable=False)
    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    failed_stage: Mapped[str] = mapped_column(Text, nullable=False)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------
# 3. Mapping registry
# ---------------------------------------------------------------------
class FieldMapping(Base):
    __tablename__ = "field_mappings"

    mapping_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    api_version: Mapped[str] = mapped_column(Text, nullable=False, default="v1")
    source_object: Mapped[str] = mapped_column(Text, nullable=False)
    source_field: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_field: Mapped[str] = mapped_column(Text, nullable=False)
    transformation: Mapped[str] = mapped_column(Text, nullable=False)
    mapping_source: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Numeric(3, 2))
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approved_by: Mapped[str | None] = mapped_column(Text)
    version: Mapped[str] = mapped_column(Text, nullable=False, default="v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(
            "mapping_source IN ('official_documentation','llm_proposed','manual')",
            name="field_mappings_mapping_source_check",
        ),
        CheckConstraint(
            "NOT (canonical_field IN ('amount_minor','at_risk_minor','recovered_minor',"
            "'failure_code_raw','total_amount_minor') AND mapping_source = 'llm_proposed' "
            "AND approved_by IS NULL)",
            name="money_fields_need_human",
        ),
        UniqueConstraint(
            "provider", "api_version", "source_object", "source_field", "version", name="field_mappings_unique"
        ),
    )


class ValueMapping(Base):
    __tablename__ = "value_mappings"

    value_mapping_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_field: Mapped[str] = mapped_column(Text, nullable=False)
    source_value: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_value: Mapped[str] = mapped_column(Text, nullable=False)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        UniqueConstraint("provider", "canonical_field", "source_value", name="value_mappings_unique"),
    )


class FailureTaxonomyEntry(Base):
    __tablename__ = "failure_taxonomy"

    failure_reason: Mapped[str] = mapped_column(Text, primary_key=True)
    loss_category: Mapped[str] = mapped_column(Text, nullable=False)
    fault_attribution: Mapped[str] = mapped_column(Text, nullable=False)
    claimable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    backoff_policy: Mapped[str | None] = mapped_column(Text)
    default_action: Mapped[str] = mapped_column(Text, nullable=False)
    escalation_action: Mapped[str | None] = mapped_column(Text)
    attribution_window_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=259200)
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "fault_attribution IN ('BUSINESS_FAULT','CUSTOMER_SENTIMENT','CUSTOMER_CIRCUMSTANCE','EXTERNAL')",
            name="failure_taxonomy_fault_attribution_check",
        ),
    )


class UnmappedField(Base):
    __tablename__ = "unmapped_fields"

    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    source_path: Mapped[str] = mapped_column(Text, primary_key=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    occurrences: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    sample_value: Mapped[dict | None] = mapped_column(JSONB)
    triaged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ReprocessingRun(Base):
    __tablename__ = "reprocessing_runs"

    run_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    from_version: Mapped[str | None] = mapped_column(Text)
    to_version: Mapped[str | None] = mapped_column(Text)
    events_affected: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_delta_minor: Mapped[int | None] = mapped_column(BigInteger)
    ran_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


# ---------------------------------------------------------------------
# 4. Canonical entities
# ---------------------------------------------------------------------
class Order(Base):
    __tablename__ = "orders"

    order_id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.customer_id"))
    order_status: Mapped[str] = mapped_column(Text, nullable=False)
    subtotal_minor: Mapped[int | None] = mapped_column(BigInteger)
    discount_minor: Mapped[int | None] = mapped_column(BigInteger)
    tax_minor: Mapped[int | None] = mapped_column(BigInteger)
    shipping_minor: Mapped[int | None] = mapped_column(BigInteger)
    total_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    raw_event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    mapping_version: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_orders_business_customer", "business_id", "customer_id"),)


class Payment(Base):
    __tablename__ = "payments"

    payment_id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.customer_id"))
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.order_id"))
    payment_intent_id: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    parent_payment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payments.payment_id"))
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount_refunded_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    payment_status: Mapped[str] = mapped_column(Text, nullable=False)
    payment_method: Mapped[str | None] = mapped_column(Text)
    failure_code_raw: Mapped[str | None] = mapped_column(Text)
    failure_code_canonical: Mapped[str | None] = mapped_column(ForeignKey("failure_taxonomy.failure_reason"))
    error_source: Mapped[str | None] = mapped_column(Text)
    error_step: Mapped[str | None] = mapped_column(Text)
    error_description: Mapped[str | None] = mapped_column(Text)
    issuer_bank: Mapped[str | None] = mapped_column(Text)
    card_network: Mapped[str | None] = mapped_column(Text)
    card_last4: Mapped[str | None] = mapped_column(CHAR(4))
    card_expiry_month: Mapped[int | None] = mapped_column(SMALLINT)
    card_expiry_year: Mapped[int | None] = mapped_column(SMALLINT)
    vpa: Mapped[str | None] = mapped_column(Text)
    wallet: Mapped[str | None] = mapped_column(Text)
    acquirer_data: Mapped[dict | None] = mapped_column(JSONB)
    initiated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    raw_event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    mapping_version: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_payments_business_intent_attempt", "business_id", "payment_intent_id", "attempt_number"),
        Index("ix_payments_business_order", "business_id", "order_id"),
        Index("ix_payments_business_failure_initiated", "business_id", "failure_code_canonical", "initiated_at"),
        Index(
            "ix_payments_business_issuer_initiated",
            "business_id",
            "issuer_bank",
            "initiated_at",
            postgresql_where=text("payment_status = 'FAILED'"),
        ),
    )


class CheckoutSession(Base):
    __tablename__ = "checkout_sessions"

    checkout_id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.customer_id"))
    cart_value_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    item_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payment_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    abandoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_recovery_url: Mapped[str | None] = mapped_column(Text)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    raw_event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    mapping_version: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_checkout_sessions_business_status_started", "business_id", "status", "started_at"),)


class Subscription(Base):
    __tablename__ = "subscriptions"

    subscription_id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.customer_id"))
    plan_id: Mapped[str | None] = mapped_column(Text)
    billing_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    billing_frequency: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    mandate_type: Mapped[str | None] = mapped_column(Text)
    mandate_status: Mapped[str | None] = mapped_column(Text)
    mandate_max_amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    mandate_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    start_date: Mapped[date | None] = mapped_column(Date)
    next_billing_date: Mapped[date | None] = mapped_column(Date)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    raw_event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    mapping_version: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_subscriptions_business_status_next_billing", "business_id", "status", "next_billing_date"),)


class Invoice(Base):
    __tablename__ = "invoices"

    invoice_id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.customer_id"))
    invoice_number: Mapped[str | None] = mapped_column(Text)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount_paid_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    payment_terms_days: Mapped[int | None] = mapped_column(Integer)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False)
    dunning_stage: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    raw_event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    mapping_version: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_invoices_business_status_due", "business_id", "status", "due_at"),)


class Dispute(Base):
    __tablename__ = "disputes"

    dispute_id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    payment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payments.payment_id"))
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    respond_by: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    __table_args__ = (Index("ix_disputes_business_payment_status", "business_id", "payment_id", "status"),)


class RevenueEvent(Base):
    __tablename__ = "revenue_events"

    event_id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str | None] = mapped_column(CHAR(3))
    source_provider: Mapped[str] = mapped_column(Text, nullable=False)
    source_event_id: Mapped[str | None] = mapped_column(Text)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    raw_event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("raw_events.raw_event_id"), nullable=False)
    mapping_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        UniqueConstraint("raw_event_id", "entity_type", "entity_id", name="revenue_events_lineage_uq"),
        Index("ix_revenue_events_business_type_occurred", "business_id", "event_type", "occurred_at"),
        Index("ix_revenue_events_attributes_gin", "attributes", postgresql_using="gin", postgresql_ops={"attributes": "jsonb_path_ops"}),
    )


# ---------------------------------------------------------------------
# 5. The loss ledger
# ---------------------------------------------------------------------
class RevenueAtRisk(Base):
    __tablename__ = "revenue_at_risk"

    at_risk_id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.customer_id"))
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    at_risk_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    loss_category: Mapped[str] = mapped_column(Text, nullable=False)
    fault_attribution: Mapped[str] = mapped_column(Text, nullable=False)
    claimable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(ForeignKey("failure_taxonomy.failure_reason"))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    detection_rule: Mapped[str] = mapped_column(Text, nullable=False)
    detection_version: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="OPEN")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("revenue_events.event_id"))
    raw_event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        CheckConstraint(
            "entity_type IN ('PAYMENT','CHECKOUT','INVOICE','SUBSCRIPTION')", name="revenue_at_risk_entity_type_check"
        ),
        CheckConstraint(
            "status IN ('OPEN','IN_RECOVERY','RECOVERED','LOST','EXPIRED','SUPPRESSED')",
            name="revenue_at_risk_status_check",
        ),
        Index(
            "revenue_at_risk_open_uq",
            "business_id",
            "entity_type",
            "entity_id",
            unique=True,
            postgresql_where=text("status = 'OPEN'"),
        ),
        Index("ix_revenue_at_risk_business_status_detected", "business_id", "status", "detected_at"),
        Index("ix_revenue_at_risk_business_category_claimable", "business_id", "loss_category", "claimable"),
    )


# ---------------------------------------------------------------------
# 6. Bounds
# ---------------------------------------------------------------------
class PolicyBounds(Base):
    __tablename__ = "policy_bounds"

    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.business_id"), primary_key=True)
    max_attempts_per_entity: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    max_contacts_per_week: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    min_gap_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    max_entities_per_batch: Mapped[int] = mapped_column(Integer, nullable=False, default=500)
    max_batch_spend_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=500000)
    max_sends_per_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    human_approval_above_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=5000000)
    allow_automated_charge: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    quiet_hours_enforced: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    holdout_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    policy_version: Mapped[str] = mapped_column(Text, nullable=False, default="bounds@v1")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (CheckConstraint("holdout_percent BETWEEN 0 AND 50", name="policy_bounds_holdout_percent_check"),)


class MessageTemplate(Base):
    __tablename__ = "message_templates"

    template_id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    loss_category: Mapped[str] = mapped_column(Text, nullable=False)
    locale: Mapped[str] = mapped_column(Text, nullable=False, default="en-IN")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    variables: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    dlt_template_id: Mapped[str | None] = mapped_column(Text)
    dlt_header: Mapped[str | None] = mapped_column(Text)
    meta_template_name: Mapped[str | None] = mapped_column(Text)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approved_by: Mapped[str | None] = mapped_column(Text)
    generated_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("generated_by IN ('human','llm_proposed')", name="message_templates_generated_by_check"),
        CheckConstraint(
            "NOT (approved AND ((channel = 'SMS' AND dlt_template_id IS NULL) "
            "OR (channel = 'WHATSAPP' AND meta_template_name IS NULL)))",
            name="registered_before_approved",
        ),
    )


# ---------------------------------------------------------------------
# 7. Recovery ledgers
# ---------------------------------------------------------------------
class RecoveryToken(Base):
    __tablename__ = "recovery_tokens"

    token: Mapped[str] = mapped_column(Text, primary_key=True)
    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    at_risk_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("revenue_at_risk.at_risk_id"), nullable=False)
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    target_url: Mapped[str] = mapped_column(Text, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    first_clicked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    click_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    converted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_recovery_tokens_at_risk", "at_risk_id"),)


class RecoveryAttempt(Base):
    __tablename__ = "recovery_attempts"

    attempt_id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    at_risk_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("revenue_at_risk.at_risk_id"), nullable=False)
    correlation_id: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    strategy: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str | None] = mapped_column(Text)
    template_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("message_templates.template_id"))
    cohort: Mapped[str] = mapped_column(Text, nullable=False)
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    bounds_version: Mapped[str] = mapped_column(Text, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    consent_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    bounds_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suppressed_reason: Mapped[str | None] = mapped_column(Text)
    channel_receipt: Mapped[dict | None] = mapped_column(JSONB)
    delivery_status: Mapped[str | None] = mapped_column(Text)
    cost_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    attribution_key_type: Mapped[str] = mapped_column(Text, nullable=False)
    attribution_key_value: Mapped[str] = mapped_column(Text, nullable=False)
    attribution_window_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    attribution_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recovery_token: Mapped[str | None] = mapped_column(ForeignKey("recovery_tokens.token"))

    __table_args__ = (
        CheckConstraint(
            "strategy IN ('RETRY_NOW','RETRY_SCHEDULED','NUDGE','REQUEST_NEW_INSTRUMENT',"
            "'RECOLLECT_MANDATE','ESCALATE_HUMAN','OPS_ALERT','STOP')",
            name="recovery_attempts_strategy_check",
        ),
        CheckConstraint("cohort IN ('TREATMENT','HOLDOUT')", name="recovery_attempts_cohort_check"),
        CheckConstraint(
            "attribution_key_type IN ('TOKEN','PAYMENT_INTENT','ORDER','INVOICE','CHECKOUT','SUBSCRIPTION')",
            name="recovery_attempts_attribution_key_type_check",
        ),
        UniqueConstraint("correlation_id", name="recovery_attempts_correlation_id_uq"),
        Index("ix_recovery_attempts_business_at_risk_attempt", "business_id", "at_risk_id", "attempt_number"),
        Index(
            "ix_recovery_attempts_attribution_key",
            "attribution_key_type",
            "attribution_key_value",
            postgresql_where=text("executed_at IS NOT NULL"),
        ),
    )


class RecoveryOutcome(Base):
    __tablename__ = "recovery_outcomes"

    outcome_id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    at_risk_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("revenue_at_risk.at_risk_id"), nullable=False)
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("recovery_attempts.attempt_id"))
    cohort: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    recovered_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    proof_event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("revenue_events.event_id"))
    attribution_method: Mapped[str] = mapped_column(Text, nullable=False)
    attribution_confidence: Mapped[str] = mapped_column(Text, nullable=False)
    latency_seconds: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(
            "outcome IN ('RECOVERED','PARTIALLY_RECOVERED','NOT_RECOVERED','EXPIRED','STOPPED')",
            name="recovery_outcomes_outcome_check",
        ),
        CheckConstraint(
            "attribution_method IN ('TOKEN_CLICK','PAYMENT_INTENT_MATCH','ORDER_MATCH',"
            "'INVOICE_PAID','SUBSCRIPTION_CHARGED','HOLDOUT_BASELINE')",
            name="recovery_outcomes_attribution_method_check",
        ),
        CheckConstraint(
            "attribution_confidence IN ('STRONG','WEAK')", name="recovery_outcomes_attribution_confidence_check"
        ),
        UniqueConstraint("at_risk_id", name="recovery_outcomes_at_risk_uq"),
        Index("ix_recovery_outcomes_business_cohort_outcome", "business_id", "cohort", "outcome"),
    )


class RecoveryExplanation(Base):
    __tablename__ = "recovery_explanations"

    explanation_id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    at_risk_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("revenue_at_risk.at_risk_id"))
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("recovery_attempts.attempt_id"))
    batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    evidence_bundle: Mapped[dict] = mapped_column(JSONB, nullable=False)
    narrative: Mapped[str] = mapped_column(Text, nullable=False)
    numbers_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    unverified_spans: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(
            "scope IN ('ATTEMPT','AT_RISK','BATCH','SUPPRESSION')", name="recovery_explanations_scope_check"
        ),
        Index("ix_recovery_explanations_business_scope_at_risk", "business_id", "scope", "at_risk_id"),
    )


class OpsAlert(Base):
    __tablename__ = "ops_alerts"

    alert_id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    alert_type: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False)
    amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------
# 8. Batch runs
# ---------------------------------------------------------------------
class RecoveryBatch(Base):
    __tablename__ = "recovery_batches"

    batch_id: Mapped[uuid.UUID] = uuid_pk()
    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    entities_scanned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    at_risk_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    decisions_made: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    actions_executed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    actions_suppressed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    actions_stopped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    spend_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    bounds_version: Mapped[str] = mapped_column(Text, nullable=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False)
