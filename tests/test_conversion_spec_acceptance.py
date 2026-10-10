"""Specification acceptance: every sample application against spec section 15, independently.

Reconciliation determines dispositions independently of the planner (RC-11, RC-12), but both are
written from the same specification. These tests guard against an error shared by both for the
sample extract: the expected dispositions, rule codes, root causes, unit membership, amounts, and
loaded relationships below are transcribed by hand from docs/conversion-specification.md
sections 5.4, 15.2, 15.3, and 15.5. Nothing here is computed from the planner's output.
"""

import shutil
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

import pytest

from loan_lab.conversion.legacy import (
    ConversionPlan,
    Disposition,
    RowResult,
    SourceRef,
    contract,
    plan_conversion,
    reconcile_run,
    run_conversion,
)
from loan_lab.conversion.legacy import plan as plan_module
from loan_lab.conversion.legacy.loader import DATABASE_NAME
from loan_lab.paths import find_project_root

SAMPLE_DIR = find_project_root(Path(__file__)) / "sample_data" / "legacy"
BORROWERS = "borrowers.csv"
APPLICATIONS = "applications.csv"
PARTIES = "application_parties.csv"

# Spec 15 outcome terms.
LOADED = "loaded"
EXCLUDED = "excluded"
EXCLUDED_DEPENDENT = "excluded (dependent)"
REJECTED = "rejected"
REJECTED_DEPENDENT = "rejected (dependent)"

# Spec 5.4: relationship codes to target roles.
ROLES = {"PRI": "primary_borrower", "COB": "co_borrower", "GTR": "guarantor"}


class RootCause(StrEnum):
    """Why an application (and so its conversion unit) is not loaded, per spec 15.2."""

    NONE = "none"
    # Out of scope by an exclusion rule on the application itself.
    EXCLUSION = "exclusion"
    # The application row fails one of its own field or mapping rules.
    OWN_FIELD = "own_field"
    # A relationship row of the unit is invalid (RF-07), so the unit has no valid primary (RF-06).
    INVALID_RELATIONSHIP = "invalid_relationship"
    # All relationships are valid, but there is no primary borrower (RF-06 only).
    NO_PRIMARY_BORROWER = "no_primary_borrower"


@dataclass(frozen=True)
class SpecParty:
    cust_no: str
    rel_cd: str
    outcome: str
    rules: frozenset[str] = frozenset()
    # (rule, file, line) of the originating failure, when the spec names one.
    causes: frozenset[tuple[str, str, int]] = frozenset()


@dataclass(frozen=True)
class SpecApplication:
    line: int
    outcome: str
    rules: frozenset[str]
    root_cause: RootCause
    causes: frozenset[tuple[str, str, int]]
    # Spec 15.5.
    amount: Decimal
    # Spec 15.3, by line.
    parties: Mapping[int, SpecParty]


def _rules(*codes: str) -> frozenset[str]:
    return frozenset(codes)


