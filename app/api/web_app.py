"""The web app until app/api/main.py includes these routers: chat, library and ingestion,
plus the static pages in web/. Run with: uvicorn app.api.web_app:app --reload"""
from fastapi import FastAPI

from app.api.chat_routes import mount_web, router as chat_router
from app.api.ingest_routes import router as ingest_router
from app.api.review_routes import router as review_router
from app.api.session_routes import router as session_router

app = FastAPI(title="CiteWise")
app.include_router(chat_router)
app.include_router(ingest_router)
app.include_router(session_router)
app.include_router(review_router)
mount_web(app)  # last: it serves "/"
