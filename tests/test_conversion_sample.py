"""Acceptance tests: the sample extract must produce the outcomes in spec section 15."""

import dataclasses
import shutil
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest

from loan_lab.conversion.legacy import (
    ConversionPlan,
    Disposition,
    MappedApplication,
    MappedBorrower,
    Outcome,
    SourceRef,
    plan_conversion,
)
from loan_lab.models.enums import ApplicationStatus, BorrowerType, LoanProduct, PartyRole
from loan_lab.paths import find_project_root

SAMPLE_DIR = find_project_root(Path(__file__)) / "sample_data" / "legacy"
BORROWERS = "borrowers.csv"
APPLICATIONS = "applications.csv"
PARTIES = "application_parties.csv"

L = Outcome.LOAD_ELIGIBLE
X = Outcome.EXCLUDED
XD = Outcome.EXCLUDED_DEPENDENT
R = Outcome.REJECTED
RD = Outcome.REJECTED_DEPENDENT

# Spec 15.1 to 15.3: line -> (key, outcome, rule codes).
EXPECTED_BORROWERS = {
    2: ("00010001", L, ()),
    3: ("00010002", L, ()),
    4: ("00010003", L, ()),
    5: ("00010004", L, ()),
    6: ("00010005", L, ()),
    7: ("00010006", L, ()),
    8: ("00010007", L, ()),
    9: ("00010008", L, ()),
    10: ("00010009", L, ()),
    11: ("00010010", L, ()),
    12: ("00010011", R, ("SV-02",)),
    13: ("00010012", X, ("EX-01",)),
    14: ("00010013", R, ("SV-09",)),
    15: ("00010014", R, ("MP-01",)),
    16: ("00010013", R, ("SV-09",)),
    17: ("00010015", L, ()),
}
EXPECTED_APPLICATIONS = {
    2: ("0000500101", L, ()),
    3: ("0000500102", L, ()),
    4: ("0000500103", L, ()),
    5: ("0000500104", L, ()),
    6: ("0000500105", L, ()),
    7: ("0000500106", L, ()),
    8: ("0000500107", R, ("MP-02",)),
    9: ("0000500108", R, ("SV-05",)),
    10: ("0000500109", R, ("RF-06", "RF-07")),
    11: ("0000500110", X, ("EX-02",)),
    12: ("0000500111", X, ("EX-03",)),
    13: ("0000500112", R, ("RF-06", "RF-07")),
    14: ("0000500113", R, ("RF-06",)),
}
EXPECTED_PARTIES = {
    2: ("0000500101/00010001", L, ()),
    3: ("0000500101/00010002", L, ()),
    4: ("0000500102/00010001", L, ()),
    5: ("0000500102/00010002", L, ()),
    6: ("0000500103/00010003", L, ()),
    7: ("0000500103/00010004", L, ()),
    8: ("0000500104/00010005", L, ()),
    9: ("0000500104/00010006", L, ()),
    10: ("0000500104/00010007", X, ("EX-04",)),
    11: ("0000500105/00010007", L, ()),
    12: ("0000500106/00010010", L, ()),
    13: ("0000500107/00010009", RD, ("RF-05",)),
    14: ("0000500108/00010008", RD, ("RF-05",)),
    15: ("0000500109/00010011", R, ("RF-03",)),
    16: ("0000500109/00010099", R, ("RF-02",)),
    17: ("0000500110/00010008", XD, ("EX-05",)),
    18: ("0000500111/00010015", XD, ("EX-05",)),
    19: ("0000500112/10015", R, ("SV-03",)),
    20: ("0000500113/00010009", RD, ("RF-05",)),
    21: ("0000500199/00010003", R, ("RF-01",)),
}


@pytest.fixture(scope="module")
def plan() -> ConversionPlan:
    return plan_conversion(SAMPLE_DIR)


def _check_rows(plan: ConversionPlan, file: str, expected: dict) -> None:
    rows = plan.rows(file)
    assert [row.ref.line for row in rows] == list(expected)
    for row in rows:
        key, outcome, rules = expected[row.ref.line]
        assert (row.key, row.outcome, tuple(sorted(row.rules))) == (key, outcome, rules), row.ref


def test_customer_outcomes(plan: ConversionPlan) -> None:
    _check_rows(plan, BORROWERS, EXPECTED_BORROWERS)


def test_application_outcomes(plan: ConversionPlan) -> None:
    _check_rows(plan, APPLICATIONS, EXPECTED_APPLICATIONS)


def test_relationship_outcomes(plan: ConversionPlan) -> None:
    _check_rows(plan, PARTIES, EXPECTED_PARTIES)


@pytest.mark.parametrize(
    ("file", "eligible", "excluded", "rejected"),
    [(BORROWERS, 11, 1, 4), (APPLICATIONS, 6, 2, 5), (PARTIES, 10, 3, 7)],
)
def test_disposition_accounting(
    plan: ConversionPlan, file: str, eligible: int, excluded: int, rejected: int
) -> None:
    counts = plan.disposition_counts(file)

    assert dict(counts) == {
        Disposition.ELIGIBLE: eligible,
        Disposition.EXCLUDED: excluded,
        Disposition.REJECTED: rejected,
    }
    assert sum(counts.values()) == plan.control.record_counts[file]


