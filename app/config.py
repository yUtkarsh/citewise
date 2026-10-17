"""Settings loaded from .env. Models are never hard-coded."""
from dataclasses import dataclass
from functools import lru_cache
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    falkordb_host: str
    falkordb_port: int
    model_notation: str
    model_tables: str
    model_clauses: str
    model_agent: str
    model_review: str


@lru_cache
def get_settings() -> Settings:
    return Settings(
        falkordb_host=os.getenv("FALKORDB_HOST", "localhost"),
        falkordb_port=int(os.getenv("FALKORDB_PORT", "6379")),
        model_notation=os.getenv("MODEL_NOTATION", ""),
        model_tables=os.getenv("MODEL_TABLES", ""),
        model_clauses=os.getenv("MODEL_CLAUSES", ""),
        model_agent=os.getenv("MODEL_AGENT", ""),
        model_review=os.getenv("MODEL_REVIEW", ""),
    )
