"""FastAPI entry point. Run with: uvicorn app.api.main:app --reload"""
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.graph import connection

app = FastAPI(title="Code Graph Agent")


@app.get("/health")
def health() -> JSONResponse:
    try:
        falkordb_ok = connection.ping()
        error = None
    except Exception as exc:  # connection refused, timeout, etc.
        falkordb_ok = False
        error = str(exc)
    body = {"status": "ok" if falkordb_ok else "degraded", "falkordb": falkordb_ok}
    if error:
        body["error"] = error
    return JSONResponse(body, status_code=200 if falkordb_ok else 503)
