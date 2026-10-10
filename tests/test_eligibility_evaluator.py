"""The independent eligibility evaluator against hand-authored expectations (spec 12.3, 12.4, 15).

Every expected disposition, rule code, immediate cause, dependency flag, unit key, report row, and
warning below is transcribed by hand from docs/conversion-specification.md. None is computed by
the converter. The planner is used only to show that an injected planner defect is really active
while the evaluator's answer stays the same.
"""

import ast
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from loan_lab.conversion.legacy import SourceRef, contract, eligibility, independent_rules
from loan_lab.conversion.legacy import plan as plan_module
from loan_lab.conversion.legacy.eligibility import (
    Cause,
    Disposition,
    EligibilityResult,
    evaluate_directory,
    evaluate_source,
)
from loan_lab.conversion.legacy.planner import plan_conversion
from loan_lab.paths import find_project_root

SAMPLE_DIR = find_project_root(Path(__file__)) / "sample_data" / "legacy"
B = "borrowers.csv"
A = "applications.csv"
P = "application_parties.csv"

LOADED = Disposition.LOADED
EXCLUDED = Disposition.EXCLUDED
REJECTED = Disposition.REJECTED


def causes(text: str) -> frozenset[Cause]:
    """Parse ``RULE file:line; ...`` as written in the specification."""
    found = set()
    for entry in filter(None, text.split("; ")):
        rule, location = entry.split(" ")
        file, line = location.split(":")
        found.add(Cause(rule, file, int(line)))
    return frozenset(found)


@dataclass(frozen=True)
class Spec:
    disposition: Disposition
    # Rule code -> immediate causes, as written in the specification.
    rules: Mapping[str, str] = field(default_factory=dict)
    dependent: bool = False
    unit_key: str = ""


def mismatches(result: EligibilityResult, expected: Mapping[tuple[str, int], Spec]) -> list[str]:
    found = []
    for (file, number), spec in expected.items():
        line = result.line(file, number)
        where = f"{file}:{number}"
        if line.disposition != spec.disposition:
            found.append(f"{where} disposition {line.disposition}, spec {spec.disposition}")
        if line.rules != frozenset(spec.rules):
            found.append(f"{where} rules {sorted(line.rules)}, spec {sorted(spec.rules)}")
        for rule, text in spec.rules.items():
            if rule in line.causes and line.causes[rule] != causes(text):
                actual = "; ".join(map(str, sorted(line.causes[rule])))
                found.append(f"{where} {rule} causes {actual!r}, spec {text!r}")
        if line.dependent != spec.dependent:
            found.append(f"{where} dependent {line.dependent}, spec {spec.dependent}")
        if line.unit_key != spec.unit_key:
            found.append(f"{where} unit key {line.unit_key!r}, spec {spec.unit_key!r}")
    return found


# --- Spec section 15: the sample extract, line by line ---------------------------------------

UNITS = {
    2: "0000500101", 3: "0000500102", 4: "0000500103", 5: "0000500104", 6: "0000500105",
    7: "0000500106", 8: "0000500107", 9: "0000500108", 10: "0000500109", 11: "0000500110",
    12: "0000500111", 13: "0000500112", 14: "0000500113",
}
PARTY_UNITS = {
    2: "0000500101", 3: "0000500101", 4: "0000500102", 5: "0000500102", 6: "0000500103",
    7: "0000500103", 8: "0000500104", 9: "0000500104", 10: "0000500104", 11: "0000500105",
    12: "0000500106", 13: "0000500107", 14: "0000500108", 15: "0000500109", 16: "0000500109",
    17: "0000500110", 18: "0000500111", 19: "0000500112", 20: "0000500113", 21: "",
}

