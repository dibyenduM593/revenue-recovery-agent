from fastapi import FastAPI

from app.ingest.imports import router as imports_router
from app.ingest.webhooks import router as webhooks_router

app = FastAPI(title="Revenue Recovery")
app.include_router(imports_router)
app.include_router(webhooks_router)


@app.get("/health")
def health():
    return {"status": "ok"}
