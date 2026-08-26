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

RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET")