SAMPLE: dict[tuple[str, int], Spec] = {
    # 15.1
    **{(B, n): Spec(LOADED) for n in (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 17)},
    (B, 12): Spec(REJECTED, {"SV-02": "SV-02 borrowers.csv:12"}),
    (B, 13): Spec(EXCLUDED, {"EX-01": "EX-01 borrowers.csv:13"}),
    (B, 14): Spec(REJECTED, {"SV-09": "SV-09 borrowers.csv:14"}),
    (B, 15): Spec(REJECTED, {"MP-01": "MP-01 borrowers.csv:15"}),
    (B, 16): Spec(REJECTED, {"SV-09": "SV-09 borrowers.csv:16"}),
    # 15.2 and 12.3.4
    **{(A, n): Spec(LOADED, unit_key=UNITS[n]) for n in (2, 3, 4, 5, 6, 7)},
    (A, 8): Spec(REJECTED, {"MP-02": "MP-02 applications.csv:8"}, unit_key=UNITS[8]),
    (A, 9): Spec(REJECTED, {"SV-05": "SV-05 applications.csv:9"}, unit_key=UNITS[9]),
    (A, 10): Spec(
        REJECTED,
        {
            "RF-06": "RF-03 application_parties.csv:15",
            "RF-07": "RF-03 application_parties.csv:15; RF-02 application_parties.csv:16",
        },
        unit_key=UNITS[10],
    ),
    (A, 11): Spec(EXCLUDED, {"EX-02": "EX-02 applications.csv:11"}, unit_key=UNITS[11]),
    (A, 12): Spec(EXCLUDED, {"EX-03": "EX-03 applications.csv:12"}, unit_key=UNITS[12]),
    (A, 13): Spec(
        REJECTED,
        {
            "RF-06": "SV-03 application_parties.csv:19",
            "RF-07": "SV-03 application_parties.csv:19",
        },
        unit_key=UNITS[13],
    ),
    (A, 14): Spec(REJECTED, {"RF-06": "RF-06 applications.csv:14"}, unit_key=UNITS[14]),
    # 15.3 and 12.3.4
    **{(P, n): Spec(LOADED, unit_key=PARTY_UNITS[n]) for n in (2, 3, 4, 5, 6, 7, 8, 9, 11, 12)},
    (P, 10): Spec(EXCLUDED, {"EX-04": "EX-04 application_parties.csv:10"}, unit_key=PARTY_UNITS[10]),
    (P, 13): Spec(
        REJECTED, {"RF-05": "MP-02 applications.csv:8"}, dependent=True, unit_key=PARTY_UNITS[13]
    ),
    (P, 14): Spec(
        REJECTED, {"RF-05": "SV-05 applications.csv:9"}, dependent=True, unit_key=PARTY_UNITS[14]
    ),
    (P, 15): Spec(REJECTED, {"RF-03": "SV-02 borrowers.csv:12"}, unit_key=PARTY_UNITS[15]),
    (P, 16): Spec(REJECTED, {"RF-02": "RF-02 application_parties.csv:16"}, unit_key=PARTY_UNITS[16]),
    (P, 17): Spec(
        EXCLUDED, {"EX-05": "EX-02 applications.csv:11"}, dependent=True, unit_key=PARTY_UNITS[17]
    ),
    (P, 18): Spec(
        EXCLUDED, {"EX-05": "EX-03 applications.csv:12"}, dependent=True, unit_key=PARTY_UNITS[18]
    ),
    (P, 19): Spec(REJECTED, {"SV-03": "SV-03 application_parties.csv:19"}, unit_key=PARTY_UNITS[19]),
    (P, 20): Spec(
        REJECTED, {"RF-05": "RF-06 applications.csv:14"}, dependent=True, unit_key=PARTY_UNITS[20]
    ),
    (P, 21): Spec(REJECTED, {"RF-01": "RF-01 application_parties.csv:21"}),
}

# 12.3.4 and 15.1: WN-01 by borrower line.
SAMPLE_WARNINGS = {
    9: "RF-05 application_parties.csv:14; EX-05 application_parties.csv:17",
    10: "RF-05 application_parties.csv:13; RF-05 application_parties.csv:20",
    17: "EX-05 application_parties.csv:18",
}