def test_dependent_outcomes_are_counted_separately(plan: ConversionPlan) -> None:
    counts = plan.outcome_counts(PARTIES)

    assert counts[Outcome.REJECTED] == 4
    assert counts[Outcome.REJECTED_DEPENDENT] == 3
    assert counts[Outcome.EXCLUDED] == 1
    assert counts[Outcome.EXCLUDED_DEPENDENT] == 2


def test_amount_totals(plan: ConversionPlan) -> None:
    amounts = plan.amounts

    assert amounts.eligible == Decimal("2281000.00")
    assert amounts.excluded == Decimal("291000.00")
    assert amounts.rejected == Decimal("2895000.00")
    assert amounts.unparseable == 0
    assert amounts.total == plan.control.amount_total == Decimal("5467000.00")
    assert plan.control.amount_total_verified


def test_exception_rows(plan: ConversionPlan) -> None:
    by_file = Counter(row.ref.file for row, _ in plan.issues)

    assert len(plan.issues) == 18
    assert by_file == {BORROWERS: 4, APPLICATIONS: 7, PARTIES: 7}


def test_root_causes_point_to_the_originating_lines(plan: ConversionPlan) -> None:
    def causes(file: str, line: int) -> list[str]:
        return [str(cause) for cause in plan.row(SourceRef(file, line)).root_causes]

    assert causes(APPLICATIONS, 10) == [
        "RF-03 application_parties.csv:15",
        "RF-02 application_parties.csv:16",
    ]
    assert causes(PARTIES, 15) == ["SV-02 borrowers.csv:12"]
    assert causes(APPLICATIONS, 13) == ["SV-03 application_parties.csv:19"]
    assert causes(APPLICATIONS, 14) == ["RF-06 applications.csv:14"]
    assert causes(PARTIES, 13) == ["MP-02 applications.csv:8"]
    assert causes(PARTIES, 14) == ["SV-05 applications.csv:9"]
    assert causes(PARTIES, 20) == ["RF-06 applications.csv:14"]
    assert causes(PARTIES, 17) == ["EX-02 applications.csv:11"]
    assert causes(PARTIES, 18) == ["EX-03 applications.csv:12"]


def test_unit_keys_follow_spec_section_13(plan: ConversionPlan) -> None:
    for line, (appl_no, _, _) in EXPECTED_APPLICATIONS.items():
        assert plan.row(SourceRef(APPLICATIONS, line)).unit_key == appl_no
    for line, (key, _, _) in EXPECTED_PARTIES.items():
        expected = None if line == 21 else key.split("/")[0]
        assert plan.row(SourceRef(PARTIES, line)).unit_key == expected
    assert {row.unit_key for row in plan.borrowers} == {None}


def test_rejected_rows_keep_their_raw_source_line(plan: ConversionPlan) -> None:
    row = plan.row(SourceRef(PARTIES, 16))

    assert row.raw == "0000500109,00010099,GTR"
    assert row.unit_key == "0000500109"
    assert row.issues[0].value == "00010099"


def test_complete_application_mapping(plan: ConversionPlan) -> None:
    unit = next(u for u in plan.units if u.key == "0000500101")

    assert unit.disposition is Disposition.ELIGIBLE
    assert unit.application == MappedApplication(
        source_system_id="0000500101",
        loan_product=LoanProduct.COMMERCIAL_REAL_ESTATE,
        requested_amount=Decimal("1250000.00"),
        interest_rate=Decimal("6.5000"),
        term_months=240,
        status=ApplicationStatus.IN_REVIEW,
        source_system="LEGACY_LOS",
    )
    assert [(p.target.borrower_source_id, p.target.role) for p in unit.parties] == [
        ("00010001", PartyRole.PRIMARY_BORROWER),
        ("00010002", PartyRole.GUARANTOR),
    ]


def test_decimal_values_keep_exact_scale(plan: ConversionPlan) -> None:
    for application in plan.applications_to_load:
        assert application.requested_amount.as_tuple().exponent == -2
        assert application.interest_rate.as_tuple().exponent == -4
    rates = {a.source_system_id: a.interest_rate for a in plan.applications_to_load}
    assert rates["0000500102"] == Decimal("7.1250")


def test_customer_names_and_types(plan: ConversionPlan) -> None:
    customers = {b.source_system_id: b for b in plan.borrowers_to_load}

    assert customers["00010001"] == MappedBorrower(
        "00010001", BorrowerType.BUSINESS, "Cedar Hollow Logistics LLC"
    )
    assert customers["00010002"].legal_name == "Elena R. Marsh"
    assert customers["00010010"].legal_name == "Grace Haverford"
    assert "00010014" not in customers


