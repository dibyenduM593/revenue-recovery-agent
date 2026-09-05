from fastapi import FastAPI

from app.dashboard import router as dashboard_router
from app.ingest.imports import router as imports_router
from app.ingest.webhooks import router as webhooks_router
from app.live_demo import router as live_demo_router
from app.recovery.api import router as recovery_router
from app.risk.api import router as risk_router

app = FastAPI(title="Revenue Recovery")
app.include_router(imports_router)
app.include_router(webhooks_router)
app.include_router(risk_router)
app.include_router(recovery_router)
app.include_router(dashboard_router)
app.include_router(live_demo_router)


@app.get("/health")
def health():
    return {"status": "ok"}
