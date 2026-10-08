"""Read-only queries for the web interface. Each page uses a fixed number of SQL statements."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from math import ceil

from sqlalchemy import ColumnElement, Row, and_, func, or_, select
from sqlalchemy.orm import Session, aliased, joinedload, raiseload, selectinload

from loan_lab.models import (
    ApplicationParty,
    ApplicationStatus,
    Borrower,
    Collateral,
    CollateralPledge,
    LoanApplication,
    LoanProduct,
    PartyRole,
)

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
SQLITE_INTEGER_MAX = 2**63 - 1


@dataclass(frozen=True)
class Breakdown:
    key: LoanProduct | ApplicationStatus
    count: int
    volume: Decimal
    share: float


@dataclass(frozen=True)
class DashboardStats:
    application_count: int
    total_volume: Decimal
    average_amount: Decimal | None
    collateral_count: int
    collateral_without_valuation: int
    by_product: list[Breakdown]
    by_status: list[Breakdown]


def dashboard_stats(session: Session) -> DashboardStats:
    product_rows = {
        product: (count, volume)
        for product, count, volume in session.execute(
            select(
                LoanApplication.loan_product,
                func.count(),
                func.sum(LoanApplication.requested_amount),
            ).group_by(LoanApplication.loan_product)
        )
    }
    status_rows = {
        status: (count, volume)
        for status, count, volume in session.execute(
            select(
                LoanApplication.status,
                func.count(),
                func.sum(LoanApplication.requested_amount),
            ).group_by(LoanApplication.status)
        )
    }
    collateral_count, valued_count = session.execute(
        select(func.count(), func.count(Collateral.appraised_value))
    ).one()

    application_count = sum(count for count, _ in product_rows.values())
    total_volume = sum((volume for _, volume in product_rows.values()), ZERO)
    average = (
        (total_volume / application_count).quantize(CENT, rounding=ROUND_HALF_EVEN)
        if application_count
        else None
    )
    return DashboardStats(
        application_count=application_count,
        total_volume=total_volume,
        average_amount=average,
        collateral_count=collateral_count,
        collateral_without_valuation=collateral_count - valued_count,
        by_product=_breakdown(list(LoanProduct), product_rows, application_count),
        by_status=_breakdown(list(ApplicationStatus), status_rows, application_count),
    )


def _breakdown[K: (LoanProduct, ApplicationStatus)](
    keys: list[K], rows: dict[K, tuple[int, Decimal]], total: int
) -> list[Breakdown]:
    result = []
    for key in keys:
        count, volume = rows.get(key, (0, ZERO))
        result.append(Breakdown(key, count, volume or ZERO, count / total if total else 0.0))
    return result


@dataclass(frozen=True)
class DirectoryFilters:
    q: str = ""
    product: LoanProduct | None = None
    status: ApplicationStatus | None = None
    page: int = 1
    page_size: int = 25


@dataclass(frozen=True)
class DirectoryPage:
    rows: list[Row[tuple[int, LoanProduct, Decimal, Decimal, int, ApplicationStatus, str | None]]]
    total: int
    page: int
    page_size: int

    @property
    def page_count(self) -> int:
        return max(1, ceil(self.total / self.page_size))

    @property
    def out_of_range(self) -> bool:
        return self.page > self.page_count

    @property
    def first_index(self) -> int:
        return (self.page - 1) * self.page_size + 1 if self.rows else 0

    @property
    def last_index(self) -> int:
        return (self.page - 1) * self.page_size + len(self.rows)


def application_directory(session: Session, filters: DirectoryFilters) -> DirectoryPage:
    conditions = []
    if filters.product is not None:
        conditions.append(LoanApplication.loan_product == filters.product)
    if filters.status is not None:
        conditions.append(LoanApplication.status == filters.status)
    if filters.q:
        conditions.append(_search_condition(filters.q))

    total = session.scalar(select(func.count()).select_from(LoanApplication).where(*conditions))

    primary_party = aliased(ApplicationParty)
    primary_borrower = aliased(Borrower)
    rows = session.execute(
        select(
            LoanApplication.id,
            LoanApplication.loan_product,
            LoanApplication.requested_amount,
            LoanApplication.interest_rate,
            LoanApplication.term_months,
            LoanApplication.status,
            primary_borrower.legal_name.label("borrower_name"),
        )
        .outerjoin(
            primary_party,
            and_(
                primary_party.application_id == LoanApplication.id,
                primary_party.role == PartyRole.PRIMARY_BORROWER,
            ),
        )
        .outerjoin(primary_borrower, primary_borrower.id == primary_party.borrower_id)
        .where(*conditions)
        .order_by(LoanApplication.id)
        .limit(filters.page_size)
        .offset((filters.page - 1) * filters.page_size)
    ).all()
    return DirectoryPage(list(rows), total or 0, filters.page, filters.page_size)


def _search_condition(q: str) -> ColumnElement[bool]:
    """Match any party's legal name (case-insensitive, wildcards escaped) or an application ID."""
    party = aliased(ApplicationParty)
    borrower = aliased(Borrower)
    name_match = (
        select(party.id)
        .join(borrower, borrower.id == party.borrower_id)
        .where(
            party.application_id == LoanApplication.id,
            borrower.legal_name.icontains(q, autoescape=True),
        )
        .exists()
    )
    application_id = q.removeprefix("#")
    if application_id.isascii() and application_id.isdigit():
        number = int(application_id)
        if number <= SQLITE_INTEGER_MAX:
            return or_(name_match, LoanApplication.id == number)
    return name_match


def application_detail(session: Session, application_id: int) -> LoanApplication | None:
    """Load an application with everything the detail page shows; any other access raises."""
    pledges = selectinload(LoanApplication.collateral_pledges)
    collateral = pledges.joinedload(CollateralPledge.collateral)
    return session.scalars(
        select(LoanApplication)
        .where(LoanApplication.id == application_id)
        .options(
            selectinload(LoanApplication.parties).joinedload(ApplicationParty.borrower),
            collateral.joinedload(Collateral.owner),
            collateral.selectinload(Collateral.liens),
            collateral.selectinload(Collateral.pledges),
            raiseload("*", sql_only=True),
        )
    ).one_or_none()
