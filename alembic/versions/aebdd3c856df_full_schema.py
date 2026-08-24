"""full schema

Revision ID: aebdd3c856df
Revises:
Create Date: 2026-08-25 03:24:14.918615

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'aebdd3c856df'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        'CREATE TABLE businesses (\n\tbusiness_id UUID NOT NULL, \n\tname TEXT NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (business_id)\n)'
    )
    op.execute(
        'CREATE TABLE failure_taxonomy (\n\ttaxonomy_id UUID NOT NULL, \n\tsource_provider TEXT NOT NULL, \n\tprovider_code_raw TEXT NOT NULL, \n\tcanonical_reason TEXT NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (taxonomy_id), \n\tCONSTRAINT ux_failure_taxonomy_code UNIQUE (source_provider, provider_code_raw)\n)'
    )
    op.execute(
        'CREATE TABLE customers (\n\tcustomer_id UUID NOT NULL, \n\tbusiness_id UUID NOT NULL, \n\texternal_customer_id TEXT, \n\tname TEXT, \n\temail TEXT, \n\tphone TEXT, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (customer_id), \n\tFOREIGN KEY(business_id) REFERENCES businesses (business_id)\n)'
    )
    op.execute(
        'CREATE INDEX ix_customers_business ON customers (business_id)'
    )
    op.execute(
        'CREATE TABLE field_mappings (\n\tmapping_id UUID NOT NULL, \n\tbusiness_id UUID NOT NULL, \n\tsource_provider TEXT NOT NULL, \n\tsource_field TEXT NOT NULL, \n\tcanonical_field TEXT NOT NULL, \n\ttransform TEXT NOT NULL, \n\tapproved BOOLEAN NOT NULL, \n\tversion TEXT NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (mapping_id), \n\tFOREIGN KEY(business_id) REFERENCES businesses (business_id)\n)'
    )
    op.execute(
        'CREATE INDEX ix_field_mappings_lookup ON field_mappings (business_id, source_provider, source_field)'
    )
    op.execute(
        'CREATE TABLE raw_events (\n\traw_event_id UUID NOT NULL, \n\tbusiness_id UUID NOT NULL, \n\tsource_provider TEXT NOT NULL, \n\tsource_type TEXT NOT NULL, \n\texternal_event_id TEXT, \n\tpayload_hash TEXT NOT NULL, \n\tevent_type_raw TEXT, \n\tpayload JSONB NOT NULL, \n\theaders JSONB, \n\toccurred_at TIMESTAMP WITH TIME ZONE, \n\treceived_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tingestion_method TEXT NOT NULL, \n\tprocessing_status TEXT NOT NULL, \n\tattempt_count INTEGER NOT NULL, \n\tlast_error TEXT, \n\tlocked_until TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (raw_event_id), \n\tCONSTRAINT ux_raw_events_payload_hash UNIQUE (business_id, source_provider, payload_hash), \n\tFOREIGN KEY(business_id) REFERENCES businesses (business_id)\n)'
    )
    op.execute(
        'CREATE UNIQUE INDEX ux_raw_events_external_id ON raw_events (business_id, source_provider, external_event_id) WHERE external_event_id IS NOT NULL'
    )
    op.execute(
        'CREATE INDEX ix_raw_events_status_queue ON raw_events (processing_status, business_id)'
    )
    op.execute(
        'CREATE TABLE source_mappings (\n\tbusiness_id UUID NOT NULL, \n\tsource_provider TEXT NOT NULL, \n\twebhook_secret_ref TEXT, \n\tenabled BOOLEAN NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (business_id, source_provider), \n\tFOREIGN KEY(business_id) REFERENCES businesses (business_id)\n)'
    )
    op.execute(
        'CREATE TABLE customer_contactability (\n\tbusiness_id UUID NOT NULL, \n\tcustomer_id UUID NOT NULL, \n\tchannel TEXT NOT NULL, \n\topted_in BOOLEAN NOT NULL, \n\topted_out_at TIMESTAMP WITH TIME ZONE, \n\tdnd_registered BOOLEAN NOT NULL, \n\tquiet_hours_start TIME WITHOUT TIME ZONE, \n\tquiet_hours_end TIME WITHOUT TIME ZONE, \n\ttimezone TEXT NOT NULL, \n\tmax_contacts_per_week INTEGER NOT NULL, \n\tlast_contacted_at TIMESTAMP WITH TIME ZONE, \n\tconsecutive_failures INTEGER NOT NULL, \n\tPRIMARY KEY (business_id, customer_id, channel), \n\tFOREIGN KEY(business_id) REFERENCES businesses (business_id), \n\tFOREIGN KEY(customer_id) REFERENCES customers (customer_id)\n)'
    )
    op.execute(
        'CREATE TABLE dead_letter_events (\n\tdead_letter_id UUID NOT NULL, \n\traw_event_id UUID NOT NULL, \n\tbusiness_id UUID NOT NULL, \n\tstage TEXT NOT NULL, \n\terror_message TEXT NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tresolved BOOLEAN NOT NULL, \n\tPRIMARY KEY (dead_letter_id), \n\tFOREIGN KEY(raw_event_id) REFERENCES raw_events (raw_event_id), \n\tFOREIGN KEY(business_id) REFERENCES businesses (business_id)\n)'
    )
    op.execute(
        'CREATE INDEX ix_dead_letter_events_business ON dead_letter_events (business_id)'
    )
    op.execute(
        'CREATE TABLE orders (\n\torder_id UUID NOT NULL, \n\tbusiness_id UUID NOT NULL, \n\tcustomer_id UUID, \n\texternal_order_id TEXT, \n\tamount_minor BIGINT NOT NULL, \n\tcurrency CHAR(3) NOT NULL, \n\tstatus TEXT NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (order_id), \n\tFOREIGN KEY(business_id) REFERENCES businesses (business_id), \n\tFOREIGN KEY(customer_id) REFERENCES customers (customer_id)\n)'
    )
    op.execute(
        'CREATE INDEX ix_orders_business ON orders (business_id)'
    )
    op.execute(
        'CREATE TABLE recovery_attempts (\n\tattempt_id UUID NOT NULL, \n\tbusiness_id UUID NOT NULL, \n\tcustomer_id UUID NOT NULL, \n\tcorrelation_id TEXT NOT NULL, \n\ttarget_entity_type TEXT NOT NULL, \n\ttarget_entity_id UUID NOT NULL, \n\tat_risk_minor BIGINT NOT NULL, \n\tcurrency CHAR(3) NOT NULL, \n\tstrategy TEXT NOT NULL, \n\tchannel TEXT, \n\tcohort TEXT NOT NULL, \n\tattempt_number INTEGER NOT NULL, \n\tpolicy_version TEXT NOT NULL, \n\tdecided_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\texecuted_at TIMESTAMP WITH TIME ZONE, \n\tsuppressed_reason TEXT, \n\tcost_minor BIGINT NOT NULL, \n\tPRIMARY KEY (attempt_id), \n\tFOREIGN KEY(business_id) REFERENCES businesses (business_id), \n\tFOREIGN KEY(customer_id) REFERENCES customers (customer_id)\n)'
    )
    op.execute(
        'CREATE INDEX ix_recovery_attempts_business_target ON recovery_attempts (business_id, target_entity_type, target_entity_id)'
    )
    op.execute(
        'CREATE INDEX ix_recovery_attempts_business_correlation ON recovery_attempts (business_id, correlation_id)'
    )
    op.execute(
        'CREATE TABLE revenue_events (\n\trevenue_event_id UUID NOT NULL, \n\tbusiness_id UUID NOT NULL, \n\tevent_type TEXT NOT NULL, \n\tentity_type TEXT NOT NULL, \n\tentity_id UUID NOT NULL, \n\tcustomer_id UUID, \n\tamount_minor BIGINT NOT NULL, \n\tcurrency CHAR(3) NOT NULL, \n\toccurred_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\traw_event_id UUID NOT NULL, \n\tmapping_version TEXT, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (revenue_event_id), \n\tFOREIGN KEY(business_id) REFERENCES businesses (business_id), \n\tFOREIGN KEY(customer_id) REFERENCES customers (customer_id), \n\tFOREIGN KEY(raw_event_id) REFERENCES raw_events (raw_event_id)\n)'
    )
    op.execute(
        'CREATE INDEX ix_revenue_events_entity ON revenue_events (business_id, entity_type, entity_id)'
    )
    op.execute(
        'CREATE INDEX ix_revenue_events_business_type ON revenue_events (business_id, event_type)'
    )
    op.execute(
        'CREATE TABLE subscriptions (\n\tsubscription_id UUID NOT NULL, \n\tbusiness_id UUID NOT NULL, \n\tcustomer_id UUID NOT NULL, \n\texternal_subscription_id TEXT, \n\tstatus TEXT NOT NULL, \n\tmandate_status TEXT NOT NULL, \n\tamount_minor BIGINT NOT NULL, \n\tcurrency CHAR(3) NOT NULL, \n\tcurrent_period_start TIMESTAMP WITH TIME ZONE, \n\tcurrent_period_end TIMESTAMP WITH TIME ZONE, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (subscription_id), \n\tFOREIGN KEY(business_id) REFERENCES businesses (business_id), \n\tFOREIGN KEY(customer_id) REFERENCES customers (customer_id)\n)'
    )
    op.execute(
        'CREATE INDEX ix_subscriptions_business ON subscriptions (business_id)'
    )
    op.execute(
        'CREATE TABLE checkout_sessions (\n\tcheckout_session_id UUID NOT NULL, \n\tbusiness_id UUID NOT NULL, \n\tcustomer_id UUID, \n\torder_id UUID, \n\tamount_minor BIGINT NOT NULL, \n\tcurrency CHAR(3) NOT NULL, \n\tstatus TEXT NOT NULL, \n\tstarted_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tlast_activity_at TIMESTAMP WITH TIME ZONE, \n\tabandoned_at TIMESTAMP WITH TIME ZONE, \n\tcompleted_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (checkout_session_id), \n\tFOREIGN KEY(business_id) REFERENCES businesses (business_id), \n\tFOREIGN KEY(customer_id) REFERENCES customers (customer_id), \n\tFOREIGN KEY(order_id) REFERENCES orders (order_id)\n)'
    )
    op.execute(
        'CREATE INDEX ix_checkout_sessions_business ON checkout_sessions (business_id)'
    )
    op.execute(
        'CREATE INDEX ix_checkout_sessions_business_status ON checkout_sessions (business_id, status)'
    )
    op.execute(
        'CREATE TABLE invoices (\n\tinvoice_id UUID NOT NULL, \n\tbusiness_id UUID NOT NULL, \n\tcustomer_id UUID NOT NULL, \n\tsubscription_id UUID, \n\tamount_minor BIGINT NOT NULL, \n\tcurrency CHAR(3) NOT NULL, \n\tstatus TEXT NOT NULL, \n\tdue_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tpaid_at TIMESTAMP WITH TIME ZONE, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (invoice_id), \n\tFOREIGN KEY(business_id) REFERENCES businesses (business_id), \n\tFOREIGN KEY(customer_id) REFERENCES customers (customer_id), \n\tFOREIGN KEY(subscription_id) REFERENCES subscriptions (subscription_id)\n)'
    )
    op.execute(
        'CREATE INDEX ix_invoices_business_status ON invoices (business_id, status)'
    )
    op.execute(
        'CREATE INDEX ix_invoices_business ON invoices (business_id)'
    )
    op.execute(
        'CREATE TABLE payments (\n\tpayment_id UUID NOT NULL, \n\tbusiness_id UUID NOT NULL, \n\tcustomer_id UUID, \n\torder_id UUID, \n\tpayment_intent_id TEXT NOT NULL, \n\tattempt_number INTEGER NOT NULL, \n\tparent_payment_id UUID, \n\tamount_minor BIGINT NOT NULL, \n\tcurrency CHAR(3) NOT NULL, \n\tpayment_status TEXT NOT NULL, \n\tpayment_method TEXT, \n\tfailure_code_raw TEXT, \n\tfailure_code_canonical TEXT, \n\tinitiated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tcompleted_at TIMESTAMP WITH TIME ZONE, \n\traw_event_id UUID NOT NULL, \n\tmapping_version TEXT, \n\tPRIMARY KEY (payment_id), \n\tFOREIGN KEY(business_id) REFERENCES businesses (business_id), \n\tFOREIGN KEY(customer_id) REFERENCES customers (customer_id), \n\tFOREIGN KEY(order_id) REFERENCES orders (order_id), \n\tFOREIGN KEY(parent_payment_id) REFERENCES payments (payment_id), \n\tFOREIGN KEY(raw_event_id) REFERENCES raw_events (raw_event_id)\n)'
    )
    op.execute(
        'CREATE INDEX ix_payments_business_intent_attempt ON payments (business_id, payment_intent_id, attempt_number)'
    )
    op.execute(
        'CREATE TABLE recovery_outcomes (\n\toutcome_id UUID NOT NULL, \n\tattempt_id UUID NOT NULL, \n\tbusiness_id UUID NOT NULL, \n\toutcome_status TEXT NOT NULL, \n\trecovered_amount_minor BIGINT, \n\tcurrency CHAR(3), \n\tmatched_revenue_event_id UUID, \n\tattribution_method TEXT, \n\tobserved_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (outcome_id), \n\tFOREIGN KEY(attempt_id) REFERENCES recovery_attempts (attempt_id), \n\tFOREIGN KEY(business_id) REFERENCES businesses (business_id), \n\tFOREIGN KEY(matched_revenue_event_id) REFERENCES revenue_events (revenue_event_id)\n)'
    )
    op.execute(
        'CREATE INDEX ix_recovery_outcomes_business ON recovery_outcomes (business_id)'
    )


def downgrade() -> None:
    op.execute('DROP TABLE IF EXISTS recovery_outcomes CASCADE')
    op.execute('DROP TABLE IF EXISTS payments CASCADE')
    op.execute('DROP TABLE IF EXISTS invoices CASCADE')
    op.execute('DROP TABLE IF EXISTS checkout_sessions CASCADE')
    op.execute('DROP TABLE IF EXISTS subscriptions CASCADE')
    op.execute('DROP TABLE IF EXISTS revenue_events CASCADE')
    op.execute('DROP TABLE IF EXISTS recovery_attempts CASCADE')
    op.execute('DROP TABLE IF EXISTS orders CASCADE')
    op.execute('DROP TABLE IF EXISTS dead_letter_events CASCADE')
    op.execute('DROP TABLE IF EXISTS customer_contactability CASCADE')
    op.execute('DROP TABLE IF EXISTS source_mappings CASCADE')
    op.execute('DROP TABLE IF EXISTS raw_events CASCADE')
    op.execute('DROP TABLE IF EXISTS field_mappings CASCADE')
    op.execute('DROP TABLE IF EXISTS customers CASCADE')
    op.execute('DROP TABLE IF EXISTS failure_taxonomy CASCADE')
    op.execute('DROP TABLE IF EXISTS businesses CASCADE')
