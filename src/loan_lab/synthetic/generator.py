"""Deterministic synthetic LOS data. Every name, address, and amount is fictional."""

from __future__ import annotations

import random
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field, fields
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from loan_lab.models import (
    ApplicationStatus,
    BorrowerType,
    CollateralType,
    LienStatus,
    LoanProduct,
    PartyRole,
    PledgeStatus,
)

DEFAULT_SEED = 20261008
DEFAULT_BATCH_SIZE = 1_000
PRESETS: dict[str, int] = {"small": 25, "demo": 5_000, "stress": 100_000}

# Fixed so output never depends on the day the generator runs.
AS_OF_DATE = date(2026, 9, 30)
MAX_VALUATION_AGE_DAYS = 540

# Bounded pools of recent borrowers that later applications may reuse; keeps memory flat at any size.
REUSE_POOL_SIZE = 250
MAX_TRACKED_COLLATERAL_PER_BUSINESS = 5

CENT = Decimal("0.01")
RATE_STEP = Decimal("0.125")
RATE_QUANTUM = Decimal("0.0001")

Row = dict[str, Any]


def dollars(units: int) -> Decimal:
    return Decimal(units).quantize(CENT)


def rate_from_eighths(eighths: int) -> Decimal:
    """Annual percentage rate in 1/8-point steps: 49 -> Decimal("6.1250")."""
    return (Decimal(eighths) * RATE_STEP).quantize(RATE_QUANTUM)


@dataclass(frozen=True)
class ProductSpec:
    weight: int
    min_amount: int
    max_amount: int
    amount_step: int
    terms_months: tuple[int, ...]
    min_rate_eighths: int
    max_rate_eighths: int
    commercial: bool
    collateral_type: CollateralType | None
    # Hidden "true" collateral value as a percentage of the requested amount, and rounding step.
    value_percent: tuple[int, int] = (0, 0)
    value_rounding: int = 1

    @property
    def min_rate(self) -> Decimal:
        return rate_from_eighths(self.min_rate_eighths)

    @property
    def max_rate(self) -> Decimal:
        return rate_from_eighths(self.max_rate_eighths)


PRODUCT_SPECS: dict[LoanProduct, ProductSpec] = {
    LoanProduct.CONSUMER_AUTO: ProductSpec(
        25, 8_000, 75_000, 100, (36, 48, 60, 72), 36, 95, False,
        CollateralType.VEHICLE, (100, 135), 50,
    ),
    LoanProduct.CONSUMER_PERSONAL: ProductSpec(
        15, 2_000, 40_000, 100, (12, 24, 36, 48, 60), 64, 159, False, None,
    ),
    LoanProduct.RESIDENTIAL_MORTGAGE: ProductSpec(
        20, 120_000, 1_200_000, 1_000, (180, 240, 360), 42, 62, False,
        CollateralType.REAL_ESTATE, (105, 170), 1_000,
    ),
    LoanProduct.HOME_EQUITY: ProductSpec(
        10, 15_000, 250_000, 500, (60, 120, 180, 240), 52, 84, False,
        CollateralType.REAL_ESTATE, (250, 600), 1_000,
    ),
    LoanProduct.COMMERCIAL_TERM: ProductSpec(
        18, 50_000, 2_000_000, 1_000, (36, 60, 84, 120), 54, 82, True,
        CollateralType.EQUIPMENT, (80, 140), 100,
    ),
    LoanProduct.COMMERCIAL_REAL_ESTATE: ProductSpec(
        12, 250_000, 5_000_000, 5_000, (60, 120, 180, 240, 300), 50, 72, True,
        CollateralType.REAL_ESTATE, (125, 180), 5_000,
    ),
}
_PRODUCTS = list(PRODUCT_SPECS)
_PRODUCT_WEIGHTS = [spec.weight for spec in PRODUCT_SPECS.values()]

