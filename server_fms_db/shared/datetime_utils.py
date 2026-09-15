"""Strict conversion of application instants for UTC wire boundaries."""

from datetime import datetime, timezone


def as_utc(value: datetime) -> datetime:
    """Preserve the instant; reject timestamps with no usable UTC offset."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware before UTC conversion.")
    return value.astimezone(timezone.utc)