SPEC_APPLICATIONS: dict[str, SpecApplication] = {
    "0000500101": SpecApplication(
        2, LOADED, _rules(), RootCause.NONE, frozenset(), Decimal("1250000.00"),
        {2: SpecParty("00010001", "PRI", LOADED), 3: SpecParty("00010002", "GTR", LOADED)},
    ),
    "0000500102": SpecApplication(
        3, LOADED, _rules(), RootCause.NONE, frozenset(), Decimal("180000.00"),
        {4: SpecParty("00010001", "PRI", LOADED), 5: SpecParty("00010002", "GTR", LOADED)},
    ),
    "0000500103": SpecApplication(
        4, LOADED, _rules(), RootCause.NONE, frozenset(), Decimal("412500.00"),
        {6: SpecParty("00010003", "PRI", LOADED), 7: SpecParty("00010004", "COB", LOADED)},
    ),
    "0000500104": SpecApplication(
        5, LOADED, _rules(), RootCause.NONE, frozenset(), Decimal("350000.00"),
        {
            8: SpecParty("00010005", "PRI", LOADED),
            9: SpecParty("00010006", "GTR", LOADED),
            10: SpecParty(
                "00010007", "SGN", EXCLUDED, _rules("EX-04"),
                frozenset({("EX-04", PARTIES, 10)}),
            ),
        },
    ),
    "0000500105": SpecApplication(
        6, LOADED, _rules(), RootCause.NONE, frozenset(), Decimal("28500.00"),
        {11: SpecParty("00010007", "PRI", LOADED)},
    ),
    "0000500106": SpecApplication(
        7, LOADED, _rules(), RootCause.NONE, frozenset(), Decimal("60000.00"),
        {12: SpecParty("00010010", "PRI", LOADED)},
    ),
    "0000500107": SpecApplication(
        8, REJECTED, _rules("MP-02"), RootCause.OWN_FIELD,
        frozenset({("MP-02", APPLICATIONS, 8)}), Decimal("0.00"),
        {
            13: SpecParty(
                "00010009", "PRI", REJECTED_DEPENDENT, _rules("RF-05"),
                frozenset({("MP-02", APPLICATIONS, 8)}),
            ),
        },
    ),
    "0000500108": SpecApplication(
        9, REJECTED, _rules("SV-05"), RootCause.OWN_FIELD,
        frozenset({("SV-05", APPLICATIONS, 9)}), Decimal("2400000.00"),
        {
            14: SpecParty(
                "00010008", "PRI", REJECTED_DEPENDENT, _rules("RF-05"),
                frozenset({("SV-05", APPLICATIONS, 9)}),
            ),
        },
    ),
    "0000500109": SpecApplication(
        10, REJECTED, _rules("RF-06", "RF-07"), RootCause.INVALID_RELATIONSHIP,
        frozenset({("RF-03", PARTIES, 15), ("RF-02", PARTIES, 16)}), Decimal("95000.00"),
        {
            15: SpecParty(
                "00010011", "PRI", REJECTED, _rules("RF-03"),
                frozenset({("SV-02", BORROWERS, 12)}),
            ),
            16: SpecParty(
                "00010099", "GTR", REJECTED, _rules("RF-02"),
                frozenset({("RF-02", PARTIES, 16)}),
            ),
        },
    ),
    "0000500110": SpecApplication(
        11, EXCLUDED, _rules("EX-02"), RootCause.EXCLUSION,
        frozenset({("EX-02", APPLICATIONS, 11)}), Decimal("250000.00"),
        {
            17: SpecParty(
                "00010008", "PRI", EXCLUDED_DEPENDENT, _rules("EX-05"),
                frozenset({("EX-02", APPLICATIONS, 11)}),
            ),
        },
    ),
    "0000500111": SpecApplication(
        12, EXCLUDED, _rules("EX-03"), RootCause.EXCLUSION,
        frozenset({("EX-03", APPLICATIONS, 12)}), Decimal("41000.00"),
        {
            18: SpecParty(
                "00010015", "PRI", EXCLUDED_DEPENDENT, _rules("EX-05"),
                frozenset({("EX-03", APPLICATIONS, 12)}),
            ),
        },
    ),
    "0000500112": SpecApplication(
        13, REJECTED, _rules("RF-06", "RF-07"), RootCause.INVALID_RELATIONSHIP,
        frozenset({("SV-03", PARTIES, 19)}), Decimal("15000.00"),
        {
            19: SpecParty(
                "10015", "PRI", REJECTED, _rules("SV-03"),
                frozenset({("SV-03", PARTIES, 19)}),
            ),
        },
    ),
    "0000500113": SpecApplication(
        14, REJECTED, _rules("RF-06"), RootCause.NO_PRIMARY_BORROWER,
        frozenset({("RF-06", APPLICATIONS, 14)}), Decimal("385000.00"),
        {
            20: SpecParty(
                "00010009", "COB", REJECTED_DEPENDENT, _rules("RF-05"),
                frozenset({("RF-06", APPLICATIONS, 14)}),
            ),
        },
    ),
}


# --- Comparing an implementation against the constants ----------------------------------------


def outcome_of(row: RowResult) -> str:
    match row.disposition, row.dependent:
        case Disposition.ELIGIBLE, _:
            return LOADED
        case Disposition.EXCLUDED, False:
            return EXCLUDED
        case Disposition.EXCLUDED, True:
            return EXCLUDED_DEPENDENT
        case Disposition.REJECTED, False:
            return REJECTED
        case _:
            return REJECTED_DEPENDENT


def causes_of(row: RowResult) -> frozenset[tuple[str, str, int]]:
    return frozenset((str(c.rule), c.ref.file, c.ref.line) for c in row.root_causes)


def root_cause_of(row: RowResult) -> RootCause:
    """Classify an application row by the definitions above, from its outcome and causes."""
    if row.disposition is Disposition.ELIGIBLE:
        return RootCause.NONE
    if row.disposition is Disposition.EXCLUDED:
        return RootCause.EXCLUSION
    causes = causes_of(row)
    if any(file != APPLICATIONS for _, file, _ in causes):
        return RootCause.INVALID_RELATIONSHIP
    if {str(rule) for rule in row.rules} == {"RF-06"}:
        return RootCause.NO_PRIMARY_BORROWER
    return RootCause.OWN_FIELD


