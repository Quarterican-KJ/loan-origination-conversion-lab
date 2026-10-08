from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from loan_lab.models import (
    Borrower,
    BorrowerType,
    Collateral,
    CollateralPledge,
    CollateralType,
    Lien,
    LienStatus,
    LoanApplication,
    LoanProduct,
    PledgeStatus,
)


def make_application(**overrides: object) -> LoanApplication:
    values: dict[str, object] = {
        "loan_product": LoanProduct.COMMERCIAL_TERM,
        "requested_amount": Decimal("500000.00"),
        "interest_rate": Decimal("7.2500"),
        "term_months": 84,
    }
    values.update(overrides)
    return LoanApplication(**values)


def make_collateral(**overrides: object) -> Collateral:
    values: dict[str, object] = {
        "collateral_type": CollateralType.REAL_ESTATE,
        "description": "Synthetic warehouse, 100 Example Way",
        "appraised_value": Decimal("750000.00"),
        "valuation_date": date(2026, 6, 30),
    }
    values.update(overrides)
    return Collateral(**values)


def make_lien(**overrides: object) -> Lien:
    values: dict[str, object] = {
        "creditor_name": "Example Community Lender",
        "priority": 1,
        "outstanding_balance": Decimal("250000.00"),
    }
    values.update(overrides)
    return Lien(**values)


def test_application_with_multiple_collateral_assets(session: Session) -> None:
    application = make_application()
    building = make_collateral()
    truck = make_collateral(
        collateral_type=CollateralType.VEHICLE,
        description="Synthetic 2024 box truck",
        appraised_value=Decimal("48500.50"),
        valuation_date=date(2026, 7, 15),
    )
    application.collateral_pledges.extend(
        [
            CollateralPledge(collateral=building, pledged_amount=Decimal("450000.00")),
            CollateralPledge(collateral=truck, status=PledgeStatus.ACTIVE),
        ]
    )
    session.add(application)
    session.commit()
    session.expire_all()

    loaded = session.scalars(select(LoanApplication)).one()
    assert [c.collateral_type for c in loaded.collateral] == [
        CollateralType.REAL_ESTATE,
        CollateralType.VEHICLE,
    ]
    pledges = {p.collateral.collateral_type: p for p in loaded.collateral_pledges}
    assert pledges[CollateralType.REAL_ESTATE].pledged_amount == Decimal("450000.00")
    assert pledges[CollateralType.REAL_ESTATE].status is PledgeStatus.PROPOSED
    assert pledges[CollateralType.VEHICLE].pledged_amount is None
    assert pledges[CollateralType.VEHICLE].status is PledgeStatus.ACTIVE
    vehicle = pledges[CollateralType.VEHICLE].collateral
    assert vehicle.appraised_value == Decimal("48500.50")
    assert vehicle.valuation_date == date(2026, 7, 15)


def test_same_collateral_secures_multiple_applications(session: Session) -> None:
    owner = Borrower(legal_name="Example Holdings LLC", borrower_type=BorrowerType.BUSINESS)
    building = make_collateral(owner=owner)
    term_loan = make_application()
    line_upgrade = make_application(
        loan_product=LoanProduct.COMMERCIAL_REAL_ESTATE, requested_amount=Decimal("150000.00")
    )
    term_loan.collateral_pledges.append(
        CollateralPledge(collateral=building, pledged_amount=Decimal("500000.00"))
    )
    line_upgrade.collateral_pledges.append(
        CollateralPledge(collateral=building, pledged_amount=Decimal("150000.00"))
    )
    session.add_all([term_loan, line_upgrade])
    session.commit()
    session.expire_all()

    loaded = session.scalars(select(Collateral)).one()
    assert [app.requested_amount for app in loaded.applications] == [
        Decimal("500000.00"),
        Decimal("150000.00"),
    ]
    assert sum(p.pledged_amount for p in loaded.pledges) == Decimal("650000.00")
    assert loaded.owner is not None
    assert loaded.owner.legal_name == "Example Holdings LLC"
    assert loaded.owner.owned_collateral == [loaded]


