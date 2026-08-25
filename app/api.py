from fastapi import FastAPI

from app.ingest.imports import router as imports_router

app = FastAPI(title="Revenue Recovery")
app.include_router(imports_router)


@app.get("/health")
def health():
    return {"status": "ok"}
