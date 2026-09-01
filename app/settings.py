import os
import uuid

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@localhost:5432/revenue_recovery",
)

# This build has exactly one business (see seed/bootstrap.py). Real
# multi-tenant webhook routing -- resolving business_id from the
# provider's own account id, or from a per-business URL segment -- is out
# of scope for a solo demo; every inbound webhook is attributed here.
# Default matches `python -m seed.bootstrap --seed 42`'s deterministic id;
# override if you reseeded with a different --seed.
DEMO_BUSINESS_ID = uuid.UUID(os.environ.get("DEMO_BUSINESS_ID", "6765c159-0551-59dd-bb83-bf5277a98f45"))

# A real deployment must set this from the Razorpay dashboard. The dev
# default lets seed/generate.py's webhook smoke test (and any local
# `curl`ing) work without extra setup -- it is not a secret, it's a fixture.
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "dev_demo_webhook_secret_not_for_prod")

# Test-mode API credentials (Settings -> API Keys in the Razorpay dashboard).
# Unset by default -- anything that calls the real Razorpay API (payment
# link creation, a card_id lookup) must check these are present and fail
# closed/fall back to simulated behavior otherwise, the same fail-closed
# discipline as EmailChannel.
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET")

# Global kill switch, independent of any single business's dry_run flag --
# checked first, before anything else, in bounds.execute_action().
EMERGENCY_STOP = os.environ.get("EMERGENCY_STOP", "false").lower() in ("1", "true", "yes")

# The one real channel (Day 9). Unset by default -- EmailChannel fails
# closed (delivery_status SKIPPED_NOT_CONFIGURED) rather than fake-sending
# when these aren't set. Per the plan, the real send target is the
# developer's own inbox ("email to yourself, proving the pipe"), not
# synthetic customer addresses.
SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
SMTP_FROM = os.environ.get("SMTP_FROM")
DEMO_RECIPIENT_EMAIL = os.environ.get("DEMO_RECIPIENT_EMAIL")

# Day 11: explainability. Unset by default -- explain.py fails closed to a
# deterministic template narrative (never fabricates, never blocks the demo)
# rather than skipping explanation entirely when no key is configured.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
EXPLAIN_MODEL = os.environ.get("EXPLAIN_MODEL", "claude-haiku-4-5")