_STATUS_WEIGHTS: dict[ApplicationStatus, int] = {
    ApplicationStatus.DRAFT: 10,
    ApplicationStatus.SUBMITTED: 20,
    ApplicationStatus.IN_REVIEW: 25,
    ApplicationStatus.APPROVED: 25,
    ApplicationStatus.DECLINED: 12,
    ApplicationStatus.WITHDRAWN: 8,
}
PLEDGE_STATUS_FOR_APPLICATION: dict[ApplicationStatus, PledgeStatus] = {
    ApplicationStatus.DRAFT: PledgeStatus.PROPOSED,
    ApplicationStatus.SUBMITTED: PledgeStatus.PROPOSED,
    ApplicationStatus.IN_REVIEW: PledgeStatus.PROPOSED,
    ApplicationStatus.APPROVED: PledgeStatus.ACTIVE,
    ApplicationStatus.DECLINED: PledgeStatus.RELEASED,
    ApplicationStatus.WITHDRAWN: PledgeStatus.RELEASED,
}
# Only early-stage applications may still be waiting on an appraisal.
APPRAISAL_PENDING_STATUSES = frozenset({ApplicationStatus.DRAFT, ApplicationStatus.SUBMITTED})
APPRAISAL_PENDING_PERCENT = 40

OAK_RIDGE_NAME = "Oak Ridge Properties LLC"
OAK_RIDGE_GUARANTOR_NAME = "Dana R. Whitfield"
OAK_RIDGE_APPLICATIONS = 2
OAK_RIDGE_CRE_AMOUNT = dollars(500_000)
OAK_RIDGE_EQUIPMENT_AMOUNT = dollars(75_000)
OAK_RIDGE_PROPERTY_VALUE = dollars(725_000)
OAK_RIDGE_EQUIPMENT_VALUE = dollars(150_000)
OAK_RIDGE_FIRST_LIEN_BALANCE = dollars(200_000)

# Share of new collateral owned by a guarantor or co-borrower instead of the primary borrower,
# drawn only when the application has such a party.
LINKED_PARTY_OWNER_PERCENT = 10

_FIRST_NAMES = (
    "Avery", "Blake", "Casey", "Devon", "Elliot", "Finley", "Harper", "Jordan", "Kendall",
    "Logan", "Morgan", "Noel", "Parker", "Quinn", "Reese", "Rowan", "Sage", "Taylor",
    "Emery", "Skyler", "Marlow", "Hollis", "Jules", "Arden",
)
_LAST_NAMES = (
    "Abernathy", "Bellamy", "Calloway", "Delacroix", "Ellsworth", "Fairbanks", "Galloway",
    "Holloway", "Ingram", "Jessup", "Kingsley", "Lockhart", "Merriweather", "Northcott",
    "Pemberton", "Quimby", "Radcliffe", "Stanhope", "Thornbury", "Underhill", "Vance",
    "Whitcombe", "Yardley", "Zeller",
)
_BUSINESS_PREFIXES = (
    "Maple", "Summit", "Bluewater", "Granite", "Prairie", "Lakeshore", "Ironwood",
    "Silverline", "Northgate", "Brightfield", "Copperleaf", "Redstone", "Clearview", "Harborview",
)
_BUSINESS_INDUSTRIES = (
    "Logistics", "Dental Group", "Manufacturing", "Hospitality", "Landscaping", "Construction",
    "Holdings", "Bakery", "Auto Repair", "Veterinary Clinic", "Printing", "Fitness",
)
_BUSINESS_SUFFIXES = ("LLC", "Inc.", "Co.", "LLP")
_STREET_NAMES = (
    "Maple", "Cedar", "Birch", "Willow", "Aspen", "Juniper", "Sycamore", "Linden", "Spruce",
    "Magnolia", "Poplar", "Chestnut",
)
_STREET_SUFFIXES = ("Street", "Avenue", "Lane", "Drive", "Court", "Way")
_VEHICLE_BODIES = ("sedan", "SUV", "pickup truck", "minivan", "hatchback", "crossover")
_RESIDENCE_KINDS = ("single-family residence", "townhome", "condominium unit", "duplex")
_COMMERCIAL_PROPERTY_KINDS = (
    "retail strip center", "light industrial building", "medical office building",
    "12-unit apartment building", "distribution warehouse", "mixed-use storefront",
)
_EQUIPMENT_KINDS = (
    "CNC milling machine", "wheel loader", "commercial kitchen equipment package",
    "packaging line", "forklift fleet (4 units)", "dental imaging suite", "box truck fleet (3 units)",
)
_MORTGAGE_CREDITORS = (
    "Lakeside Example Bank", "Summit Example Mortgage Co.", "Prairie Example Credit Union",
)
_JUNIOR_CREDITORS = ("Harbor Example Savings Bank", "Prairie Example Credit Union")
_EQUIPMENT_CREDITORS = ("Example Equipment Finance LLC", "Northgate Example Leasing Co.")


