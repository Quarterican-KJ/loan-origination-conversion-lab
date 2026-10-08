from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from loan_lab.models import (
    ApplicationParty,
    ApplicationStatus,
    Borrower,
    BorrowerType,
    LoanApplication,
    LoanProduct,
    PartyRole,
)


def make_application(**overrides: object) -> LoanApplication:
    values: dict[str, object] = {
        "loan_product": LoanProduct.CONSUMER_AUTO,
        "requested_amount": Decimal("25000.00"),
        "interest_rate": Decimal("6.1250"),
        "term_months": 60,
    }
    values.update(overrides)
    return LoanApplication(**values)


def test_one_borrower_with_two_distinct_applications(session: Session) -> None:
    borrower = Borrower(legal_name="Avery Example", borrower_type=BorrowerType.INDIVIDUAL)
    auto = make_application()
    mortgage = make_application(
        loan_product=LoanProduct.RESIDENTIAL_MORTGAGE,
        requested_amount=Decimal("350000.00"),
        interest_rate=Decimal("5.8750"),
        term_months=360,
    )
    auto.parties.append(ApplicationParty(borrower=borrower, role=PartyRole.PRIMARY_BORROWER))
    mortgage.parties.append(ApplicationParty(borrower=borrower, role=PartyRole.PRIMARY_BORROWER))
    session.add_all([auto, mortgage])
    session.commit()
    session.expire_all()

    loaded = session.scalars(select(Borrower)).one()
    assert [app.loan_product for app in loaded.applications] == [
        LoanProduct.CONSUMER_AUTO,
        LoanProduct.RESIDENTIAL_MORTGAGE,
    ]
    assert loaded.applications[0].id != loaded.applications[1].id
    assert loaded.applications[1].requested_amount == Decimal("350000.00")
    assert loaded.applications[1].interest_rate == Decimal("5.8750")
    assert all(app.status is ApplicationStatus.DRAFT for app in loaded.applications)


def test_application_parties_and_roles(session: Session) -> None:
    primary = Borrower(legal_name="Jordan Sample", borrower_type=BorrowerType.INDIVIDUAL)
    co_borrower = Borrower(legal_name="Casey Sample", borrower_type=BorrowerType.INDIVIDUAL)
    guarantor = Borrower(legal_name="Sample Holdings LLC", borrower_type=BorrowerType.BUSINESS)
    application = make_application(loan_product=LoanProduct.COMMERCIAL_TERM)
    application.parties.extend(
        [
            ApplicationParty(borrower=primary, role=PartyRole.PRIMARY_BORROWER),
            ApplicationParty(borrower=co_borrower, role=PartyRole.CO_BORROWER),
            ApplicationParty(borrower=guarantor, role=PartyRole.GUARANTOR),
        ]
    )
    session.add(application)
    session.commit()
    session.expire_all()

    loaded = session.scalars(select(LoanApplication)).one()
    roles = {party.borrower.legal_name: party.role for party in loaded.parties}
    assert roles == {
        "Jordan Sample": PartyRole.PRIMARY_BORROWER,
        "Casey Sample": PartyRole.CO_BORROWER,
        "Sample Holdings LLC": PartyRole.GUARANTOR,
    }
    assert {b.legal_name for b in loaded.borrowers} == set(roles)
    business = session.scalars(select(Borrower).filter_by(legal_name="Sample Holdings LLC")).one()
    assert business.borrower_type is BorrowerType.BUSINESS
    assert business.applications == [loaded]


def test_borrower_cannot_appear_twice_on_same_application(session: Session) -> None:
    borrower = Borrower(legal_name="Riley Test", borrower_type=BorrowerType.INDIVIDUAL)
    application = make_application()
    application.parties.extend(
        [
            ApplicationParty(borrower=borrower, role=PartyRole.PRIMARY_BORROWER),
            ApplicationParty(borrower=borrower, role=PartyRole.GUARANTOR),
        ]
    )
    session.add(application)
    with pytest.raises(IntegrityError):
        session.commit()


def test_application_allows_only_one_primary_borrower(session: Session) -> None:
    application = make_application()
    for name in ("Morgan One", "Morgan Two"):
        borrower = Borrower(legal_name=name, borrower_type=BorrowerType.INDIVIDUAL)
        application.parties.append(
            ApplicationParty(borrower=borrower, role=PartyRole.PRIMARY_BORROWER)
        )
    session.add(application)
    with pytest.raises(IntegrityError):
        session.commit()


def test_source_ids_are_unique_only_within_a_source_system(session: Session) -> None:
    session.add_all(
        [
            Borrower(
                source_system="LEGACY_A",
                source_system_id="1001",
                legal_name="Taylor Alpha",
                borrower_type=BorrowerType.INDIVIDUAL,
            ),
            Borrower(
                source_system="LEGACY_B",
                source_system_id="1001",
                legal_name="Taylor Beta",
                borrower_type=BorrowerType.INDIVIDUAL,
            ),
            Borrower(legal_name="No Source One", borrower_type=BorrowerType.INDIVIDUAL),
            Borrower(legal_name="No Source Two", borrower_type=BorrowerType.INDIVIDUAL),
        ]
    )
    session.commit()

    session.add(
        Borrower(
            source_system="LEGACY_A",
            source_system_id="1001",
            legal_name="Taylor Duplicate",
            borrower_type=BorrowerType.INDIVIDUAL,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_financial_values_are_stored_exactly(session: Session) -> None:
    application = make_application(
        requested_amount=Decimal("1234567890123.45"), interest_rate=Decimal("0.0001")
    )
    session.add(application)
    session.commit()

    raw = session.execute(
        text("SELECT requested_amount, interest_rate FROM loan_application")
    ).one()
    assert raw == (123456789012345, 1)

    session.expire_all()
    loaded = session.scalars(select(LoanApplication)).one()
    assert loaded.requested_amount == Decimal("1234567890123.45")
    assert loaded.interest_rate == Decimal("0.0001")


@pytest.mark.parametrize(
    "requested_amount",
    [25000.5, Decimal("100.001")],
    ids=["float", "too-many-decimal-places"],
)
def test_inexact_financial_values_are_rejected(
    session: Session, requested_amount: object
) -> None:
    session.add(make_application(requested_amount=requested_amount))
    with pytest.raises(StatementError):
        session.commit()
