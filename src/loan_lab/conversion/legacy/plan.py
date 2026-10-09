"""The immutable conversion plan produced by validation and mapping.

A plan records one outcome for every source row and every application conversion unit, plus the
target-shaped values for everything eligible to load. Loading and reconciliation consume it; they
never change it.
"""

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

from loan_lab.conversion.legacy import contract
from loan_lab.conversion.legacy.contract import Rule
from loan_lab.conversion.legacy.source import ControlTotals, SourceRef
from loan_lab.conversion.legacy.transforms import exact_sum
from loan_lab.models.enums import ApplicationStatus, BorrowerType, LoanProduct, PartyRole


class Disposition(StrEnum):
    ELIGIBLE = "eligible"
    EXCLUDED = "excluded"
    REJECTED = "rejected"


class Outcome(StrEnum):
    """Disposition plus whether it was inherited from a related record (spec section 15)."""

    LOAD_ELIGIBLE = "load_eligible"
    EXCLUDED = "excluded"
    EXCLUDED_DEPENDENT = "excluded_dependent"
    REJECTED = "rejected"
    REJECTED_DEPENDENT = "rejected_dependent"


@dataclass(frozen=True)
class Cause:
    """A rule failure at a specific source line: the evidence behind an outcome."""

    rule: Rule
    ref: SourceRef

    def __str__(self) -> str:
        return f"{self.rule} {self.ref}"


@dataclass(frozen=True)
class Issue:
    rule: Rule
    message: str
    field: str | None = None
    value: str | None = None
    # The failures this issue follows from: itself for a direct failure, or related records'
    # failures for a reference or dependent rule.
    causes: tuple[Cause, ...] = ()


@dataclass(frozen=True)
class MappedBorrower:
    source_system_id: str
    borrower_type: BorrowerType
    legal_name: str
    source_system: str = contract.SOURCE_SYSTEM


@dataclass(frozen=True)
class MappedApplication:
    source_system_id: str
    loan_product: LoanProduct
    requested_amount: Decimal
    interest_rate: Decimal
    term_months: int
    status: ApplicationStatus
    source_system: str = contract.SOURCE_SYSTEM


@dataclass(frozen=True)
class MappedParty:
    application_source_id: str
    borrower_source_id: str
    role: PartyRole


type MappedRecord = MappedBorrower | MappedApplication | MappedParty


@dataclass(frozen=True)
class RowResult:
    ref: SourceRef
    # CUST_NO, APPL_NO, or APPL_NO/CUST_NO exactly as extracted ("" if the row was unreadable).
    key: str
    raw: str
    # APPL_NO of the conversion unit a party or application row belongs to.
    unit_key: str | None
    disposition: Disposition
    dependent: bool
    # Rejection or exclusion reasons; empty for eligible rows.
    issues: tuple[Issue, ...]
    warnings: tuple[Issue, ...]
    # Set when the row passed mapping, even if it was later rejected by a reference rule.
    target: MappedRecord | None

    @property
    def outcome(self) -> Outcome:
        match self.disposition, self.dependent:
            case Disposition.ELIGIBLE, _:
                return Outcome.LOAD_ELIGIBLE
            case Disposition.EXCLUDED, False:
                return Outcome.EXCLUDED
            case Disposition.EXCLUDED, True:
                return Outcome.EXCLUDED_DEPENDENT
            case Disposition.REJECTED, False:
                return Outcome.REJECTED
            case _:
                return Outcome.REJECTED_DEPENDENT

    @property
    def rules(self) -> tuple[Rule, ...]:
        return tuple(issue.rule for issue in self.issues)

    @property
    def root_causes(self) -> tuple[Cause, ...]:
        return tuple(dict.fromkeys(cause for issue in self.issues for cause in issue.causes))


