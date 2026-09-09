"""python -m app.recovery.run [--seed 42] [--enable] [--live]

Safe by default: recovery_enabled=False and dry_run=True until told
otherwise, matching businesses' own schema defaults. --enable turns on
the business-level switch; --live additionally turns off dry_run so
actions actually dispatch (through SimulatedChannel/EmailChannel) instead
of only being decided and recorded.
"""

import argparse
from datetime import datetime

from app.db import SessionLocal
from app.models import Business
from app.recovery.batch import run_batch
from seed.generator import GeneratorConfig, business_id_for


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one recovery batch: policy -> bounds -> channels.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--enable", action="store_true", help="set recovery_enabled=True for this run")
    parser.add_argument("--live", action="store_true", help="also set dry_run=False (implies --enable)")
    parser.add_argument(
        "--now", type=str, default=None,
        help="ISO8601 timestamp to decide this batch AT, instead of wall-clock now. Backdating it is "
             "what lets a training-corpus run resolve labels: attribution only writes an outcome once "
             "attribution_expires_at has elapsed, so attempts stamped 'now' leave every long-window "
             "category (invoices, subscriptions, instrument updates) permanently unlabeled.",
    )
    args = parser.parse_args()

    now = datetime.fromisoformat(args.now) if args.now else None

    business_id = business_id_for(GeneratorConfig(seed=args.seed))
    session = SessionLocal()
    try:
        if args.enable or args.live:
            business = session.get(Business, business_id)
            business.recovery_enabled = True
            if args.live:
                business.dry_run = False
            session.commit()

        batch = run_batch(session, business_id, now=now)
        session.commit()

        print(f"batch {batch.batch_id}")
        print(f"  dry_run: {batch.dry_run}")
        print(f"  entities_scanned: {batch.entities_scanned}")
        print(f"  decisions_made: {batch.decisions_made}")
        print(f"  actions_executed: {batch.actions_executed}")
        print(f"  actions_suppressed: {batch.actions_suppressed}")
        print(f"  actions_stopped (dry_run/holdout/held): {batch.actions_stopped}")
        print(f"  at_risk_minor scanned: {batch.at_risk_minor}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