@dataclass
class Batch:
    """Rows for a group of applications, as dicts ready for Core executemany inserts."""

    borrowers: list[Row] = field(default_factory=list)
    applications: list[Row] = field(default_factory=list)
    application_parties: list[Row] = field(default_factory=list)
    collateral: list[Row] = field(default_factory=list)
    collateral_pledges: list[Row] = field(default_factory=list)
    liens: list[Row] = field(default_factory=list)

    def extend(self, other: Batch) -> None:
        for f in fields(self):
            getattr(self, f.name).extend(getattr(other, f.name))


class SyntheticDataGenerator:
    """Yields batches of at most `batch_size` applications plus their related rows.

    The same (applications, seed) always yields the same rows; batch size only changes how the
    rows are grouped. Row IDs are assigned from 1, so the target tables must start empty.
    """

    def __init__(
        self, applications: int, *, seed: int = DEFAULT_SEED, batch_size: int = DEFAULT_BATCH_SIZE
    ) -> None:
        if applications < OAK_RIDGE_APPLICATIONS:
            raise ValueError(f"applications must be at least {OAK_RIDGE_APPLICATIONS}")
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        self.applications = applications
        self.seed = seed
        self.batch_size = batch_size

    def batches(self) -> Iterator[Batch]:
        state = _GenerationState(random.Random(self.seed))
        batch = Batch()
        for fragment in state.application_fragments(self.applications):
            batch.extend(fragment)
            if len(batch.applications) >= self.batch_size:
                yield batch
                batch = Batch()
        if batch.applications:
            yield batch


@dataclass
class _Business:
    borrower_id: int
    collateral_ids: list[int] = field(default_factory=list)


