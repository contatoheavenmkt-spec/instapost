"""Utilidades compartilhadas entre módulos — evita duplicação."""
from datetime import datetime, timezone


def now_iso() -> str:
    """Retorna timestamp UTC ISO 8601 com segundos."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    """Parse ISO 8601 string, tolerando com ou sem timezone."""
    if not s:
        raise ValueError("empty ISO string")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
