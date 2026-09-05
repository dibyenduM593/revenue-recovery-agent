-- =====================================================================
-- AI Revenue Recovery -- canonical schema v2
-- Revised 25 Aug 2026 against the recoverable-revenue-loss research.
--
-- Design rules enforced throughout:
--   * Money is ALWAYS <name>_minor BIGINT + a currency column. No floats,
--     no rupees, ever. Format at render time only.
--   * Every derived row carries raw_event_id + mapping_version (lineage).
--   * business_id leads every index (multi-tenant).
--   * Ledgers (at_risk / attempts / outcomes / explanations) are append-only.
--   * Provider-specific fields live in `attributes` JSONB unless >1 provider
--     has them, in which case they earn a typed column.
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------
-- 0. Enumerations (as CHECK constraints -- easier to evolve than PG enums)
-- ---------------------------------------------------------------------
-- loss_category   : WHAT kind of revenue loss this is (research A1..B4)
-- fault_attribution: WHO is responsible -- drives whether it is claimable
-- claimable       : may this appear in the headline recovered-revenue number
--
--   BUSINESS_FAULT       A1 A2 A3 A4 A5 A6 A7   -> claimable
--   CUSTOMER_SENTIMENT   B1 B2 B3 B4            -> claimable
--   CUSTOMER_CIRCUMSTANCE X_INSUFFICIENT_FUNDS  -> segment-reported only
--   EXTERNAL             X_FRAUD X_BANK_BLOCK   -> excluded entirely

