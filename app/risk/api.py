import uuid

from fastapi import APIRouter, HTTPException

from app.db import SessionLocal
from app.risk.lineage import build_lineage
from app.risk.projection import current_projection
from app.settings import DEMO_BUSINESS_ID

router = APIRouter()


@router.get("/v1/at-risk/summary")
def get_at_risk_summary():
    session = SessionLocal()
    try:
        return current_projection(session, DEMO_BUSINESS_ID)
    finally:
        session.close()


@router.get("/v1/at-risk/{at_risk_id}/lineage")
def get_at_risk_lineage(at_risk_id: uuid.UUID):
    session = SessionLocal()
    try:
        lineage = build_lineage(session, at_risk_id)
    finally:
        session.close()

    if lineage is None:
        raise HTTPException(status_code=404, detail=f"no at-risk record {at_risk_id}")
    return lineage
