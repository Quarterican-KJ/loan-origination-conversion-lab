"""Database engine setup."""

from typing import Any

from sqlalchemy import Engine, create_engine, event


def create_db_engine(url: str, **kwargs: Any) -> Engine:
    """Create an engine; on SQLite, foreign-key enforcement is switched on per connection."""
    engine = create_engine(url, **kwargs)
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


def _enable_sqlite_foreign_keys(dbapi_connection: Any, _connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
