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
    model_profile: str
    model_global: str  # global pass: inferred edges and merge adjudication (default MODEL_REVIEW)
    clause_crops: str  # always | flagged (SPEC Step 3b.4)
    server_fallback: bool  # send `fallbacks: "default"` on models that support it
    embed_model: str  # local sentence-transformers model for find_entry (runs offline)
    agent_effort: str  # output_config.effort for the chat agent
    agent_max_tool_calls: int  # tool budget for a procedure question (AGENT_MAX_TOOL_CALLS)
    agent_max_tool_calls_simple: int  # tool budget for any other question (AGENT_MAX_TOOL_CALLS_SIMPLE)


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
        model_profile=os.getenv("MODEL_PROFILE", ""),
        model_global=os.getenv("MODEL_GLOBAL") or os.getenv("MODEL_REVIEW", ""),
        clause_crops=os.getenv("CLAUSE_CROPS", "always").strip().lower(),
        server_fallback=os.getenv("SERVER_FALLBACK", "1").strip() not in ("0", "false", "no", ""),
        embed_model=os.getenv("EMBED_MODEL", "BAAI/bge-small-en-v1.5"),
        agent_effort=os.getenv("AGENT_EFFORT", "medium").strip().lower(),
        agent_max_tool_calls=int(os.getenv("AGENT_MAX_TOOL_CALLS", "30")),
        agent_max_tool_calls_simple=int(os.getenv("AGENT_MAX_TOOL_CALLS_SIMPLE", "10")),
    )


def require_model(name: str) -> str:
    """The model id for a settings field such as 'model_profile', or a clear error."""
    model = getattr(get_settings(), name)
    if not model:
        raise RuntimeError(f"{name.upper()} is not set in .env (see .env.example)")
    return model