def test_collateral_owner_is_optional(session: Session) -> None:
    session.add(make_collateral(collateral_type=CollateralType.OTHER, description="Synthetic note"))
    session.commit()

    assert session.scalars(select(Collateral)).one().owner is None


@pytest.mark.parametrize(
    ("appraised_value", "valuation_date"),
    [
        (None, date(2026, 6, 30)),
        (Decimal("750000.00"), None),
        (None, None),
    ],
    ids=["missing appraised value", "missing valuation date", "both missing"],
)
def test_incomplete_valuation_is_stored_as_null(
    session: Session, appraised_value: Decimal | None, valuation_date: date | None
) -> None:
    session.add(make_collateral(appraised_value=appraised_value, valuation_date=valuation_date))
    session.commit()
    session.expire_all()

    loaded = session.scalars(select(Collateral)).one()
    assert loaded.appraised_value == appraised_value
    assert loaded.valuation_date == valuation_date


def test_collateral_without_valuation_can_be_pledged(session: Session) -> None:
    application = make_application()
    application.collateral_pledges.append(
        CollateralPledge(
            collateral=make_collateral(appraised_value=None, valuation_date=None),
            pledged_amount=Decimal("100000.00"),
        )
    )
    session.add(application)
    session.commit()

    assert session.scalars(select(Collateral)).one().appraised_value is None


@pytest.mark.parametrize("valuation_date", [date(2026, 6, 30), None], ids=["dated", "undated"])
@pytest.mark.parametrize("appraised_value", ["0.00", "0", "-0.01", "-750000.00"])
def test_provided_appraised_value_must_be_positive(
    session: Session, appraised_value: str, valuation_date: date | None
) -> None:
    session.add(
        make_collateral(appraised_value=Decimal(appraised_value), valuation_date=valuation_date)
    )
    with pytest.raises(IntegrityError, match="ck_collateral_appraised_value_positive"):
        session.commit()


def test_collateral_with_multiple_liens(session: Session) -> None:
    building = make_collateral()
    building.liens.extend(
        [
            make_lien(creditor_name="First Example Bank", outstanding_balance=Decimal("300000.00")),
            make_lien(
                creditor_name="Second Example Credit Union",
                priority=2,
                outstanding_balance=Decimal("0.00"),
                status=LienStatus.RELEASED,
            ),
            # Same recorded priority as the first lien: allowed, not interpreted as ranking.
            make_lien(creditor_name="Example Tax Authority", outstanding_balance=Decimal("1234.56")),
        ]
    )
    session.add(building)
    session.commit()
    session.expire_all()

    loaded = session.scalars(select(Collateral)).one()
    assert [(l.creditor_name, l.priority, l.outstanding_balance, l.status) for l in loaded.liens] == [
        ("First Example Bank", 1, Decimal("300000.00"), LienStatus.ACTIVE),
        ("Second Example Credit Union", 2, Decimal("0.00"), LienStatus.RELEASED),
        ("Example Tax Authority", 1, Decimal("1234.56"), LienStatus.ACTIVE),
    ]


def test_lien_balance_is_independent_of_loan_amount(session: Session) -> None:
    application = make_application(requested_amount=Decimal("500000.00"))
    building = make_collateral()
    building.liens.append(make_lien(outstanding_balance=Decimal("612345.67")))
    application.collateral_pledges.append(CollateralPledge(collateral=building))
    session.add(application)
    session.commit()

    lien = session.scalars(select(Lien)).one()
    assert lien.outstanding_balance == Decimal("612345.67")
    assert lien.outstanding_balance != application.requested_amount


def test_deleting_application_removes_pledges_but_keeps_collateral(session: Session) -> None:
    application = make_application()
    application.collateral_pledges.append(CollateralPledge(collateral=make_collateral()))
    session.add(application)
    session.commit()

    session.delete(application)
    session.commit()

    assert session.scalar(select(func.count()).select_from(CollateralPledge)) == 0
    assert session.scalar(select(func.count()).select_from(Collateral)) == 1


def test_deleting_collateral_removes_its_liens(session: Session) -> None:
    building = make_collateral()
    building.liens.extend([make_lien(), make_lien(priority=2)])
    session.add(building)
    session.commit()

    session.delete(building)
    session.commit()

    assert session.scalar(select(func.count()).select_from(Lien)) == 0


