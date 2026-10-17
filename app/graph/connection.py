"""FalkorDB connection."""
from falkordb import FalkorDB

from app.config import get_settings


def get_db() -> FalkorDB:
    settings = get_settings()
    return FalkorDB(host=settings.falkordb_host, port=settings.falkordb_port)


def ping() -> bool:
    """Return True if FalkorDB answers a PING."""
    return bool(get_db().connection.ping())
