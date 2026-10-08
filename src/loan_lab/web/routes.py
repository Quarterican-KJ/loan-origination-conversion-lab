"""Read-only HTML pages. Only GET routes are registered."""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from loan_lab.models import ApplicationStatus, LoanProduct, PartyRole
from loan_lab.web.database import get_session
from loan_lab.web.queries import (
    SQLITE_INTEGER_MAX,
    DirectoryFilters,
    application_detail,
    application_directory,
    dashboard_stats,
)
from loan_lab.web.templating import templates

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]

MAX_SEARCH_LENGTH = 100
MAX_PAGE = 1_000_000
PAGE_SIZES = (10, 25, 50, 100)
DEFAULT_PAGE_SIZE = 25
_ROLE_ORDER = {role: index for index, role in enumerate(PartyRole)}


class InvalidQuery(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


@router.get("/", response_class=HTMLResponse, name="dashboard")
def dashboard(request: Request, session: SessionDep) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "dashboard.html", {"active": "dashboard", "stats": dashboard_stats(session)}
    )


@router.get("/applications", response_class=HTMLResponse, name="applications")
def applications(
    request: Request,
    session: SessionDep,
    q: str = "",
    product: str = "",
    status: str = "",
    page: str = "1",
    page_size: str = str(DEFAULT_PAGE_SIZE),
) -> HTMLResponse:
    filters = parse_directory_filters(q, product, status, page, page_size)
    result = application_directory(session, filters)
    return templates.TemplateResponse(
        request,
        "applications.html",
        {
            "active": "applications",
            "filters": filters,
            "result": result,
            "page_sizes": PAGE_SIZES,
            "page_links": _page_links(filters, result.page_count),
            "query_string": lambda page_number: _query_string(filters, page_number),
        },
        status_code=404 if result.out_of_range else 200,
    )


@router.get("/applications/{application_id}", response_class=HTMLResponse, name="application")
def application(
    request: Request,
    session: SessionDep,
    application_id: Annotated[int, Path(ge=1, le=SQLITE_INTEGER_MAX)],
) -> HTMLResponse:
    loaded = application_detail(session, application_id)
    if loaded is None:
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "title": "Application not found",
                "message": f"There is no loan application with ID {application_id}.",
            },
            status_code=404,
        )
    parties = sorted(loaded.parties, key=lambda p: (_ROLE_ORDER[p.role], p.borrower.legal_name))
    return templates.TemplateResponse(
        request,
        "application_detail.html",
        {
            "active": "applications",
            "application": loaded,
            "parties": parties,
            "party_roles": {party.borrower_id: party.role for party in loaded.parties},
            "pledges": sorted(loaded.collateral_pledges, key=lambda p: p.id),
        },
    )


def parse_directory_filters(
    q: str, product: str, status: str, page: str, page_size: str
) -> DirectoryFilters:
    """Validate raw query-string values. Empty product/status mean "all"."""
    search = q.strip()
    if len(search) > MAX_SEARCH_LENGTH:
        raise InvalidQuery(f"Search text must be at most {MAX_SEARCH_LENGTH} characters.")
    if not search.isprintable():
        raise InvalidQuery("Search text contains unsupported characters.")
    try:
        product_value = LoanProduct(product) if product else None
    except ValueError:
        raise InvalidQuery("Unknown loan product filter.") from None
    try:
        status_value = ApplicationStatus(status) if status else None
    except ValueError:
        raise InvalidQuery("Unknown application status filter.") from None
    page_number = _parse_int(page or "1", "Page", 1, MAX_PAGE)
    size = _parse_int(page_size or str(DEFAULT_PAGE_SIZE), "Page size", 1, max(PAGE_SIZES))
    if size not in PAGE_SIZES:
        raise InvalidQuery(f"Page size must be one of {', '.join(map(str, PAGE_SIZES))}.")
    return DirectoryFilters(search, product_value, status_value, page_number, size)


def _parse_int(raw: str, name: str, minimum: int, maximum: int) -> int:
    if not (raw.isascii() and raw.isdigit() and len(raw) <= len(str(maximum))):
        raise InvalidQuery(f"{name} must be a whole number between {minimum} and {maximum:,}.")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise InvalidQuery(f"{name} must be a whole number between {minimum} and {maximum:,}.")
    return value


def _query_string(filters: DirectoryFilters, page: int) -> str:
    params: dict[str, str | int] = {}
    if filters.q:
        params["q"] = filters.q
    if filters.product:
        params["product"] = filters.product.value
    if filters.status:
        params["status"] = filters.status.value
    if filters.page_size != DEFAULT_PAGE_SIZE:
        params["page_size"] = filters.page_size
    if page != 1:
        params["page"] = page
    return urlencode(params)


def _page_links(filters: DirectoryFilters, page_count: int) -> list[int | None]:
    """Page numbers to show, with None marking a gap: [1, None, 4, 5, 6, None, 20]."""
    current = min(filters.page, page_count)
    wanted = {1, page_count, *range(current - 2, current + 3)}
    pages = sorted(p for p in wanted if 1 <= p <= page_count)
    links: list[int | None] = []
    for number in pages:
        if links and number - (links[-1] or 0) > 1:
            links.append(None)
        links.append(number)
    return links
