from fastapi import FastAPI

app = FastAPI(title="Revenue Recovery")


@app.get("/health")
def health():
    return {"status": "ok"}