# 12.4: FIELD and SOURCE_VALUE of every expected exceptions.csv and exclusions.csv row.
SAMPLE_REPORT_ROWS = {
    (B, 12): [("SV-02", "LAST_NAME", "")],
    (B, 13): [("EX-01", "RECORD_STATUS", "D")],
    (B, 14): [("SV-09", "CUST_NO", "00010013")],
    (B, 15): [("MP-01", "CUST_TYPE", "T")],
    (B, 16): [("SV-09", "CUST_NO", "00010013")],
    (A, 8): [("MP-02", "REQ_AMT", "0.00")],
    (A, 9): [("SV-05", "INT_RATE", "6.875")],
    (A, 10): [("RF-06", "", ""), ("RF-07", "", "")],
    (A, 11): [("EX-02", "PROD_CD", "330")],
    (A, 12): [("EX-03", "APPL_STAT", "X")],
    (A, 13): [("RF-06", "", ""), ("RF-07", "", "")],
    (A, 14): [("RF-06", "", "")],
    (P, 10): [("EX-04", "REL_CD", "SGN")],
    (P, 13): [("RF-05", "", "")],
    (P, 14): [("RF-05", "", "")],
    (P, 15): [("RF-03", "CUST_NO", "00010011")],
    (P, 16): [("RF-02", "CUST_NO", "00010099")],
    (P, 17): [("EX-05", "APPL_NO", "0000500110")],
    (P, 18): [("EX-05", "APPL_NO", "0000500111")],
    (P, 19): [("SV-03", "CUST_NO", "10015")],
    (P, 20): [("RF-05", "", "")],
    (P, 21): [("RF-01", "APPL_NO", "0000500199")],
}


@pytest.fixture(scope="module")
def sample() -> EligibilityResult:
    return evaluate_directory(SAMPLE_DIR)


def test_expectations_cover_every_sample_line() -> None:
    assert sorted(n for f, n in SAMPLE if f == B) == list(range(2, 18))
    assert sorted(n for f, n in SAMPLE if f == A) == list(range(2, 15))
    assert sorted(n for f, n in SAMPLE if f == P) == list(range(2, 22))


def test_sample_lines_match_spec(sample: EligibilityResult) -> None:
    assert len(sample.lines) == 49
    assert mismatches(sample, SAMPLE) == []


def test_sample_keys_are_exact_source_text(sample: EligibilityResult) -> None:
    assert sample.line(B, 17).key == "00010015"
    assert sample.line(P, 19).key == "0000500112/10015"
    assert sample.line(P, 16).key == "0000500109/00010099"


def test_sample_report_rows_match_spec(sample: EligibilityResult) -> None:
    actual = {
        (line.file, line.line): [(r.rule, r.field, r.source_value) for r in line.report_rows]
        for line in sample.lines
        if line.report_rows
    }

    assert actual == SAMPLE_REPORT_ROWS
    assert sum(len(rows) for (f, n), rows in actual.items() if SAMPLE[(f, n)].disposition == REJECTED) == 18
    assert sum(len(rows) for (f, n), rows in actual.items() if SAMPLE[(f, n)].disposition == EXCLUDED) == 6


def test_sample_report_row_stages(sample: EligibilityResult) -> None:
    stages = {row.rule: row.stage for line in sample.lines for row in line.report_rows}

    assert stages["SV-02"] == "source_validation"
    assert stages["MP-02"] == "mapping"
    assert stages["RF-06"] == "reference"
    assert stages["EX-05"] == "exclusion"


def test_sample_warnings_match_spec(sample: EligibilityResult) -> None:
    assert {line.line: line.warning for line in sample.warnings} == {
        line: causes(text) for line, text in SAMPLE_WARNINGS.items()
    }
    assert all(line.file == B and line.disposition == LOADED for line in sample.warnings)


def test_sample_totals_match_spec_15_4(sample: EligibilityResult) -> None:
    assert dict(sample.counts(B)) == {LOADED: 11, EXCLUDED: 1, REJECTED: 4}
    assert dict(sample.counts(A)) == {LOADED: 6, EXCLUDED: 2, REJECTED: 5}
    assert dict(sample.counts(P)) == {LOADED: 10, EXCLUDED: 3, REJECTED: 7}
    assert (sample.dependent_count(B), sample.dependent_count(A), sample.dependent_count(P)) == (
        0, 0, 5,
    )


def test_sample_rf06_names_only_the_rejected_primary(sample: EligibilityResult) -> None:
    # Spec 12.3.4 (Q8): the rejected guarantor on line 16 is a cause of RF-07, not of RF-06.
    line = sample.line(A, 10)

    assert line.causes["RF-06"] == causes("RF-03 application_parties.csv:15")
    assert Cause("RF-02", P, 16) in line.causes["RF-07"]


# --- Independence from the converter ----------------------------------------------------------


