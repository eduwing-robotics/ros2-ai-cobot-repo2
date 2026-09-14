"""Timezone-preserving ORM adapter for SQLite-backed domain tests."""
from datetime import timezone
from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator
from shared.datetime_utils import as_utc


class AwareDateTime(TypeDecorator):
    """Keep PostgreSQL TIMESTAMPTZ behavior and explicit SQLite UTC storage.

    SQLite DATETIME loses offsets. Normalize aware writes before that loss and
    restore the known UTC storage offset on reads, never on application input.
    PostgreSQL retains its driver-provided timezone representation.
    """
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        utc = as_utc(value)
        return utc if dialect.name == "sqlite" else value

    def process_result_value(self, value, dialect):
        if value is not None and dialect.name == "sqlite" and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
