"""FastAPI entry point. Run with: uvicorn app.api.main:app --reload"""
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.api.chat_routes import mount_web, router as chat_router
from app.api.ingest_routes import router as ingest_router
from app.api.review_routes import router as review_router
from app.api.session_routes import router as session_router
from app.graph import connection

app = FastAPI(title="CiteWise")
for router in (chat_router, ingest_router, session_router, review_router):
    app.include_router(router)


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


mount_web(app)  # last: it serves "/" and the static pages in web/
