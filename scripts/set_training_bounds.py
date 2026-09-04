"""Relaxes policy_bounds for a throwaway training-corpus business.

The live demo's bounds (seed 42) are the product: max_entities_per_batch
500 and a Rs 50,000 human-approval gate are what make the batch report
and the approval-hold story real, so they are never touched. But when the
same bounds are applied to a 39,000-item corpus run they stall it: the
score x amount ordering floats the highest-value items to the top of
every batch, nearly all of them trip the approval gate, and each call
re-scores the entire open pool (O(n), ~51 min at this scale) only to
execute nothing. Raising the bounds for that one disposable business lets
a single scoring pass actually drain the pool.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so this runs as `python scripts/...`

from app.db import SessionLocal
from app.models import PolicyBounds
from seed.generator import GeneratorConfig, business_id_for


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--max-entities-per-batch", type=int, default=45_000)
    parser.add_argument("--human-approval-above-minor", type=int, default=25_000_000)
    args = parser.parse_args()

    business_id = business_id_for(GeneratorConfig(seed=args.seed))
    if args.seed == 42:
        raise SystemExit("refusing to touch the seed=42 demo business's bounds")

    session = SessionLocal()
    try:
        bounds = session.get(PolicyBounds, business_id)
        if bounds is None:
            raise SystemExit(f"no policy_bounds row for business {business_id}")
        print(f"business {business_id}")
        print(f"  before: max_entities_per_batch={bounds.max_entities_per_batch} "
              f"human_approval_above_minor={bounds.human_approval_above_minor}")
        bounds.max_entities_per_batch = args.max_entities_per_batch
        bounds.human_approval_above_minor = args.human_approval_above_minor
        session.commit()
        print(f"  after:  max_entities_per_batch={bounds.max_entities_per_batch} "
              f"human_approval_above_minor={bounds.human_approval_above_minor}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