def spec_mismatches(plan: ConversionPlan, appl_no: str) -> list[str]:
    """Every way the plan's treatment of one sample application differs from spec section 15."""
    spec = SPEC_APPLICATIONS[appl_no]
    problems = []

    def compare(what: str, expected: object, actual: object) -> None:
        if expected != actual:
            problems.append(f"{appl_no} {what}: spec {expected!r}, plan {actual!r}")

    head = plan.row(SourceRef(APPLICATIONS, spec.line))
    compare("key", appl_no, head.key)
    # Spec 13 and 14.2: every row of the unit carries UNIT_KEY = APPL_NO.
    compare("unit key", appl_no, head.unit_key)
    compare("outcome", spec.outcome, outcome_of(head))
    compare("rules", spec.rules, frozenset(str(rule) for rule in head.rules))
    compare("root cause", str(spec.root_cause), str(root_cause_of(head)))
    compare("root cause lines", spec.causes, causes_of(head))

    units = [unit for unit in plan.units if unit.key == appl_no]
    compare("conversion units", 1, len(units))
    if units:
        compare("unit outcome", spec.outcome, outcome_of(units[0].applications[0]))
        compare("unit party lines", sorted(spec.parties), [r.ref.line for r in units[0].parties])
    for line, party in spec.parties.items():
        row = plan.row(SourceRef(PARTIES, line))
        where = f"party line {line}"
        compare(f"{where} key", f"{appl_no}/{party.cust_no}", row.key)
        compare(f"{where} unit key", appl_no, row.unit_key)
        compare(f"{where} outcome", party.outcome, outcome_of(row))
        compare(f"{where} rules", party.rules, frozenset(str(rule) for rule in row.rules))
        compare(f"{where} root cause lines", party.causes, causes_of(row))
    return problems


def assert_matches_spec(plan: ConversionPlan, appl_no: str) -> None:
    problems = spec_mismatches(plan, appl_no)
    assert not problems, "\n".join(problems)


