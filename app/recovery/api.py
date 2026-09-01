from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

from app.db import SessionLocal
from app.models import RecoveryToken

router = APIRouter()


@router.get("/r/{token}")
def redirect_recovery_token(token: str):
    """A click on this link is STRONG attribution evidence (Day 10): it

    proves this specific nudge, not just "the customer came back anyway".
    Recorded regardless of expiry -- a click on an expired token is still
    evidence worth keeping, attribution just won't credit it as STRONG.
    """
    session = SessionLocal()
    try:
        recovery_token = session.get(RecoveryToken, token)
        if recovery_token is None:
            raise HTTPException(status_code=404, detail="unknown or expired token")

        now = datetime.now(timezone.utc)
        if recovery_token.first_clicked_at is None:
            recovery_token.first_clicked_at = now
        recovery_token.click_count += 1
        target_url = recovery_token.target_url
        session.commit()
    finally:
        session.close()

    return RedirectResponse(url=target_url, status_code=307)
