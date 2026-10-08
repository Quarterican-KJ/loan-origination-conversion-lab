from pathlib import Path

from fastapi import FastAPI

from loan_lab import __version__
from loan_lab.web import install_web


def health() -> dict[str, str]:
    return {"status": "ok"}


def create_app(database_path: Path | None = None) -> FastAPI:
    """Build the app. The web pages read `database_path` (default: the dev database) read-only."""
    app = FastAPI(title="Loan Origination & Data Conversion Lab", version=__version__)
    app.get("/health")(health)
    install_web(app, database_path)
    return app


app = create_app()