-- ---------------------------------------------------------------------
-- 1. Tenancy and identity
-- ---------------------------------------------------------------------
CREATE TABLE businesses (
  business_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name              TEXT        NOT NULL,
  default_currency  CHAR(3)     NOT NULL DEFAULT 'INR',
  default_timezone  TEXT        NOT NULL DEFAULT 'Asia/Kolkata',
  recovery_enabled  BOOLEAN     NOT NULL DEFAULT FALSE,  -- global kill switch
  dry_run           BOOLEAN     NOT NULL DEFAULT TRUE,   -- decide but never send
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE customers (
  customer_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id       UUID NOT NULL REFERENCES businesses(business_id),
  canonical_name    TEXT,
  email             TEXT,
  email_normalized  TEXT,          -- lowercased, +tag and gmail-dot stripped
  phone_e164        TEXT,
  customer_type     TEXT CHECK (customer_type IN ('B2C','B2B')),
  customer_segment  TEXT,          -- nullable: no segmentation model yet
  locale            TEXT NOT NULL DEFAULT 'en-IN',   -- drives Hinglish templates
  timezone          TEXT NOT NULL DEFAULT 'Asia/Kolkata',
  lifetime_value_minor BIGINT NOT NULL DEFAULT 0,
  -- B2B receivables: the AP contact is usually NOT the customer contact.
  ap_contact_name   TEXT,
  ap_contact_email  TEXT,
  ap_contact_phone  TEXT,
  raw_event_id      UUID,
  mapping_version   TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- get-or-create-by-email during normalization needs this to be race-safe
  -- (ON CONFLICT DO NOTHING) across concurrent workers; NULLs are distinct
  -- under a Postgres UNIQUE constraint, so customers with no email at all
  -- are unaffected. Added early, still pre-Gate-B.
  UNIQUE (business_id, email_normalized)
);
CREATE INDEX ON customers (business_id, phone_e164);

-- Consent is a data model, not a feature. Read at decision time,
-- SNAPSHOTTED onto the attempt so later changes never rewrite history.
CREATE TABLE customer_contactability (
  business_id           UUID NOT NULL REFERENCES businesses(business_id),
  customer_id           UUID NOT NULL REFERENCES customers(customer_id),
  channel               TEXT NOT NULL CHECK (channel IN ('SMS','EMAIL','WHATSAPP','VOICE')),
  opted_in              BOOLEAN NOT NULL DEFAULT FALSE,
  opted_out_at          TIMESTAMPTZ,
  dnd_registered        BOOLEAN NOT NULL DEFAULT FALSE,   -- TRAI DND / DLT
  quiet_hours_start     TIME NOT NULL DEFAULT '21:00',
  quiet_hours_end       TIME NOT NULL DEFAULT '09:00',
  max_contacts_per_week INT  NOT NULL DEFAULT 3,
  min_gap_hours         INT  NOT NULL DEFAULT 24,
  last_contacted_at     TIMESTAMPTZ,
  consecutive_failures  INT  NOT NULL DEFAULT 0,          -- bounces / undelivered
  hard_bounced          BOOLEAN NOT NULL DEFAULT FALSE,
  PRIMARY KEY (business_id, customer_id, channel)
);

-- Identity resolution is deterministic + reversible. A wrong merge is a
-- privacy incident, not a data-quality issue.
CREATE TABLE customer_merges (
  merge_id     BIGSERIAL PRIMARY KEY,
  business_id  UUID NOT NULL,
  surviving_id UUID NOT NULL,
  merged_id    UUID NOT NULL,
  method       TEXT NOT NULL CHECK (method IN
                 ('exact_email','exact_phone','source_mapping','manual')),
  evidence     JSONB NOT NULL,
  merged_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  reversed_at  TIMESTAMPTZ
);

CREATE TABLE source_mappings (
  mapping_id         BIGSERIAL PRIMARY KEY,
  business_id        UUID NOT NULL,
  internal_id        UUID NOT NULL,
  internal_type      TEXT NOT NULL,    -- customer | order | payment | invoice
  source_provider    TEXT NOT NULL,
  source_object_type TEXT NOT NULL,
  source_object_id   TEXT NOT NULL,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (business_id, source_provider, source_object_type, source_object_id)
);

-- ---------------------------------------------------------------------
-- 2. Raw layer -- immutable payloads + processing state
-- ---------------------------------------------------------------------
CREATE TABLE raw_events (
  raw_event_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id       UUID NOT NULL,
  source_provider   TEXT NOT NULL,       -- razorpay | shopify | woocommerce | csv
  source_type       TEXT NOT NULL,       -- PAYMENT | ECOMMERCE | CRM
  external_event_id TEXT,
  payload_hash      TEXT NOT NULL,
  event_type_raw    TEXT,
  payload           JSONB NOT NULL,      -- IMMUTABLE
  headers           JSONB,               -- kept for signature re-verification
  api_version       TEXT,
  occurred_at       TIMESTAMPTZ,         -- provider's clock
  received_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  ingestion_method  TEXT NOT NULL CHECK (ingestion_method IN ('WEBHOOK','API_PULL','CSV','JSON')),
  signature_verified BOOLEAN NOT NULL DEFAULT FALSE,
  -- mutable processing state (only these columns ever change)
  processing_status TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK (processing_status IN ('PENDING','PROCESSING','PROCESSED','DEAD')),
  attempt_count     INT  NOT NULL DEFAULT 0,
  next_attempt_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  locked_until      TIMESTAMPTZ,
  last_error        TEXT
);
-- Idempotency enforced by the DATABASE, not by check-then-insert.
CREATE UNIQUE INDEX raw_events_ext_id_uq ON raw_events
  (business_id, source_provider, external_event_id) WHERE external_event_id IS NOT NULL;
CREATE UNIQUE INDEX raw_events_hash_uq ON raw_events
  (business_id, source_provider, payload_hash);
-- The queue's hot path.
CREATE INDEX raw_events_queue_idx ON raw_events (processing_status, next_attempt_at)
  WHERE processing_status IN ('PENDING','PROCESSING');

CREATE TABLE dead_letter_events (
  dead_letter_id BIGSERIAL PRIMARY KEY,
  raw_event_id   UUID NOT NULL REFERENCES raw_events(raw_event_id),
  business_id    UUID NOT NULL,
  failed_stage   TEXT NOT NULL,   -- STRUCTURAL|COERCION|UNITS|SEMANTIC|VALIDATION|PERSIST
  error_message  TEXT NOT NULL,
  attempt_count  INT  NOT NULL,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at    TIMESTAMPTZ
);

-- ---------------------------------------------------------------------
-- 3. Mapping registry -- the rules, as data
-- ---------------------------------------------------------------------
CREATE TABLE field_mappings (
  mapping_id      BIGSERIAL PRIMARY KEY,
  provider        TEXT NOT NULL,
  api_version     TEXT NOT NULL DEFAULT 'v1',
  source_object   TEXT NOT NULL,
  source_field    TEXT NOT NULL,        -- dotted JSON path
  canonical_field TEXT NOT NULL,
  transformation  TEXT NOT NULL,        -- name from the TRANSFORMS whitelist. NEVER eval'd.
  mapping_source  TEXT NOT NULL CHECK (mapping_source IN
                    ('official_documentation','llm_proposed','manual')),
  confidence      NUMERIC(3,2),
  approved        BOOLEAN NOT NULL DEFAULT FALSE,
  approved_by     TEXT,
  version         TEXT NOT NULL DEFAULT 'v1',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (provider, api_version, source_object, source_field, version)
);
-- Money and failure-code fields can NEVER auto-approve: the unit ambiguity
-- (is 8500 rupees or paise?) is not resolvable from the payload alone.
ALTER TABLE field_mappings ADD CONSTRAINT money_fields_need_human CHECK (
  NOT (canonical_field IN ('amount_minor','at_risk_minor','recovered_minor',
                           'failure_code_raw','total_amount_minor')
       AND mapping_source = 'llm_proposed'
       AND approved_by IS NULL)
);

CREATE TABLE value_mappings (
  value_mapping_id BIGSERIAL PRIMARY KEY,
  provider         TEXT NOT NULL,
  canonical_field  TEXT NOT NULL,    -- event_type | failure_reason | payment_status
  source_value     TEXT NOT NULL,
  canonical_value  TEXT NOT NULL,
  approved         BOOLEAN NOT NULL DEFAULT FALSE,
  UNIQUE (provider, canonical_field, source_value)
);

-- The taxonomy IS the product: reason -> recoverability -> action -> bound.
CREATE TABLE failure_taxonomy (
  failure_reason     TEXT PRIMARY KEY,
  loss_category      TEXT NOT NULL,      -- A1 A5 A6 A7 B1 B2 B3 B4 X_*
  fault_attribution  TEXT NOT NULL CHECK (fault_attribution IN
                       ('BUSINESS_FAULT','CUSTOMER_SENTIMENT',
                        'CUSTOMER_CIRCUMSTANCE','EXTERNAL')),
  claimable          BOOLEAN NOT NULL,   -- may enter the headline number
  retryable          BOOLEAN NOT NULL,
  max_attempts       INT     NOT NULL DEFAULT 0,
  backoff_policy     TEXT,               -- exponential_15m | exponential_30m | payday_aligned | fixed_24h
  default_action     TEXT NOT NULL,      -- RETRY_NOW | RETRY_SCHEDULED | NUDGE | ...
  escalation_action  TEXT,
  attribution_window_seconds INT NOT NULL DEFAULT 259200,  -- 72h default
  notes              TEXT
);

CREATE TABLE unmapped_fields (
  provider     TEXT NOT NULL,
  source_path  TEXT NOT NULL,
  first_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
  occurrences  BIGINT NOT NULL DEFAULT 1,
  sample_value JSONB,
  triaged      BOOLEAN NOT NULL DEFAULT FALSE,  -- feeds a review queue, NEVER auto-DDL
  PRIMARY KEY (provider, source_path)
);

CREATE TABLE reprocessing_runs (
  run_id             BIGSERIAL PRIMARY KEY,
  reason             TEXT NOT NULL,
  from_version       TEXT,
  to_version         TEXT,
  events_affected    INT NOT NULL,
  amount_delta_minor BIGINT,
  ran_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- 4. Canonical entities
-- ---------------------------------------------------------------------
CREATE TABLE orders (
  order_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id     UUID NOT NULL,
  customer_id     UUID REFERENCES customers(customer_id),
  order_status    TEXT NOT NULL,
  subtotal_minor  BIGINT,
  discount_minor  BIGINT,
  tax_minor       BIGINT,
  shipping_minor  BIGINT,
  total_amount_minor BIGINT NOT NULL,
  currency        CHAR(3) NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL,
  completed_at    TIMESTAMPTZ,
  cancelled_at    TIMESTAMPTZ,
  attributes      JSONB NOT NULL DEFAULT '{}',
  raw_event_id    UUID NOT NULL,
  mapping_version TEXT
);
CREATE INDEX ON orders (business_id, customer_id);

-- N attempts against ONE intent. Getting this grain wrong double-counts
-- both the loss and the recovery.
CREATE TABLE payments (
  payment_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id           UUID NOT NULL,
  customer_id           UUID REFERENCES customers(customer_id),
  order_id              UUID REFERENCES orders(order_id),
  payment_intent_id     TEXT NOT NULL,
  attempt_number        INT  NOT NULL DEFAULT 1,
  parent_payment_id     UUID REFERENCES payments(payment_id),
  amount_minor          BIGINT NOT NULL,
  amount_refunded_minor BIGINT NOT NULL DEFAULT 0,
  currency              CHAR(3) NOT NULL,
  payment_status        TEXT NOT NULL,
  payment_method        TEXT,        -- card | upi | netbanking | wallet | emi
  -- Razorpay error envelope, promoted to columns: these drive categorisation.
  failure_code_raw      TEXT,
  failure_code_canonical TEXT REFERENCES failure_taxonomy(failure_reason),
  error_source          TEXT,        -- customer | business | bank | gateway  <- A5 false-decline signal
  error_step            TEXT,        -- payment_authentication | payment_authorization | ...
  error_description     TEXT,
  -- Instrument detail, promoted for A1 "which bank is degrading right now"
  issuer_bank           TEXT,
  card_network           TEXT,
  card_last4            CHAR(4),
  card_expiry_month     SMALLINT,
  card_expiry_year      SMALLINT,
  vpa                   TEXT,
  wallet                TEXT,
  acquirer_data          JSONB,
  initiated_at          TIMESTAMPTZ NOT NULL,
  completed_at          TIMESTAMPTZ,
  attributes            JSONB NOT NULL DEFAULT '{}',
  raw_event_id          UUID NOT NULL,
  mapping_version       TEXT
);
CREATE INDEX ON payments (business_id, payment_intent_id, attempt_number);
CREATE INDEX ON payments (business_id, order_id);
CREATE INDEX ON payments (business_id, failure_code_canonical, initiated_at);
CREATE INDEX ON payments (business_id, issuer_bank, initiated_at)
  WHERE payment_status = 'FAILED';   -- bank-degradation detection

CREATE TABLE checkout_sessions (
  checkout_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id       UUID NOT NULL,
  customer_id       UUID REFERENCES customers(customer_id),
  cart_value_minor  BIGINT NOT NULL,
  currency          CHAR(3) NOT NULL,
  item_count        INT,
  status            TEXT NOT NULL,   -- STARTED|PAYMENT_STARTED|COMPLETED|ABANDONED
  started_at        TIMESTAMPTZ NOT NULL,
  payment_started_at TIMESTAMPTZ,
  completed_at      TIMESTAMPTZ,
  abandoned_at      TIMESTAMPTZ,     -- DERIVED by the sweep job, never received
  -- Shopify hands you this for free; for other platforms we mint our own.
  provider_recovery_url TEXT,
  attributes        JSONB NOT NULL DEFAULT '{}',
  raw_event_id      UUID NOT NULL,
  mapping_version   TEXT
);
CREATE INDEX ON checkout_sessions (business_id, status, started_at);

CREATE TABLE subscriptions (
  subscription_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id         UUID NOT NULL,
  customer_id         UUID REFERENCES customers(customer_id),
  plan_id             TEXT,
  billing_amount_minor BIGINT NOT NULL,
  currency            CHAR(3) NOT NULL,
  billing_frequency   TEXT,
  status              TEXT NOT NULL,
  -- Mandate state decides whether a retry is even POSSIBLE (a stopping rule).
  mandate_type        TEXT,     -- UPI_AUTOPAY | ENACH | CARD_ON_FILE
  mandate_status      TEXT,     -- ACTIVE | PAUSED | REVOKED | EXPIRED
  mandate_max_amount_minor BIGINT,
  mandate_expires_at  TIMESTAMPTZ,
  start_date          DATE,
  next_billing_date   DATE,
  cancelled_at        TIMESTAMPTZ,
  attributes          JSONB NOT NULL DEFAULT '{}',
  raw_event_id        UUID NOT NULL,
  mapping_version     TEXT
);
CREATE INDEX ON subscriptions (business_id, status, next_billing_date);

CREATE TABLE invoices (
  invoice_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id     UUID NOT NULL,
  customer_id     UUID REFERENCES customers(customer_id),
  invoice_number  TEXT,
  amount_minor    BIGINT NOT NULL,
  amount_paid_minor BIGINT NOT NULL DEFAULT 0,   -- partial settlement support
  currency        CHAR(3) NOT NULL,
  payment_terms_days INT,
  issued_at       TIMESTAMPTZ NOT NULL,
  due_at          TIMESTAMPTZ NOT NULL,
  paid_at         TIMESTAMPTZ,
  status          TEXT NOT NULL,   -- ISSUED|OVERDUE|PARTIALLY_PAID|PAID|WRITTEN_OFF
  dunning_stage   INT NOT NULL DEFAULT 0,
  attributes      JSONB NOT NULL DEFAULT '{}',
  raw_event_id    UUID NOT NULL,
  mapping_version TEXT
);
CREATE INDEX ON invoices (business_id, status, due_at);
-- Derived, not stored: days_overdue = EXTRACT(DAY FROM now() - due_at)

CREATE TABLE disputes (
  dispute_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id     UUID NOT NULL,
  payment_id      UUID REFERENCES payments(payment_id),
  amount_minor    BIGINT NOT NULL,
  currency        CHAR(3) NOT NULL,
  reason_code     TEXT,
  status          TEXT NOT NULL,
  respond_by      TIMESTAMPTZ,
  raw_event_id    UUID NOT NULL
);
-- An open dispute is a HARD STOP on contacting that customer about that payment.
CREATE INDEX ON disputes (business_id, payment_id, status);

-- Unified event log across every entity type.
CREATE TABLE revenue_events (
  event_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id     UUID NOT NULL,
  customer_id     UUID,
  event_type      TEXT NOT NULL,
  entity_type     TEXT NOT NULL,
  entity_id       UUID NOT NULL,
  occurred_at     TIMESTAMPTZ NOT NULL,
  received_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  amount_minor    BIGINT,
  currency        CHAR(3),
  source_provider TEXT NOT NULL,
  source_event_id TEXT,
  attributes      JSONB NOT NULL DEFAULT '{}',
  raw_event_id    UUID NOT NULL REFERENCES raw_events(raw_event_id),
  mapping_version TEXT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Processing idempotency: reprocessing the same raw event updates, never duplicates.
CREATE UNIQUE INDEX revenue_events_lineage_uq
  ON revenue_events (raw_event_id, entity_type, entity_id);
CREATE INDEX ON revenue_events (business_id, event_type, occurred_at);
CREATE INDEX ON revenue_events USING GIN (attributes jsonb_path_ops);

-- ---------------------------------------------------------------------
-- 5. THE LOSS LEDGER -- what we detected, why, and how much
--    (this table was missing entirely from v1)
-- ---------------------------------------------------------------------
CREATE TABLE revenue_at_risk (
  at_risk_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id       UUID NOT NULL,
  customer_id       UUID REFERENCES customers(customer_id),
  entity_type       TEXT NOT NULL CHECK (entity_type IN
                      ('PAYMENT','CHECKOUT','INVOICE','SUBSCRIPTION')),
  entity_id         UUID NOT NULL,
  -- amount snapshotted AT DETECTION TIME so later edits can't move the number
  at_risk_minor     BIGINT NOT NULL,
  currency          CHAR(3) NOT NULL,
  -- the research taxonomy, resolved at detection
  loss_category     TEXT NOT NULL,
  fault_attribution TEXT NOT NULL,
  claimable         BOOLEAN NOT NULL,
  failure_reason    TEXT REFERENCES failure_taxonomy(failure_reason),
  detected_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  detection_rule    TEXT NOT NULL,        -- which risk rule fired
  detection_version TEXT NOT NULL,
  status            TEXT NOT NULL DEFAULT 'OPEN'
                    CHECK (status IN ('OPEN','IN_RECOVERY','RECOVERED',
                                      'LOST','EXPIRED','SUPPRESSED')),
  resolved_at       TIMESTAMPTZ,
  source_event_id   UUID REFERENCES revenue_events(event_id),
  raw_event_id      UUID NOT NULL,
  attributes        JSONB NOT NULL DEFAULT '{}'
);
-- One open at-risk record per entity: prevents double-counting the same loss.
CREATE UNIQUE INDEX revenue_at_risk_open_uq
  ON revenue_at_risk (business_id, entity_type, entity_id) WHERE status = 'OPEN';
CREATE INDEX ON revenue_at_risk (business_id, status, detected_at);
CREATE INDEX ON revenue_at_risk (business_id, loss_category, claimable);

-- ---------------------------------------------------------------------
-- 6. BOUNDS -- configurable per business; hard bounds live in code
-- ---------------------------------------------------------------------
CREATE TABLE policy_bounds (
  business_id                UUID PRIMARY KEY REFERENCES businesses(business_id),
  max_attempts_per_entity    INT    NOT NULL DEFAULT 3,
  max_contacts_per_week      INT    NOT NULL DEFAULT 3,
  min_gap_hours              INT    NOT NULL DEFAULT 24,
  max_entities_per_batch     INT    NOT NULL DEFAULT 500,
  max_batch_spend_minor      BIGINT NOT NULL DEFAULT 500000,   -- Rs 5,000 of sends
  max_sends_per_hour         INT    NOT NULL DEFAULT 200,
  -- above this at-risk amount, a human must approve before any action
  human_approval_above_minor BIGINT NOT NULL DEFAULT 5000000,  -- Rs 50,000
  -- automated charging (vs. sending a link) only where a mandate exists
  allow_automated_charge     BOOLEAN NOT NULL DEFAULT FALSE,
  quiet_hours_enforced       BOOLEAN NOT NULL DEFAULT TRUE,
  holdout_percent            INT    NOT NULL DEFAULT 20 CHECK (holdout_percent BETWEEN 0 AND 50),
  policy_version              TEXT   NOT NULL DEFAULT 'bounds@v1',
  updated_at                 TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- DLT/Meta pre-registered templates. Runtime may ONLY send an approved one.
CREATE TABLE message_templates (
  template_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id       UUID NOT NULL,
  channel           TEXT NOT NULL,
  loss_category     TEXT NOT NULL,
  locale            TEXT NOT NULL DEFAULT 'en-IN',   -- 'hi-IN', 'hinglish'
  body              TEXT NOT NULL,                   -- with {{var}} slots
  variables         TEXT[] NOT NULL DEFAULT '{}',
  dlt_template_id   TEXT,        -- TRAI DLT registration id (SMS)
  dlt_header        TEXT,        -- registered sender id
  meta_template_name TEXT,       -- WhatsApp approved template
  approved          BOOLEAN NOT NULL DEFAULT FALSE,
  approved_by       TEXT,
  generated_by      TEXT CHECK (generated_by IN ('human','llm_proposed')),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- SMS and WhatsApp cannot legally send unregistered free-form copy.
ALTER TABLE message_templates ADD CONSTRAINT registered_before_approved CHECK (
  NOT (approved
       AND ((channel = 'SMS'      AND dlt_template_id IS NULL)
         OR (channel = 'WHATSAPP' AND meta_template_name IS NULL)))
);

-- ---------------------------------------------------------------------
-- 7. RECOVERY LEDGERS -- decision, execution, outcome, explanation
-- ---------------------------------------------------------------------
-- Attribution tokens: a click on THIS link proves THIS nudge worked.
-- Without them "customer paid later" gets miscredited to the nudge.
CREATE TABLE recovery_tokens (
  token           TEXT PRIMARY KEY,               -- URL-safe random
  business_id     UUID NOT NULL,
  at_risk_id      UUID NOT NULL REFERENCES revenue_at_risk(at_risk_id),
  attempt_id      UUID,
  target_url      TEXT NOT NULL,                  -- cart / payment link / update-instrument
  issued_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at      TIMESTAMPTZ NOT NULL,
  first_clicked_at TIMESTAMPTZ,
  click_count     INT NOT NULL DEFAULT 0,
  converted_at    TIMESTAMPTZ
);
CREATE INDEX ON recovery_tokens (at_risk_id);

CREATE TABLE recovery_attempts (
  attempt_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id       UUID NOT NULL,
  customer_id       UUID NOT NULL,
  at_risk_id        UUID NOT NULL REFERENCES revenue_at_risk(at_risk_id),
  correlation_id    TEXT NOT NULL,
  attempt_number    INT  NOT NULL,
  -- decision
  strategy          TEXT NOT NULL CHECK (strategy IN
                      ('RETRY_NOW','RETRY_SCHEDULED','NUDGE','REQUEST_NEW_INSTRUMENT',
                       'RECOLLECT_MANDATE','ESCALATE_HUMAN','OPS_ALERT','STOP')),
  channel           TEXT,
  template_id       UUID REFERENCES message_templates(template_id),
  cohort            TEXT NOT NULL CHECK (cohort IN ('TREATMENT','HOLDOUT')),
  policy_version    TEXT NOT NULL,
  bounds_version    TEXT NOT NULL,
  decided_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- compliance evidence, snapshotted so later consent changes cannot rewrite it
  consent_snapshot  JSONB NOT NULL,
  bounds_snapshot   JSONB NOT NULL,
  -- execution
  executed_at       TIMESTAMPTZ,
  suppressed_reason TEXT,            -- NULL when actually sent
  channel_receipt   JSONB,
  delivery_status   TEXT,            -- SENT|DELIVERED|BOUNCED|FAILED
  cost_minor        BIGINT NOT NULL DEFAULT 0,
  -- attribution config, resolved per-category at decision time
  attribution_key_type  TEXT NOT NULL CHECK (attribution_key_type IN
                          ('TOKEN','PAYMENT_INTENT','ORDER','INVOICE','CHECKOUT','SUBSCRIPTION')),
  attribution_key_value TEXT NOT NULL,
  attribution_window_seconds INT NOT NULL,
  attribution_expires_at TIMESTAMPTZ NOT NULL,
  recovery_token    TEXT REFERENCES recovery_tokens(token)
);
CREATE UNIQUE INDEX ON recovery_attempts (correlation_id);
CREATE INDEX ON recovery_attempts (business_id, at_risk_id, attempt_number);
CREATE INDEX ON recovery_attempts (attribution_key_type, attribution_key_value)
  WHERE executed_at IS NOT NULL;

CREATE TABLE recovery_outcomes (
  outcome_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id       UUID NOT NULL,
  at_risk_id        UUID NOT NULL REFERENCES revenue_at_risk(at_risk_id),
  attempt_id        UUID REFERENCES recovery_attempts(attempt_id),  -- NULL for holdout
  cohort            TEXT NOT NULL,
  outcome           TEXT NOT NULL CHECK (outcome IN
                      ('RECOVERED','PARTIALLY_RECOVERED','NOT_RECOVERED','EXPIRED','STOPPED')),
  recovered_minor   BIGINT NOT NULL DEFAULT 0,
  currency          CHAR(3) NOT NULL,
  recovered_at      TIMESTAMPTZ,
  -- WHICH event proved it, and HOW we linked it
  proof_event_id    UUID REFERENCES revenue_events(event_id),
  attribution_method TEXT NOT NULL CHECK (attribution_method IN
                      ('TOKEN_CLICK','PAYMENT_INTENT_MATCH','ORDER_MATCH',
                       'INVOICE_PAID','SUBSCRIPTION_CHARGED','HOLDOUT_BASELINE')),
  attribution_confidence TEXT NOT NULL CHECK (attribution_confidence IN ('STRONG','WEAK')),
  latency_seconds   INT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ON recovery_outcomes (at_risk_id);
CREATE INDEX ON recovery_outcomes (business_id, cohort, outcome);

-- Every rupee explainable. Narrative is GENERATED from a deterministic
-- evidence bundle and STORED -- never regenerated at view time.
CREATE TABLE recovery_explanations (
  explanation_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id      UUID NOT NULL,
  scope            TEXT NOT NULL CHECK (scope IN ('ATTEMPT','AT_RISK','BATCH','SUPPRESSION')),
  at_risk_id       UUID REFERENCES revenue_at_risk(at_risk_id),
  attempt_id       UUID REFERENCES recovery_attempts(attempt_id),
  batch_id         UUID,
  -- the deterministic facts handed to the model
  evidence_bundle  JSONB NOT NULL,
  narrative        TEXT NOT NULL,
  -- guardrail results: every number in `narrative` must appear in the bundle
  numbers_verified BOOLEAN NOT NULL,
  unverified_spans TEXT[],
  model            TEXT NOT NULL,
  prompt_version   TEXT NOT NULL,
  generated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON recovery_explanations (business_id, scope, at_risk_id);

-- A2/A3 have no customer to nudge: the action is an internal alert.
CREATE TABLE ops_alerts (
  alert_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id  UUID NOT NULL,
  alert_type   TEXT NOT NULL,   -- SETTLEMENT_OVERDUE | RECON_MISMATCH | BANK_DEGRADED
  severity     TEXT NOT NULL,
  detail       JSONB NOT NULL,
  amount_minor BIGINT,
  detected_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  acknowledged_at TIMESTAMPTZ,
  resolved_at  TIMESTAMPTZ
);

-- ---------------------------------------------------------------------
-- 8. Batch runs -- the unit the brief judges ("across a batch")
-- ---------------------------------------------------------------------
CREATE TABLE recovery_batches (
  batch_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id         UUID NOT NULL,
  started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at        TIMESTAMPTZ,
  entities_scanned    INT NOT NULL DEFAULT 0,
  at_risk_minor       BIGINT NOT NULL DEFAULT 0,
  decisions_made      INT NOT NULL DEFAULT 0,
  actions_executed    INT NOT NULL DEFAULT 0,
  actions_suppressed  INT NOT NULL DEFAULT 0,
  actions_stopped     INT NOT NULL DEFAULT 0,
  spend_minor         BIGINT NOT NULL DEFAULT 0,
  policy_version      TEXT NOT NULL,
  bounds_version      TEXT NOT NULL,
  dry_run             BOOLEAN NOT NULL
);