class _GenerationState:
    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self._last_ids: dict[str, int] = {}
        self._individuals: deque[int] = deque(maxlen=REUSE_POOL_SIZE)
        self._businesses: deque[_Business] = deque(maxlen=REUSE_POOL_SIZE)

    def application_fragments(self, count: int) -> Iterator[Batch]:
        """Yield one Batch per application, containing only rows new to that application."""
        yield from self._oak_ridge_scenario()
        for _ in range(count - OAK_RIDGE_APPLICATIONS):
            yield self._random_application()

    def _oak_ridge_scenario(self) -> Iterator[Batch]:
        first = Batch()
        oak_ridge = self._add_borrower(first, OAK_RIDGE_NAME, BorrowerType.BUSINESS)
        guarantor = self._add_borrower(first, OAK_RIDGE_GUARANTOR_NAME, BorrowerType.INDIVIDUAL)
        cre_loan = self._add_application(
            first, LoanProduct.COMMERCIAL_REAL_ESTATE, OAK_RIDGE_CRE_AMOUNT,
            Decimal("6.8750"), 120, ApplicationStatus.IN_REVIEW,
        )
        self._add_party(first, cre_loan, oak_ridge, PartyRole.PRIMARY_BORROWER)
        self._add_party(first, cre_loan, guarantor, PartyRole.GUARANTOR)
        building = self._add_collateral(
            first, CollateralType.REAL_ESTATE,
            "Synthetic three-story office building, 410 Oak Ridge Parkway",
            OAK_RIDGE_PROPERTY_VALUE, date(2026, 5, 14), oak_ridge,
        )
        self._add_pledge(first, cre_loan, building, OAK_RIDGE_CRE_AMOUNT, PledgeStatus.PROPOSED)
        self._add_lien(
            first, building, "Harbor Example Savings Bank", 1, OAK_RIDGE_FIRST_LIEN_BALANCE,
            LienStatus.ACTIVE,
        )
        yield first

        second = Batch()
        equipment_loan = self._add_application(
            second, LoanProduct.COMMERCIAL_TERM, OAK_RIDGE_EQUIPMENT_AMOUNT,
            Decimal("7.5000"), 84, ApplicationStatus.SUBMITTED,
        )
        self._add_party(second, equipment_loan, oak_ridge, PartyRole.PRIMARY_BORROWER)
        self._add_party(second, equipment_loan, guarantor, PartyRole.GUARANTOR)
        equipment = self._add_collateral(
            second, CollateralType.EQUIPMENT,
            "Synthetic HVAC and building-systems equipment package, 410 Oak Ridge Parkway",
            OAK_RIDGE_EQUIPMENT_VALUE, date(2026, 6, 2), oak_ridge,
        )
        self._add_pledge(
            second, equipment_loan, equipment, OAK_RIDGE_EQUIPMENT_AMOUNT, PledgeStatus.PROPOSED
        )
        self._add_pledge(second, equipment_loan, building, None, PledgeStatus.PROPOSED)
        yield second

    def _random_application(self) -> Batch:
        rng = self.rng
        out = Batch()
        product = rng.choices(_PRODUCTS, weights=_PRODUCT_WEIGHTS)[0]
        spec = PRODUCT_SPECS[product]
        status = rng.choices(list(_STATUS_WEIGHTS), weights=list(_STATUS_WEIGHTS.values()))[0]
        amount = rng.randrange(spec.min_amount, spec.max_amount + 1, spec.amount_step)
        rate = rate_from_eighths(rng.randint(spec.min_rate_eighths, spec.max_rate_eighths))
        term = rng.choice(spec.terms_months)
        application_id = self._add_application(out, product, dollars(amount), rate, term, status)

        party_ids: list[int] = []
        business: _Business | None = None
        if spec.commercial:
            business = self._business(out)
            primary_id = business.borrower_id
            self._add_party(out, application_id, primary_id, PartyRole.PRIMARY_BORROWER)
            party_ids.append(primary_id)
            for _ in range(rng.choices((0, 1, 2), weights=(30, 55, 15))[0]):
                guarantor_id = self._individual(out, exclude=party_ids)
                self._add_party(out, application_id, guarantor_id, PartyRole.GUARANTOR)
                party_ids.append(guarantor_id)
        else:
            primary_id = self._individual(out, exclude=party_ids)
            self._add_party(out, application_id, primary_id, PartyRole.PRIMARY_BORROWER)
            party_ids.append(primary_id)
            if self._chance(35):
                co_borrower_id = self._individual(out, exclude=party_ids)
                self._add_party(out, application_id, co_borrower_id, PartyRole.CO_BORROWER)
                party_ids.append(co_borrower_id)

        if spec.collateral_type is None:
            return out

        pledge_status = PLEDGE_STATUS_FOR_APPLICATION[status]
        if business is not None and business.collateral_ids and self._chance(40):
            shared_id = rng.choice(business.collateral_ids)
            self._add_pledge(out, application_id, shared_id, None, pledge_status)

        owner_id = primary_id
        linked_party_ids = party_ids[1:]
        if linked_party_ids and self._chance(LINKED_PARTY_OWNER_PERCENT):
            owner_id = rng.choice(linked_party_ids)
        collateral_id, appraised = self._new_collateral(out, product, spec, amount, status, owner_id)
        # Only business-owned collateral is offered to that business's later applications, so an
        # owner is always a party on every application its collateral secures.
        if business is not None and owner_id == business.borrower_id:
            business.collateral_ids.append(collateral_id)
            if len(business.collateral_ids) > MAX_TRACKED_COLLATERAL_PER_BUSINESS:
                del business.collateral_ids[0]
        pledged = dollars(min(amount, appraised)) if spec.commercial and appraised else None
        self._add_pledge(out, application_id, collateral_id, pledged, pledge_status)
        return out

    def _new_collateral(
        self,
        out: Batch,
        product: LoanProduct,
        spec: ProductSpec,
        amount: int,
        status: ApplicationStatus,
        owner_id: int,
    ) -> tuple[int, int | None]:
        """Create collateral and any prior liens; returns (id, appraised whole dollars or None)."""
        assert spec.collateral_type is not None
        rng = self.rng
        low, high = spec.value_percent
        step = spec.value_rounding
        value = max(step, amount * rng.randint(low, high) // 100 // step * step)
        valuation_date = AS_OF_DATE - timedelta(days=rng.randint(0, MAX_VALUATION_AGE_DAYS))
        description = self._collateral_description(product, spec.collateral_type)
        pending = status in APPRAISAL_PENDING_STATUSES and self._chance(APPRAISAL_PENDING_PERCENT)
        collateral_id = self._add_collateral(
            out,
            spec.collateral_type,
            description,
            None if pending else dollars(value),
            None if pending else valuation_date,
            owner_id,
        )
        self._existing_liens(out, collateral_id, product, value)
        return collateral_id, None if pending else value

    def _existing_liens(
        self, out: Batch, collateral_id: int, product: LoanProduct, value: int
    ) -> None:
        """Liens held by other creditors before this application; independent of the loan amount."""
        rng = self.rng
        if product is LoanProduct.HOME_EQUITY:
            self._add_lien(
                out, collateral_id, rng.choice(_MORTGAGE_CREDITORS), 1,
                self._balance(value, 30, 65), LienStatus.ACTIVE,
            )
            if self._chance(15):
                released = self._chance(50)
                balance = dollars(0) if released else self._balance(value, 3, 12)
                status = LienStatus.RELEASED if released else LienStatus.ACTIVE
                self._add_lien(out, collateral_id, rng.choice(_JUNIOR_CREDITORS), 2, balance, status)
        elif product is LoanProduct.RESIDENTIAL_MORTGAGE and self._chance(30):
            self._add_lien(
                out, collateral_id, rng.choice(_MORTGAGE_CREDITORS), 1,
                self._balance(value, 40, 75), LienStatus.ACTIVE,
            )
        elif product is LoanProduct.COMMERCIAL_REAL_ESTATE and self._chance(25):
            self._add_lien(
                out, collateral_id, rng.choice(_JUNIOR_CREDITORS), 1,
                self._balance(value, 30, 60), LienStatus.ACTIVE,
            )
        elif product is LoanProduct.COMMERCIAL_TERM and self._chance(10):
            self._add_lien(
                out, collateral_id, rng.choice(_EQUIPMENT_CREDITORS), 1,
                self._balance(value, 10, 40), LienStatus.ACTIVE,
            )

    def _balance(self, value: int, low_percent: int, high_percent: int) -> Decimal:
        cents = value * 100 * self.rng.randint(low_percent, high_percent) // 100
        return Decimal(cents + self.rng.randrange(100)).scaleb(-2)

    def _individual(self, out: Batch, exclude: list[int]) -> int:
        if self._individuals and self._chance(8):
            candidate = self.rng.choice(self._individuals)
            if candidate not in exclude:
                return candidate
        borrower_id = self._add_borrower(out, self._person_name(), BorrowerType.INDIVIDUAL)
        self._individuals.append(borrower_id)
        return borrower_id

    def _business(self, out: Batch) -> _Business:
        if self._businesses and self._chance(20):
            return self.rng.choice(self._businesses)
        business = _Business(
            self._add_borrower(out, self._business_name(), BorrowerType.BUSINESS)
        )
        self._businesses.append(business)
        return business

    def _person_name(self) -> str:
        rng = self.rng
        initial = chr(ord("A") + rng.randrange(26))
        return f"{rng.choice(_FIRST_NAMES)} {initial}. {rng.choice(_LAST_NAMES)}"

    def _business_name(self) -> str:
        rng = self.rng
        return (
            f"{rng.choice(_BUSINESS_PREFIXES)} {rng.choice(_BUSINESS_INDUSTRIES)} "
            f"{rng.choice(_BUSINESS_SUFFIXES)}"
        )

    def _collateral_description(self, product: LoanProduct, collateral_type: CollateralType) -> str:
        rng = self.rng
        if collateral_type is CollateralType.VEHICLE:
            return f"Synthetic {rng.randint(2019, 2027)} {rng.choice(_VEHICLE_BODIES)}"
        if collateral_type is CollateralType.EQUIPMENT:
            return f"Synthetic {rng.choice(_EQUIPMENT_KINDS)}"
        kinds = (
            _COMMERCIAL_PROPERTY_KINDS
            if product is LoanProduct.COMMERCIAL_REAL_ESTATE
            else _RESIDENCE_KINDS
        )
        address = (
            f"{rng.randint(100, 9899)} {rng.choice(_STREET_NAMES)} {rng.choice(_STREET_SUFFIXES)}"
        )
        return f"Synthetic {rng.choice(kinds)}, {address}"

    def _chance(self, percent: int) -> bool:
        return self.rng.randrange(100) < percent

    def _next_id(self, kind: str) -> int:
        self._last_ids[kind] = self._last_ids.get(kind, 0) + 1
        return self._last_ids[kind]

    def _add_borrower(self, out: Batch, legal_name: str, borrower_type: BorrowerType) -> int:
        borrower_id = self._next_id("borrower")
        out.borrowers.append(
            {
                "id": borrower_id,
                "source_system": None,
                "source_system_id": None,
                "legal_name": legal_name,
                "borrower_type": borrower_type,
            }
        )
        return borrower_id

    def _add_application(
        self,
        out: Batch,
        product: LoanProduct,
        requested_amount: Decimal,
        interest_rate: Decimal,
        term_months: int,
        status: ApplicationStatus,
    ) -> int:
        application_id = self._next_id("application")
        out.applications.append(
            {
                "id": application_id,
                "source_system": None,
                "source_system_id": None,
                "loan_product": product,
                "requested_amount": requested_amount,
                "interest_rate": interest_rate,
                "term_months": term_months,
                "status": status,
            }
        )
        return application_id

    def _add_party(self, out: Batch, application_id: int, borrower_id: int, role: PartyRole) -> None:
        out.application_parties.append(
            {
                "id": self._next_id("application_party"),
                "application_id": application_id,
                "borrower_id": borrower_id,
                "role": role,
            }
        )

    def _add_collateral(
        self,
        out: Batch,
        collateral_type: CollateralType,
        description: str,
        appraised_value: Decimal | None,
        valuation_date: date | None,
        owner_id: int | None,
    ) -> int:
        collateral_id = self._next_id("collateral")
        out.collateral.append(
            {
                "id": collateral_id,
                "source_system": None,
                "source_system_id": None,
                "collateral_type": collateral_type,
                "description": description,
                "appraised_value": appraised_value,
                "valuation_date": valuation_date,
                "owner_id": owner_id,
            }
        )
        return collateral_id

    def _add_pledge(
        self,
        out: Batch,
        application_id: int,
        collateral_id: int,
        pledged_amount: Decimal | None,
        status: PledgeStatus,
    ) -> None:
        out.collateral_pledges.append(
            {
                "id": self._next_id("collateral_pledge"),
                "application_id": application_id,
                "collateral_id": collateral_id,
                "pledged_amount": pledged_amount,
                "status": status,
            }
        )

    def _add_lien(
        self,
        out: Batch,
        collateral_id: int,
        creditor_name: str,
        priority: int,
        outstanding_balance: Decimal,
        status: LienStatus,
    ) -> None:
        out.liens.append(
            {
                "id": self._next_id("lien"),
                "collateral_id": collateral_id,
                "creditor_name": creditor_name,
                "priority": priority,
                "outstanding_balance": outstanding_balance,
                "status": status,
            }
        )