@dataclass(frozen=True)
class ApplicationUnit:
    """One APPL_NO with all of its application and party rows (spec section 8.2)."""

    key: str
    applications: tuple[RowResult, ...]
    parties: tuple[RowResult, ...]
    disposition: Disposition

    @property
    def application(self) -> MappedApplication | None:
        if self.disposition is not Disposition.ELIGIBLE:
            return None
        target = self.applications[0].target
        assert isinstance(target, MappedApplication)
        return target

    @property
    def root_causes(self) -> tuple[Cause, ...]:
        return tuple(dict.fromkeys(c for row in self.applications for c in row.root_causes))


@dataclass(frozen=True)
class AmountTotals:
    """REQ_AMT by application disposition. Unparseable amounts are counted, not summed."""

    eligible: Decimal
    excluded: Decimal
    rejected: Decimal
    unparseable: int

    @property
    def total(self) -> Decimal:
        return exact_sum((self.eligible, self.excluded, self.rejected))


@dataclass(frozen=True)
class UnmappedField:
    """Inventory of an intentionally unmapped source field (spec section 6.1)."""

    file: str
    field: str
    populated: Mapping[Disposition, int]
    nonconforming: int


@dataclass(frozen=True)
class ConversionPlan:
    control: ControlTotals
    checksums: Mapping[str, str]
    borrowers: tuple[RowResult, ...]
    applications: tuple[RowResult, ...]
    parties: tuple[RowResult, ...]
    units: tuple[ApplicationUnit, ...]
    amounts: AmountTotals
    unmapped_fields: tuple[UnmappedField, ...]
    # SHA-256 of extract_control.csv; ``checksums`` covers the three data files.
    control_sha256: str

    @property
    def source_checksums(self) -> Mapping[str, str]:
        """SHA-256 of all four source files, as read for planning."""
        return MappingProxyType({**self.checksums, contract.CONTROL_FILE: self.control_sha256})

    def rows(self, file: str) -> tuple[RowResult, ...]:
        return {
            contract.BORROWERS_FILE: self.borrowers,
            contract.APPLICATIONS_FILE: self.applications,
            contract.PARTIES_FILE: self.parties,
        }[file]

    def outcome_counts(self, file: str) -> Mapping[Outcome, int]:
        counts = Counter(row.outcome for row in self.rows(file))
        return MappingProxyType({outcome: counts[outcome] for outcome in Outcome})

    def disposition_counts(self, file: str) -> Mapping[Disposition, int]:
        counts = Counter(row.disposition for row in self.rows(file))
        return MappingProxyType({d: counts[d] for d in Disposition})

    def row(self, ref: SourceRef) -> RowResult:
        return self.rows(ref.file)[ref.line - 2]

    @property
    def borrowers_to_load(self) -> tuple[MappedBorrower, ...]:
        return _eligible_targets(self.borrowers, MappedBorrower)

    @property
    def applications_to_load(self) -> tuple[MappedApplication, ...]:
        return _eligible_targets(self.applications, MappedApplication)

    @property
    def parties_to_load(self) -> tuple[MappedParty, ...]:
        return _eligible_targets(self.parties, MappedParty)

    @property
    def customers_without_applications(self) -> tuple[RowResult, ...]:
        """Eligible customers with no eligible relationship (WN-01; reconciled by RC-10)."""
        return tuple(
            row for row in self.borrowers if any(w.rule is Rule.WN_01 for w in row.warnings)
        )

    @property
    def issues(self) -> tuple[tuple[RowResult, Issue], ...]:
        """Every rejection reason, in file and line order: the future exceptions report."""
        return tuple(
            (row, issue)
            for rows in (self.borrowers, self.applications, self.parties)
            for row in rows
            if row.disposition is Disposition.REJECTED
            for issue in row.issues
        )


def _eligible_targets[T](rows: tuple[RowResult, ...], kind: type[T]) -> tuple[T, ...]:
    targets = []
    for row in rows:
        if row.disposition is Disposition.ELIGIBLE:
            assert isinstance(row.target, kind)
            targets.append(row.target)
    return tuple(targets)