def test_identifiers_keep_leading_zeros(plan: ConversionPlan) -> None:
    assert all(len(b.source_system_id) == 8 for b in plan.borrowers_to_load)
    assert all(len(a.source_system_id) == 10 for a in plan.applications_to_load)
    assert plan.row(SourceRef(PARTIES, 19)).key == "0000500112/10015"


def test_malformed_key_is_not_matched_to_a_similar_customer(plan: ConversionPlan) -> None:
    delgado = plan.row(SourceRef(BORROWERS, 17))

    assert delgado.outcome is Outcome.LOAD_ELIGIBLE
    assert "0000500112" not in delgado.warnings[0].message
    assert "0000500111 (excluded)" in delgado.warnings[0].message


def test_customers_without_applications(plan: ConversionPlan) -> None:
    assert [row.key for row in plan.customers_without_applications] == [
        "00010008",
        "00010009",
        "00010015",
    ]


def test_loaded_distributions(plan: ConversionPlan) -> None:
    applications = plan.applications_to_load

    assert Counter(a.loan_product for a in applications) == {
        LoanProduct.COMMERCIAL_REAL_ESTATE: 1,
        LoanProduct.COMMERCIAL_TERM: 2,
        LoanProduct.RESIDENTIAL_MORTGAGE: 1,
        LoanProduct.CONSUMER_AUTO: 1,
        LoanProduct.HOME_EQUITY: 1,
    }
    assert Counter(a.status for a in applications) == {
        ApplicationStatus.IN_REVIEW: 2,
        ApplicationStatus.APPROVED: 1,
        ApplicationStatus.SUBMITTED: 1,
        ApplicationStatus.DECLINED: 1,
        ApplicationStatus.DRAFT: 1,
    }
    assert Counter(b.borrower_type for b in plan.borrowers_to_load) == {
        BorrowerType.INDIVIDUAL: 8,
        BorrowerType.BUSINESS: 3,
    }
    assert Counter(p.role for p in plan.parties_to_load) == {
        PartyRole.PRIMARY_BORROWER: 6,
        PartyRole.CO_BORROWER: 1,
        PartyRole.GUARANTOR: 3,
    }


def test_every_eligible_relationship_points_to_eligible_records(plan: ConversionPlan) -> None:
    customers = {b.source_system_id for b in plan.borrowers_to_load}
    applications = {a.source_system_id for a in plan.applications_to_load}

    for party in plan.parties_to_load:
        assert party.borrower_source_id in customers
        assert party.application_source_id in applications
    for application in applications:
        roles = [p.role for p in plan.parties_to_load if p.application_source_id == application]
        assert roles.count(PartyRole.PRIMARY_BORROWER) == 1


def test_unit_outcomes(plan: ConversionPlan) -> None:
    dispositions = Counter(unit.disposition for unit in plan.units)
    signer_unit = next(u for u in plan.units if u.key == "0000500104")

    assert dispositions == {
        Disposition.ELIGIBLE: 6,
        Disposition.EXCLUDED: 2,
        Disposition.REJECTED: 5,
    }
    assert [p.outcome for p in signer_unit.parties] == [L, L, X]
    assert "0000500199" not in {unit.key for unit in plan.units}


def test_unmapped_fields_are_inventoried(plan: ConversionPlan) -> None:
    inventory = {(f.file, f.field): f for f in plan.unmapped_fields}

    appl_date = inventory[(APPLICATIONS, "APPL_DATE")]
    assert dict(appl_date.populated) == {
        Disposition.ELIGIBLE: 6,
        Disposition.EXCLUDED: 2,
        Disposition.REJECTED: 5,
    }
    assert appl_date.nonconforming == 0
    assert set(inventory) == {
        (BORROWERS, "LAST_MAINT_DATE"),
        (APPLICATIONS, "APPL_DATE"),
        (APPLICATIONS, "BRANCH_NO"),
    }


def test_control_totals_and_checksums(plan: ConversionPlan) -> None:
    assert plan.control.extract_date == "20260930"
    assert set(plan.checksums) == {BORROWERS, APPLICATIONS, PARTIES}
    assert all(len(digest) == 64 for digest in plan.checksums.values())


def test_plan_is_immutable(plan: ConversionPlan) -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.borrowers = ()  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.applications[0].disposition = Disposition.REJECTED  # type: ignore[misc]
    with pytest.raises(TypeError):
        plan.control.record_counts[BORROWERS] = 0  # type: ignore[index]
    assert isinstance(plan.units[0].parties, tuple)


def test_planning_writes_nothing(tmp_path: Path) -> None:
    extract = tmp_path / "legacy"
    shutil.copytree(SAMPLE_DIR, extract)
    before = {p.name: p.read_bytes() for p in extract.iterdir()}

    plan_conversion(extract)

    assert {p.name: p.read_bytes() for p in extract.iterdir()} == before
    assert list(tmp_path.iterdir()) == [extract]
