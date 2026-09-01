"""At-risk projection: what is at risk RIGHT NOW.

Reads revenue_at_risk WHERE status='OPEN' only -- the ledger Day 5 built
incrementally, one row per loss as it's detected. There is no re-scan of
raw_events or payments here; if the projection is ever wrong, the bug is
in what wrote the ledger, not in this query.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import RevenueAtRisk


def current_projection(session: Session, business_id: uuid.UUID) -> dict:
    total_minor = session.execute(
        select(func.coalesce(func.sum(RevenueAtRisk.at_risk_minor), 0)).where(
            RevenueAtRisk.business_id == business_id, RevenueAtRisk.status == "OPEN"
        )
    ).scalar_one()
    claimable_minor = session.execute(
        select(func.coalesce(func.sum(RevenueAtRisk.at_risk_minor), 0)).where(
            RevenueAtRisk.business_id == business_id, RevenueAtRisk.status == "OPEN", RevenueAtRisk.claimable.is_(True)
        )
    ).scalar_one()

    by_category = session.execute(
        select(
            RevenueAtRisk.loss_category,
            RevenueAtRisk.claimable,
            func.count().label("count"),
            func.sum(RevenueAtRisk.at_risk_minor).label("at_risk_minor"),
        )
        .where(RevenueAtRisk.business_id == business_id, RevenueAtRisk.status == "OPEN")
        .group_by(RevenueAtRisk.loss_category, RevenueAtRisk.claimable)
        .order_by(RevenueAtRisk.loss_category)
    ).all()

    by_entity_type = session.execute(
        select(
            RevenueAtRisk.entity_type,
            func.count().label("count"),
            func.sum(RevenueAtRisk.at_risk_minor).label("at_risk_minor"),
        )
        .where(RevenueAtRisk.business_id == business_id, RevenueAtRisk.status == "OPEN")
        .group_by(RevenueAtRisk.entity_type)
        .order_by(RevenueAtRisk.entity_type)
    ).all()

    return {
        "business_id": business_id,
        "as_of": datetime.now(timezone.utc),
        "total_open_minor": total_minor,
        "claimable_minor": claimable_minor,
        "by_loss_category": [
            {"loss_category": r.loss_category, "claimable": r.claimable, "count": r.count, "at_risk_minor": r.at_risk_minor}
            for r in by_category
        ],
        "by_entity_type": [
            {"entity_type": r.entity_type, "count": r.count, "at_risk_minor": r.at_risk_minor} for r in by_entity_type
        ],
    }