def _duplicate_pledge(session: Session) -> None:
    application = make_application()
    building = make_collateral()
    application.collateral_pledges.extend(
        [CollateralPledge(collateral=building), CollateralPledge(collateral=building)]
    )
    session.add(application)


def _duplicate_scoped_source_reference(session: Session) -> None:
    session.add_all(
        [
            make_collateral(source_system="LEGACY_A", source_system_id="C-1"),
            make_collateral(source_system="LEGACY_A", source_system_id="C-1"),
        ]
    )


def _lien_on_missing_collateral(session: Session) -> None:
    session.add(make_lien(collateral_id=999))


def _pledge_to_missing_application(session: Session) -> None:
    collateral = make_collateral()
    session.add(collateral)
    session.flush()
    session.add(CollateralPledge(application_id=999, collateral_id=collateral.id))


INVALID_CASES: dict[str, tuple[Callable[[Session], None], str]] = {
    "zero appraised value": (
        lambda s: s.add(make_collateral(appraised_value=Decimal("0.00"))),
        "ck_collateral_appraised_value_positive",
    ),
    "negative appraised value": (
        lambda s: s.add(make_collateral(appraised_value=Decimal("-1.00"))),
        "ck_collateral_appraised_value_positive",
    ),
    "blank description": (
        lambda s: s.add(make_collateral(description="   ")),
        "ck_collateral_description_not_blank",
    ),
    "incomplete source reference": (
        lambda s: s.add(make_collateral(source_system="LEGACY_A")),
        "ck_collateral_source_ref_complete",
    ),
    "duplicate scoped source reference": (
        _duplicate_scoped_source_reference,
        "UNIQUE constraint failed: collateral.source_system, collateral.source_system_id",
    ),
    "negative lien balance": (
        lambda s: s.add(make_collateral(liens=[make_lien(outstanding_balance=Decimal("-0.01"))])),
        "ck_lien_outstanding_balance_non_negative",
    ),
    "zero lien priority": (
        lambda s: s.add(make_collateral(liens=[make_lien(priority=0)])),
        "ck_lien_priority_positive",
    ),
    "negative lien priority": (
        lambda s: s.add(make_collateral(liens=[make_lien(priority=-1)])),
        "ck_lien_priority_positive",
    ),
    "blank creditor name": (
        lambda s: s.add(make_collateral(liens=[make_lien(creditor_name="")])),
        "ck_lien_creditor_name_not_blank",
    ),
    "lien on missing collateral": (_lien_on_missing_collateral, "FOREIGN KEY constraint failed"),
    "zero pledged amount": (
        lambda s: s.add(
            make_application(
                collateral_pledges=[
                    CollateralPledge(collateral=make_collateral(), pledged_amount=Decimal("0.00"))
                ]
            )
        ),
        "ck_collateral_pledge_pledged_amount_positive",
    ),
    "duplicate application-collateral link": (
        _duplicate_pledge,
        "UNIQUE constraint failed: collateral_pledge.application_id, collateral_pledge.collateral_id",
    ),
    "pledge to missing application": (
        _pledge_to_missing_application,
        "FOREIGN KEY constraint failed",
    ),
}


@pytest.mark.parametrize(("build", "message"), INVALID_CASES.values(), ids=INVALID_CASES.keys())
def test_constraint_violations_are_rejected(
    session: Session, build: Callable[[Session], None], message: str
) -> None:
    build(session)
    with pytest.raises(IntegrityError, match=message):
        session.commit()


def test_pledged_collateral_cannot_be_deleted(session: Session) -> None:
    application = make_application()
    building = make_collateral()
    application.collateral_pledges.append(CollateralPledge(collateral=building))
    session.add(application)
    session.commit()

    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        session.execute(delete(Collateral).where(Collateral.id == building.id))


def test_collateral_owner_cannot_be_deleted(session: Session) -> None:
    owner = Borrower(legal_name="Example Owner", borrower_type=BorrowerType.INDIVIDUAL)
    session.add(make_collateral(owner=owner))
    session.commit()

    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        session.execute(delete(Borrower).where(Borrower.id == owner.id))
