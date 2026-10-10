"""Read-only conversion management pages. Only GET routes; the database is never opened."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from loan_lab.web.conversions import (
    MAX_DISCREPANCIES_SHOWN,
    MAX_ISSUES_SHOWN,
    MAX_LISTED_RUNS,
    MAX_REPORT_QUERY_LENGTH,
    MAX_REPORT_ROWS_SHOWN,
    MAX_SCANNED_ENTRIES,
    REPORT_KINDS,
    TARGET_TABLES,
    InvalidRunId,
    list_runs,
    load_reconciliation,
    load_report,
    load_run,
    report_download,
    report_filters,
    report_kind,
)
from loan_lab.web.templating import templates

router = APIRouter()

CONVERSION_COMMAND = "python -m loan_lab.conversion.legacy <source_dir> --run-id <run_id>"


def _evidence_root(request: Request) -> Path | None:
    return request.app.state.evidence_root


def _not_found(request: Request, run_id: str, message: str | None = None) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "active": "conversions",
            "title": "Conversion run not found" if message is None else "Report not available",
            "message": message or f"There is no conversion run {run_id} in the evidence directory.",
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


@router.get(
    "/conversions/{run_id}/reports/{kind}", response_class=HTMLResponse, name="conversion_report"
)
def conversion_report(
    request: Request,
    run_id: str,
    kind: str,
    q: str = "",
    file: str = "",
    rule: str = "",
    dependent: str = "",
) -> HTMLResponse:
    report = report_kind(kind)
    if report is None:
        return _not_found(request, run_id, "Reports are exceptions, exclusions, or warnings.")
    view = load_report(
        _evidence_root(request), run_id, report, report_filters(q, file, rule, dependent)
    )
    if view is None:
        return _not_found(request, run_id)
    return templates.TemplateResponse(
        request,
        "conversion_report.html",
        {
            "active": "conversions",
            "view": view,
            "run": view.run,
            "report_kinds": REPORT_KINDS,
            "max_rows": MAX_REPORT_ROWS_SHOWN,
            "max_query": MAX_REPORT_QUERY_LENGTH,
        },
    )


@router.get("/conversions/{run_id}/reports/{kind}/download", name="conversion_report_download")
def conversion_report_download(request: Request, run_id: str, kind: str) -> Response:
    report = report_kind(kind)
    if report is None:
        return _not_found(request, run_id, "Reports are exceptions, exclusions, or warnings.")
    if load_run(_evidence_root(request), run_id) is None:
        return _not_found(request, run_id)
    data = report_download(_evidence_root(request), run_id, report)
    if data is None:
        return _not_found(
            request, run_id, "This report cannot be downloaded because it does not verify."
        )
    return Response(
        data,
        media_type="text/csv; charset=utf-8",
        headers={
            # run_id matches RUN_ID_PATTERN and kind is one of three names, so both are safe here.
            "Content-Disposition": f'attachment; filename="{run_id}-{report}.csv"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


__all__ = ["InvalidRunId", "router"]