def test_planner_defect_rejecting_valid_60_month_application_does_not_affect_evaluator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = evaluate_directory(SAMPLE_DIR)

    # The planner defect: a minimum term of 61 months wrongly rejects 0000500105 (60 months).
    monkeypatch.setattr(contract, "MIN_TERM_MONTHS", 61)
    defective = plan_conversion(SAMPLE_DIR)
    planned = defective.row(SourceRef(A, 6))
    assert planned.key == "0000500105"
    assert planned.disposition == plan_module.Disposition.REJECTED
    assert [str(rule) for rule in planned.rules] == ["MP-03"]
    assert defective.row(SourceRef(P, 11)).disposition == plan_module.Disposition.REJECTED

    after = evaluate_directory(SAMPLE_DIR)

    assert after == before
    assert after.line(A, 6).disposition == LOADED
    assert after.line(A, 6).rules == frozenset()
    assert after.line(P, 11).disposition == LOADED
    assert 8 not in {line.line for line in after.warnings}  # 00010007 keeps a loaded relationship
    assert mismatches(after, SAMPLE) == []


def test_evaluator_ignores_every_converter_table(monkeypatch: pytest.MonkeyPatch) -> None:
    before = evaluate_directory(SAMPLE_DIR)
    never = re.compile(r"(?!)")
    absurd = {
        "CUST_NO_PATTERN": never,
        "APPL_NO_PATTERN": never,
        "AMOUNT_PATTERN": never,
        "RATE_PATTERN": never,
        "TERM_PATTERN": never,
        "MIDDLE_INITIAL_PATTERN": never,
        "MAX_LEGAL_NAME_LENGTH": 0,
        "MAX_NAME_LENGTHS": {"FIRST_NAME": 0, "LAST_NAME": 0, "BUSINESS_NAME": 0},
        "MIN_TERM_MONTHS": 999,
        "MAX_TERM_MONTHS": 0,
        "CUSTOMER_TYPES": {},
        "INDIVIDUAL": "?",
        "RECORD_STATUSES": frozenset(),
        "DELETED": "A",
        "PRODUCTS": {},
        "EXCLUDED_PRODUCTS": frozenset({"110", "120", "210", "220", "310", "320"}),
        "STATUSES": {},
        "VOIDED": "S",
        "ROLES": {},
        "PRIMARY": "GTR",
        "SIGNER": "PRI",
        "REQUIRED_FIELDS": {name: () for name in contract.DATA_FILES},
    }
    for name, value in absurd.items():
        assert hasattr(contract, name), name
        monkeypatch.setattr(contract, name, value)

    assert evaluate_directory(SAMPLE_DIR) == before


_EVALUATOR_MODULES = (eligibility, independent_rules)
_ALLOWED_PROJECT_IMPORTS = {"loan_lab.conversion.legacy.independent_rules"}


@pytest.mark.parametrize("module", _EVALUATOR_MODULES, ids=lambda m: m.__name__)
def test_evaluator_imports_nothing_from_the_converter(module: object) -> None:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    project_imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports are not allowed"
            names = [node.module or ""]
            if node.module == "loan_lab.conversion.legacy":
                names = [f"{node.module}.{alias.name}" for alias in node.names]
        else:
            continue
        project_imports.update(name for name in names if name.split(".")[0] == "loan_lab")

    assert project_imports <= _ALLOWED_PROJECT_IMPORTS


def test_independent_headers_match_the_sample_files() -> None:
    for name in independent_rules.DATA_FILES:
        first = (SAMPLE_DIR / name).read_text(encoding="utf-8").splitlines()[0]
        assert tuple(first.split(",")) == independent_rules.HEADERS[name]


def test_evaluation_is_deterministic() -> None:
    assert evaluate_directory(SAMPLE_DIR) == evaluate_directory(SAMPLE_DIR)


# --- Synthetic extracts: situations the sample does not contain -------------------------------


def app(appl_no: str, product: str = "110", status: str = "S", amount: str = "1000.00",
        rate: str = "005000", term: str = "12") -> str:
    return f"{appl_no},{product},{status},{amount},{rate},{term},20260101,001"


def person(cust_no: str, status: str = "A") -> str:
    return f"{cust_no},I,,Ames,Ruth,,{status},20260101"


def evaluate(borrowers: tuple[str, ...] = (), applications: tuple[str, ...] = (),
             parties: tuple[str, ...] = ()) -> EligibilityResult:
    return evaluate_source({B: borrowers, A: applications, P: parties})