def target_mismatches(database: Path) -> list[str]:
    """Compare the loaded target with spec 15: presence, amount, and (CUST_NO, role) sets."""
    with closing(sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        amounts = dict(conn.execute(
            "SELECT source_system_id, requested_amount FROM loan_application"
        ))
        relationships: dict[str, set[tuple[str, str]]] = {}
        for appl_no, cust_no, role in conn.execute(
            "SELECT a.source_system_id, b.source_system_id, p.role FROM application_party p "
            "JOIN loan_application a ON a.id = p.application_id "
            "JOIN borrower b ON b.id = p.borrower_id"
        ):
            relationships.setdefault(appl_no, set()).add((cust_no, role))

    problems = []
    for appl_no, spec in SPEC_APPLICATIONS.items():
        if spec.outcome != LOADED:
            if appl_no in amounts:
                problems.append(f"{appl_no} is {spec.outcome} in the spec but was loaded.")
            continue
        if appl_no not in amounts:
            problems.append(f"{appl_no} is loaded in the spec but is not in the target.")
            continue
        # requested_amount is stored as an exact integer number of cents.
        stored = Decimal(amounts[appl_no]).scaleb(-2)
        if stored != spec.amount:
            problems.append(f"{appl_no} amount: spec {spec.amount}, target {stored}")
        expected = {
            (p.cust_no, ROLES[p.rel_cd]) for p in spec.parties.values() if p.outcome == LOADED
        }
        if relationships.get(appl_no, set()) != expected:
            problems.append(
                f"{appl_no} relationships: spec {sorted(expected)}, "
                f"target {sorted(relationships.get(appl_no, set()))}"
            )
    extra = set(amounts) - set(SPEC_APPLICATIONS)
    if extra:
        problems.append(f"Applications not in the spec were loaded: {sorted(extra)}")
    return problems


# --- The constants themselves -------------------------------------------------------------------


def test_constants_cover_the_thirteen_applications_and_spec_totals() -> None:
    assert len(SPEC_APPLICATIONS) == 13
    lines = [spec.line for spec in SPEC_APPLICATIONS.values()]
    assert lines == list(range(2, 15))

    def total(outcome: str) -> Decimal:
        return sum(
            (s.amount for s in SPEC_APPLICATIONS.values() if s.outcome == outcome), Decimal(0)
        )

    # Spec 15.4 and 15.5.
    assert total(LOADED) == Decimal("2281000.00")
    assert total(EXCLUDED) == Decimal("291000.00")
    assert total(REJECTED) == Decimal("2895000.00")
    outcomes = [s.outcome for s in SPEC_APPLICATIONS.values()]
    assert (outcomes.count(LOADED), outcomes.count(EXCLUDED), outcomes.count(REJECTED)) == (6, 2, 5)
    # Party line 21 (0000500199) is the only relationship outside the 13 units.
    party_lines = sorted(line for s in SPEC_APPLICATIONS.values() for line in s.parties)
    assert party_lines == list(range(2, 21))


# --- The planner and the loaded target against the spec -------------------------------------


@pytest.fixture(scope="module")
def plan() -> ConversionPlan:
    return plan_conversion(SAMPLE_DIR)


@pytest.mark.parametrize("appl_no", list(SPEC_APPLICATIONS))
def test_application_matches_spec(plan: ConversionPlan, appl_no: str) -> None:
    assert_matches_spec(plan, appl_no)


def test_orphan_relationship_matches_spec(plan: ConversionPlan) -> None:
    row = plan.row(SourceRef(PARTIES, 21))

    assert (row.key, outcome_of(row), {str(r) for r in row.rules}) == (
        "0000500199/00010003", REJECTED, {"RF-01"},
    )
    assert "0000500199" not in {unit.key for unit in plan.units}


def convert_and_reconcile(tmp_path: Path) -> tuple[Path, object]:
    source = tmp_path / "source"
    shutil.copytree(SAMPLE_DIR, source)
    databases = tmp_path / "data" / "conversion"
    evidence = tmp_path / "output" / "conversion"
    run_conversion(source, "run-1", conversion_root=databases, evidence_root=evidence)
    result = reconcile_run("run-1", evidence_root=evidence)
    return databases / "run-1" / DATABASE_NAME, result


def test_loaded_target_matches_spec(tmp_path: Path) -> None:
    database, result = convert_and_reconcile(tmp_path)

    assert result.passed
    assert target_mismatches(database) == []


# --- Injected planner misclassifications must fail these tests ------------------------------


def test_wrongly_rejected_application_fails_acceptance_and_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A planner defect that rejects the valid 60-month 0000500105 as out of range. It is the only
    # application spec 15.2 loads with a term under 61 months.
    monkeypatch.setattr(contract, "MIN_TERM_MONTHS", 61)
    defective = plan_conversion(SAMPLE_DIR)

    with pytest.raises(AssertionError, match="0000500105 outcome: spec 'loaded', plan 'rejected'"):
        assert_matches_spec(defective, "0000500105")

    database, result = convert_and_reconcile(tmp_path)

    # Reconciliation determines dispositions independently (spec 12.3), so RC-11 names the defect.
    assert not result.passed
    assert "RC-11" in {str(rule) for rule in result.failed_rules}
    wrong = {
        (d.check, d.file, d.line) for d in result.discrepancies
        if d.rule == "RC-11" and d.line is not None
    }
    assert wrong == {
        ("wrongly_rejected", APPLICATIONS, 6), ("missing_from_target", APPLICATIONS, 6),
        ("wrongly_rejected", PARTIES, 11), ("missing_from_target", PARTIES, 11),
    }
    assert target_mismatches(database) == [
        "0000500105 is loaded in the spec but is not in the target."
    ]


def test_exclusion_misclassified_as_rejection_fails_acceptance_and_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A planner defect that forgets product 330 is out of scope, so it is rejected as unmapped.
    monkeypatch.setattr(contract, "EXCLUDED_PRODUCTS", frozenset({"900"}))
    defective = plan_conversion(SAMPLE_DIR)

    problems = spec_mismatches(defective, "0000500110")

    assert "0000500110 outcome: spec 'excluded', plan 'rejected'" in problems
    assert "0000500110 root cause: spec 'exclusion', plan 'own_field'" in problems
    assert any(p.startswith("0000500110 party line 17 outcome") for p in problems)

    _, result = convert_and_reconcile(tmp_path)

    assert not result.passed
    flagged = {
        (d.file, d.line) for d in result.discrepancies if d.check == "rejected_despite_exclusion"
    }
    assert flagged == {(APPLICATIONS, 11), (PARTIES, 17)}


def test_misattributed_root_cause_fails_acceptance(
    plan: ConversionPlan, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A planner defect that keeps only the first root cause of a multi-cause rejection.
    real = plan_module.RowResult.root_causes.fget
    monkeypatch.setattr(
        plan_module.RowResult, "root_causes", property(lambda row: real(row)[:1])
    )

    with pytest.raises(AssertionError, match="0000500109 root cause lines"):
        assert_matches_spec(plan, "0000500109")
    # The disposition is still right, so only the cause attribution is caught.
    assert spec_mismatches(plan, "0000500109") == [
        "0000500109 root cause lines: spec "
        f"{SPEC_APPLICATIONS['0000500109'].causes!r}, plan "
        f"{frozenset({('RF-03', PARTIES, 15)})!r}"
    ]
