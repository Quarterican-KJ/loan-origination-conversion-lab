"""Read-only database access for the web interface."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

from fastapi import Request
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session


class DatabaseUnavailable(Exception):
    def __init__(self, path: Path | None) -> None:
        self.path = path
        super().__init__(f"LOS database not found: {path}")


def create_readonly_engine(path: Path) -> Engine:
    """Engine whose connections cannot write and never create a missing database file.

    SQLite opens the file with mode=ro, and PRAGMA query_only blocks writes on the connection.
    """
    uri = f"{path.resolve().as_uri()}?mode=ro"

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
        connection.execute("PRAGMA query_only = ON")
        return connection

    return create_engine(f"sqlite:///{path.resolve().as_posix()}", creator=connect)


def get_session(request: Request) -> Iterator[Session]:
    path: Path | None = request.app.state.database_path
    if path is None or not path.is_file():
        raise DatabaseUnavailable(path)
    with Session(request.app.state.engine) as session:
        yield session