def rows_of(result: EligibilityResult, file: str, line: int) -> list[tuple[str, str, str]]:
    return [(r.rule, r.field, r.source_value) for r in result.line(file, line).report_rows]


APPL = "0000600001"
CUST = "00020001"


def test_duplicate_primary_rows_are_sv10_with_rf07_and_rf06_not_rf08() -> None:
    result = evaluate((person(CUST),), (app(APPL),), (f"{APPL},{CUST},PRI", f"{APPL},{CUST},PRI"))

    assert mismatches(result, {
        (P, 2): Spec(REJECTED, {"SV-10": "SV-10 application_parties.csv:2"}, unit_key=APPL),
        (P, 3): Spec(REJECTED, {"SV-10": "SV-10 application_parties.csv:3"}, unit_key=APPL),
        (A, 2): Spec(REJECTED, {
            "RF-07": "SV-10 application_parties.csv:2; SV-10 application_parties.csv:3",
            "RF-06": "SV-10 application_parties.csv:2; SV-10 application_parties.csv:3",
        }, unit_key=APPL),
    }) == []
    assert rows_of(result, P, 2) == [("SV-10", "", "")]


def test_two_valid_primaries_are_rf08_only() -> None:
    result = evaluate(
        (person(CUST), person("00020002")),
        (app(APPL),),
        (f"{APPL},{CUST},PRI", f"{APPL},00020002,PRI"),
    )

    assert mismatches(result, {
        (A, 2): Spec(REJECTED, {"RF-08": "RF-08 applications.csv:2"}, unit_key=APPL),
        (P, 2): Spec(REJECTED, {"RF-05": "RF-08 applications.csv:2"}, True, APPL),
        (P, 3): Spec(REJECTED, {"RF-05": "RF-08 applications.csv:2"}, True, APPL),
    }) == []


def test_rf06_excludes_rf08_when_conflicting_primaries_are_rejected() -> None:
    result = evaluate(
        (person(CUST),),
        (app(APPL),),
        (f"{APPL},00029991,PRI", f"{APPL},00029992,PRI", f"{APPL},{CUST},GTR"),
    )

    both = "RF-02 application_parties.csv:2; RF-02 application_parties.csv:3"
    assert mismatches(result, {
        (A, 2): Spec(REJECTED, {"RF-07": both, "RF-08": "RF-08 applications.csv:2", "RF-06": both},
                     unit_key=APPL),
        (P, 4): Spec(REJECTED, {"RF-05": f"{both}; RF-08 applications.csv:2"}, True, APPL),
    }) == []
    assert result.line(B, 2).warning == causes("RF-05 application_parties.csv:4")


def test_rf06_excludes_a_rejected_guarantor() -> None:
    result = evaluate((), (app(APPL),), (f"{APPL},20001,PRI", f"{APPL},00029999,GTR"))

    assert mismatches(result, {
        (P, 2): Spec(REJECTED, {"SV-03": "SV-03 application_parties.csv:2"}, unit_key=APPL),
        (P, 3): Spec(REJECTED, {"RF-02": "RF-02 application_parties.csv:3"}, unit_key=APPL),
        (A, 2): Spec(REJECTED, {
            "RF-07": "SV-03 application_parties.csv:2; RF-02 application_parties.csv:3",
            "RF-06": "SV-03 application_parties.csv:2",
        }, unit_key=APPL),
    }) == []


def test_rf05_names_the_applications_immediate_causes() -> None:
    result = evaluate((person(CUST),), (app(APPL),), (f"{APPL},00029999,PRI", f"{APPL},{CUST},GTR"))

    assert mismatches(result, {
        (A, 2): Spec(REJECTED, {
            "RF-07": "RF-02 application_parties.csv:2",
            "RF-06": "RF-02 application_parties.csv:2",
        }, unit_key=APPL),
        (P, 3): Spec(REJECTED, {"RF-05": "RF-02 application_parties.csv:2"}, True, APPL),
    }) == []


def test_duplicated_application_is_sv09_before_exclusion_and_its_parties_are_rf05() -> None:
    result = evaluate((person(CUST),), (app(APPL, product="330"), app(APPL)), (f"{APPL},{CUST},PRI",))

    assert mismatches(result, {
        (A, 2): Spec(REJECTED, {"SV-09": "SV-09 applications.csv:2"}, unit_key=APPL),
        (A, 3): Spec(REJECTED, {"SV-09": "SV-09 applications.csv:3"}, unit_key=APPL),
        (P, 2): Spec(REJECTED, {"RF-05": "SV-09 applications.csv:2; SV-09 applications.csv:3"},
                     True, APPL),
    }) == []
    assert rows_of(result, A, 2) == [("SV-09", "APPL_NO", APPL)]


