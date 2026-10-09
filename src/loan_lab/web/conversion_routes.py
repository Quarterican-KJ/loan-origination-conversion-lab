"""Read-only conversion management pages. Only GET routes; the database is never opened."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from loan_lab.web.conversions import (
    MAX_DISCREPANCIES_SHOWN,
    MAX_ISSUES_SHOWN,
    MAX_LISTED_RUNS,
    MAX_SCANNED_ENTRIES,
    TARGET_TABLES,
    InvalidRunId,
    list_runs,
    load_reconciliation,
    load_run,
)
from loan_lab.web.templating import templates

router = APIRouter()

CONVERSION_COMMAND = "python -m loan_lab.conversion.legacy <source_dir> --run-id <run_id>"


def _evidence_root(request: Request) -> Path | None:
    return request.app.state.evidence_root


def _not_found(request: Request, run_id: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "active": "conversions",
            "title": "Conversion run not found",
            "message": f"There is no conversion run {run_id} in the evidence directory.",
        },
        status_code=404,
    )


@router.get("/conversions", response_class=HTMLResponse, name="conversions")
def conversions(request: Request) -> HTMLResponse:
    listing = list_runs(_evidence_root(request))
    return templates.TemplateResponse(
        request,
        "conversions.html",
        {
            "active": "conversions",
            "listing": listing,
            "max_listed": MAX_LISTED_RUNS,
            "max_scanned": MAX_SCANNED_ENTRIES,
            "conversion_command": CONVERSION_COMMAND,
        },
    )


@router.get("/conversions/{run_id}", response_class=HTMLResponse, name="conversion")
def conversion(request: Request, run_id: str) -> HTMLResponse:
    run = load_run(_evidence_root(request), run_id)
    if run is None:
        return _not_found(request, run_id)
    return templates.TemplateResponse(
        request,
        "conversion_detail.html",
        {
            "active": "conversions",
            "run": run,
            "max_issues": MAX_ISSUES_SHOWN,
            "target_tables": TARGET_TABLES,
        },
    )


@router.get(
    "/conversions/{run_id}/reconciliation",
    response_class=HTMLResponse,
    name="conversion_reconciliation",
)
def conversion_reconciliation(request: Request, run_id: str) -> HTMLResponse:
    view = load_reconciliation(_evidence_root(request), run_id)
    if view is None:
        return _not_found(request, run_id)
    return templates.TemplateResponse(
        request,
        "conversion_reconciliation.html",
        {
            "active": "conversions",
            "view": view,
            "run": view.run,
            "max_discrepancies": MAX_DISCREPANCIES_SHOWN,
        },
    )


__all__ = ["InvalidRunId", "router"]
