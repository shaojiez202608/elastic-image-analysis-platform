"""UTC timestamp helpers (ISO 8601 with milliseconds and a Z suffix)."""

from __future__ import annotations

from datetime import UTC, datetime


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(v: str) -> float | None:
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None