def test_reference_to_deleted_customer_is_rf04() -> None:
    result = evaluate((person(CUST, status="D"),), (app(APPL),), (f"{APPL},{CUST},PRI",))

    assert mismatches(result, {
        (B, 2): Spec(EXCLUDED, {"EX-01": "EX-01 borrowers.csv:2"}),
        (P, 2): Spec(REJECTED, {"RF-04": "EX-01 borrowers.csv:2"}, unit_key=APPL),
        (A, 2): Spec(REJECTED, {
            "RF-07": "RF-04 application_parties.csv:2",
            "RF-06": "RF-04 application_parties.csv:2",
        }, unit_key=APPL),
    }) == []


def test_reference_to_duplicated_customer_is_rf03_with_every_copy() -> None:
    result = evaluate((person(CUST), person(CUST)), (app(APPL),), (f"{APPL},{CUST},PRI",))

    assert result.line(P, 2).causes == {"RF-03": causes("SV-09 borrowers.csv:2; SV-09 borrowers.csv:3")}


def test_unknown_application_and_customer_are_both_reported() -> None:
    result = evaluate((), (), ("0000699999,00029999,PRI",))

    assert mismatches(result, {
        (P, 2): Spec(REJECTED, {
            "RF-01": "RF-01 application_parties.csv:2",
            "RF-02": "RF-02 application_parties.csv:2",
        }),
    }) == []
    assert rows_of(result, P, 2) == [("RF-01", "APPL_NO", "0000699999"), ("RF-02", "CUST_NO", "00029999")]


def test_early_dependent_exclusion_skips_party_validation() -> None:
    result = evaluate(
        (),
        (app(APPL, product="900"), app("0000600002", product="330", status="X")),
        (f"{APPL},20001,PRI", "0000600002,00029999,PRI"),
    )

    assert mismatches(result, {
        (A, 2): Spec(EXCLUDED, {"EX-02": "EX-02 applications.csv:2"}, unit_key=APPL),
        (A, 3): Spec(EXCLUDED, {
            "EX-02": "EX-02 applications.csv:3",
            "EX-03": "EX-03 applications.csv:3",
        }, unit_key="0000600002"),
        (P, 2): Spec(EXCLUDED, {"EX-05": "EX-02 applications.csv:2"}, True, APPL),
        (P, 3): Spec(EXCLUDED, {"EX-05": "EX-02 applications.csv:3; EX-03 applications.csv:3"},
                     True, "0000600002"),
    }) == []
    assert rows_of(result, P, 2) == [("EX-05", "APPL_NO", APPL)]


def test_signer_is_excluded_before_references_and_does_not_affect_the_unit() -> None:
    result = evaluate((person(CUST),), (app(APPL),), (f"{APPL},{CUST},PRI", f"{APPL},00029999,SGN"))

    assert mismatches(result, {
        (A, 2): Spec(LOADED, unit_key=APPL),
        (P, 2): Spec(LOADED, unit_key=APPL),
        (P, 3): Spec(EXCLUDED, {"EX-04": "EX-04 application_parties.csv:3"}, unit_key=APPL),
    }) == []


def test_unknown_or_blank_customer_type_keeps_type_independent_checks() -> None:
    # Spec 12.3.2, "Unknown or blank CUST_TYPE" (Q10). The converter omits SV-08 here (C2).
    result = evaluate((
        "00020001,X,,Ames,Ruth,AB,A,",
        "00020002,,Acme LLC,,,7,A,",
        "00020003,B,Acme LLC,,,Q,A,",
        "00020004,T,Whitcombe Trust,Smith,,Q,A,",
    ))

    assert rows_of(result, B, 2) == [("SV-07", "CUST_TYPE", "X"), ("SV-08", "MIDDLE_INIT", "AB")]
    assert rows_of(result, B, 3) == [("SV-02", "CUST_TYPE", ""), ("SV-08", "MIDDLE_INIT", "7")]
    assert rows_of(result, B, 4) == [("SV-08", "MIDDLE_INIT", "Q")]
    assert rows_of(result, B, 5) == [("SV-08", "LAST_NAME", "Smith"), ("SV-08", "MIDDLE_INIT", "Q")]


