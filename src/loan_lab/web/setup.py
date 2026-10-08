"""Attach the read-only web interface to a FastAPI app."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from loan_lab.paths import default_database_path
from loan_lab.web.database import DatabaseUnavailable, create_readonly_engine
from loan_lab.web.routes import InvalidQuery, router
from loan_lab.web.templating import STATIC_DIR, templates

SEED_COMMAND = "python -m loan_lab.synthetic --preset demo"


def install_web(app: FastAPI, database_path: Path | None = None) -> None:
    if database_path is None:
        try:
            database_path = default_database_path()
        except FileNotFoundError:
            database_path = None
    app.state.database_path = database_path
    app.state.engine = create_readonly_engine(database_path) if database_path else None

    app.include_router(router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.add_exception_handler(InvalidQuery, _invalid_query)
    app.add_exception_handler(RequestValidationError, _invalid_request)
    app.add_exception_handler(DatabaseUnavailable, _database_unavailable)
    app.add_exception_handler(OperationalError, _database_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)


def _error(
    request: Request, status_code: int, title: str, message: str, hint: str | None = None
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "error.html",
        {"title": title, "message": message, "hint": hint},
        status_code=status_code,
    )


async def _invalid_query(request: Request, exc: Exception) -> HTMLResponse:
    assert isinstance(exc, InvalidQuery)
    return _error(request, 400, "Invalid search or filter", exc.message)


async def _invalid_request(request: Request, exc: Exception) -> HTMLResponse:
    assert isinstance(exc, RequestValidationError)
    fields = sorted({".".join(str(part) for part in error["loc"][1:]) for error in exc.errors()})
    return _error(
        request, 400, "Invalid request", f"Invalid value for: {', '.join(fields) or 'request'}."
    )


async def _database_unavailable(request: Request, exc: Exception) -> HTMLResponse:
    assert isinstance(exc, DatabaseUnavailable)
    return _error(
        request,
        503,
        "Database not found",
        f"The development database does not exist: {exc.path or 'project root not found'}.",
        f"Create it with: {SEED_COMMAND}",
    )


async def _database_error(request: Request, exc: Exception) -> HTMLResponse:
    return _error(
        request,
        503,
        "Database unavailable",
        "The development database could not be read. It may be empty or from an older schema.",
        f"Recreate it with: {SEED_COMMAND} --reset",
    )


async def _http_error(request: Request, exc: Exception) -> HTMLResponse:
    assert isinstance(exc, StarletteHTTPException)
    titles = {404: "Page not found", 405: "Method not allowed"}
    messages = {
        404: "The page you requested does not exist.",
        405: "This interface is read-only. Only GET requests are supported.",
    }
    response = _error(
        request,
        exc.status_code,
        titles.get(exc.status_code, "Request error"),
        messages.get(exc.status_code, str(exc.detail)),
    )
    if exc.headers:
        response.headers.update(exc.headers)
    return response