def test_customer_rules_and_name_lengths() -> None:
    long_last = "L" * 101
    result = evaluate((
        f"00020001,I,,{long_last},Ruth,,A,",
        f"00020002,I,,{'L' * 100},{'F' * 100},X,A,",
        f"00020003,I,,Ames,  {'F' * 100}  ,,A,",
        f"00020004,B,{'N' * 201},,,,A,",
        "00020005,I,Acme LLC,,Ruth,,A,",
        "0002000,I,,Ames,Ruth,,Z,",
        "00020007,T,Whitcombe Trust,,,,A,",
    ))

    assert rows_of(result, B, 2) == [("SV-11", "LAST_NAME", long_last)]
    assert rows_of(result, B, 3) == [("MP-04", "", "")]
    assert result.line(B, 4).disposition == LOADED
    assert rows_of(result, B, 5) == [("SV-11", "BUSINESS_NAME", "N" * 201)]
    assert rows_of(result, B, 6) == [("SV-02", "LAST_NAME", ""), ("SV-08", "BUSINESS_NAME", "Acme LLC")]
    assert rows_of(result, B, 7) == [("SV-03", "CUST_NO", "0002000"), ("SV-07", "RECORD_STATUS", "Z")]
    assert rows_of(result, B, 8) == [("MP-01", "CUST_TYPE", "T")]


def test_application_field_and_mapping_rules() -> None:
    result = evaluate(
        (person(CUST),),
        (
            app(APPL, status="s", amount="1.000", rate="6.5", term="0601"),
            app("0000600002", amount="   "),
            app("0000600003", term="601"),
            app("0000600004", amount="0.00", term="0"),
            app("0000600005", term="60"),
            app("0000600006", term="600"),
        ),
        (f"0000600005,{CUST},PRI", f"0000600006,{CUST},PRI"),
    )

    assert rows_of(result, A, 2) == [
        ("SV-04", "REQ_AMT", "1.000"),
        ("SV-05", "INT_RATE", "6.5"),
        ("SV-06", "TERM_MOS", "0601"),
        ("SV-07", "APPL_STAT", "s"),
    ]
    assert rows_of(result, A, 3) == [("SV-02", "REQ_AMT", "   ")]
    assert rows_of(result, A, 4) == [("MP-03", "TERM_MOS", "601")]
    assert rows_of(result, A, 5) == [("MP-02", "REQ_AMT", "0.00"), ("MP-03", "TERM_MOS", "0")]
    # MP-03 bounds are inclusive: 60 and 600 months load.
    assert result.line(A, 6).disposition == LOADED
    assert result.line(A, 7).disposition == LOADED


def test_source_values_keep_csv_decoding_without_trimming() -> None:
    result = evaluate((), (), (f'{APPL},{CUST},"P,R""I"', f'{APPL}, {CUST},PRI'))

    assert rows_of(result, P, 2)[-1] == ("SV-07", "REL_CD", 'P,R"I')
    assert ("SV-03", "CUST_NO", f" {CUST}") in rows_of(result, P, 3)


def test_unreadable_party_lines_belong_to_no_unit() -> None:
    # Spec 12.3.3, interim rule for open question Q9: no association is inferred.
    result = evaluate(
        (person(CUST), person("00020002")),
        (app(APPL),),
        (f"{APPL},{CUST},PRI", f'{APPL},"00020002,GTR', f"{APPL},00020002"),
    )

    for number in (3, 4):
        line = result.line(P, number)
        assert (line.disposition, line.key, line.unit_key) == (REJECTED, "", "")
        assert line.causes == {"SV-01": causes(f"SV-01 application_parties.csv:{number}")}
        assert rows_of(result, P, number) == [("SV-01", "", "")]
    assert result.line(A, 2).disposition == LOADED
    assert result.line(B, 3).warning == frozenset()


def test_customer_named_on_no_relationship_has_an_empty_warning() -> None:
    result = evaluate((person(CUST),))

    assert result.line(B, 2).disposition == LOADED
    assert result.line(B, 2).warning == frozenset()
    assert result.line(B, 2).report_rows == ()
