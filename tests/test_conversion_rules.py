"""Focused tests for individual conversion rules, using small extracts built per test."""

import csv
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from loan_lab.conversion.legacy import (
    ConversionPlan,
    Disposition,
    Outcome,
    Rule,
    SourceRef,
    SourceValidationError,
    plan_conversion,
)
from loan_lab.conversion.legacy.contract import AMOUNT_PATTERN, HEADERS
from loan_lab.models.enums import BorrowerType, PartyRole

BORROWERS = "borrowers.csv"
APPLICATIONS = "applications.csv"
PARTIES = "application_parties.csv"
CONTROL = "extract_control.csv"


def person(cust: str = "00000001", last: str = "Doe", first: str = "Jane", initial: str = "",
           *, status: str = "A", kind: str = "I", business: str = "") -> str:
    return f"{cust},{kind},{business},{last},{first},{initial},{status},20260101"


def company(cust: str = "00000002", name: str = "Acme Example LLC", *, kind: str = "B",
            status: str = "A") -> str:
    return f"{cust},{kind},{name},,,,{status},20260101"


def application(appl: str = "0000000001", *, product: str = "310", status: str = "S",
                amount: str = "100000.00", rate: str = "007500", term: str = "60",
                date: str = "20260901", branch: str = "001") -> str:
    return f"{appl},{product},{status},{amount},{rate},{term},{date},{branch}"


def party(appl: str = "0000000001", cust: str = "00000001", role: str = "PRI") -> str:
    return f"{appl},{cust},{role}"


BASE_BORROWERS = (person(), company())
BASE_APPLICATIONS = (application(),)
BASE_PARTIES = (party(), party(cust="00000002", role="GTR"))


def write_extract(
    directory: Path,
    borrowers: tuple[str, ...] = BASE_BORROWERS,
    applications: tuple[str, ...] = BASE_APPLICATIONS,
    parties: tuple[str, ...] = BASE_PARTIES,
    control: tuple[str, ...] | None = None,
) -> Path:
    data = {BORROWERS: borrowers, APPLICATIONS: applications, PARTIES: parties}
    for name, lines in data.items():
        _write(directory / name, (",".join(HEADERS[name]), *lines))
    if control is None:
        amounts = [
            Decimal(fields[3])
            for fields in csv.reader(applications)
            if len(fields) > 3 and AMOUNT_PATTERN.fullmatch(fields[3])
        ]
        control = (
            f"{BORROWERS},{len(borrowers)},,20260930",
            f"{APPLICATIONS},{len(applications)},{sum(amounts, Decimal('0.00'))},20260930",
            f"{PARTIES},{len(parties)},,20260930",
        )
    _write(directory / CONTROL, (",".join(HEADERS[CONTROL]), *control))
    return directory


def _write(path: Path, lines: tuple[str, ...]) -> None:
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))


def plan_for(tmp_path: Path, **files: tuple[str, ...]) -> ConversionPlan:
    return plan_conversion(write_extract(tmp_path, **files))


def run_issues(tmp_path: Path) -> list[tuple[str, str]]:
    with pytest.raises(SourceValidationError) as raised:
        plan_conversion(tmp_path)
    return [(str(issue.rule), issue.file) for issue in raised.value.issues]


def result(plan: ConversionPlan, file: str, line: int = 2):  # noqa: ANN201
    return plan.row(SourceRef(file, line))


def rules(plan: ConversionPlan, file: str, line: int = 2) -> list[str]:
    return sorted(str(rule) for rule in result(plan, file, line).rules)


# --- Baseline ------------------------------------------------------------------------------


def test_baseline_extract_is_fully_eligible(tmp_path: Path) -> None:
    plan = plan_for(tmp_path)

    assert len(plan.borrowers_to_load) == 2
    assert len(plan.applications_to_load) == 1
    assert [p.role for p in plan.parties_to_load] == [
        PartyRole.PRIMARY_BORROWER,
        PartyRole.GUARANTOR,
    ]
    assert plan.issues == ()


# --- Run-level checks stop the run ---------------------------------------------------------


@pytest.mark.parametrize("missing", [BORROWERS, APPLICATIONS, PARTIES, CONTROL])
def test_missing_file_stops_the_run(tmp_path: Path, missing: str) -> None:
    write_extract(tmp_path)
    (tmp_path / missing).unlink()

    assert run_issues(tmp_path) == [("RUN-01", missing)]


def test_all_run_level_failures_are_reported_together(tmp_path: Path) -> None:
    write_extract(tmp_path)
    (tmp_path / BORROWERS).unlink()
    (tmp_path / PARTIES).unlink()

    assert run_issues(tmp_path) == [("RUN-01", BORROWERS), ("RUN-01", PARTIES)]


def test_invalid_utf8_stops_the_run(tmp_path: Path) -> None:
    write_extract(tmp_path)
    (tmp_path / BORROWERS).write_bytes(b"CUST_NO\n\xff\xfe\n")

    assert run_issues(tmp_path) == [("RUN-02", BORROWERS)]


@pytest.mark.parametrize(
    "header",
    [
        "APPL_NO,REL_CD,CUST_NO",
        "appl_no,cust_no,rel_cd",
        "APPL_NO,CUST_NO",
        "APPL_NO,CUST_NO,REL_CD,EXTRA",
        "",
    ],
)
def test_header_mismatch_stops_the_run(tmp_path: Path, header: str) -> None:
    write_extract(tmp_path)
    _write(tmp_path / PARTIES, (header, party()))

    assert run_issues(tmp_path) == [("RUN-03", PARTIES)]


def test_empty_file_stops_the_run(tmp_path: Path) -> None:
    write_extract(tmp_path)
    (tmp_path / APPLICATIONS).write_bytes(b"")

    assert run_issues(tmp_path) == [("RUN-03", APPLICATIONS)]


def test_record_count_mismatch_stops_the_run(tmp_path: Path) -> None:
    write_extract(
        tmp_path,
        control=(
            f"{BORROWERS},3,,20260930",
            f"{APPLICATIONS},1,100000.00,20260930",
            f"{PARTIES},2,,20260930",
        ),
    )

    assert run_issues(tmp_path) == [("RUN-04", BORROWERS)]


def test_amount_total_mismatch_stops_the_run(tmp_path: Path) -> None:
    write_extract(
        tmp_path,
        control=(
            f"{BORROWERS},2,,20260930",
            f"{APPLICATIONS},1,100000.01,20260930",
            f"{PARTIES},2,,20260930",
        ),
    )

    assert run_issues(tmp_path) == [("RUN-05", APPLICATIONS)]


def test_unparseable_amount_makes_total_unverifiable_but_continues(tmp_path: Path) -> None:
    plan = plan_for(
        tmp_path,
        applications=(application(), application("0000000002", amount="$5.00")),
        parties=(*BASE_PARTIES, party("0000000002")),
    )

    assert not plan.control.amount_total_verified
    assert plan.control.unparseable_amounts == 1
    assert plan.amounts.unparseable == 1
    assert rules(plan, APPLICATIONS, 3) == ["SV-04"]


@pytest.mark.parametrize(
    "control",
    [
        (f"{BORROWERS},2,,20260930", f"{APPLICATIONS},1,100000.00,20260930"),
        (
            f"{BORROWERS},2,,20260930",
            f"{BORROWERS},2,,20260930",
            f"{APPLICATIONS},1,100000.00,20260930",
            f"{PARTIES},2,,20260930",
        ),
        (
            f"{BORROWERS},2,,20260930",
            f"{APPLICATIONS},1,100000.00,20260930",
            f"{PARTIES},2,,20261001",
        ),
        (
            f"{BORROWERS},2,5.00,20260930",
            f"{APPLICATIONS},1,100000.00,20260930",
            f"{PARTIES},2,,20260930",
        ),
        (
            f"{BORROWERS},two,,20260930",
            f"{APPLICATIONS},1,100000.00,20260930",
            f"{PARTIES},2,,20260930",
        ),
        (
            f"{BORROWERS},2,,20260930",
            f"{APPLICATIONS},1,,20260930",
            f"{PARTIES},2,,20260930",
        ),
        (
            f"{BORROWERS},2,,20260930",
            f"{APPLICATIONS},1,100000.00,20260930",
            f"{PARTIES},2,,20260930",
            "collateral.csv,0,,20260930",
        ),
        (
            f"{BORROWERS},2,20260930",
            f"{APPLICATIONS},1,100000.00,20260930",
            f"{PARTIES},2,,20260930",
        ),
        (
            f"{BORROWERS},-1,,20260930",
            f"{APPLICATIONS},1,100000.00,20260930",
            f"{PARTIES},2,,20260930",
        ),
        (
            f"{BORROWERS},2,,20260930",
            f"{APPLICATIONS},1,\"100,000.00\",20260930",
            f"{PARTIES},2,,20260930",
        ),
        (
            f"{BORROWERS},2,,2026-09-30",
            f"{APPLICATIONS},1,100000.00,2026-09-30",
            f"{PARTIES},2,,2026-09-30",
        ),
    ],
    ids=["missing", "duplicate", "dates-differ", "amount-on-wrong-file", "bad-count",
         "no-total", "unknown-file", "malformed-row", "negative-count", "bad-total-format",
         "bad-date-format"],
)
def test_invalid_control_file_stops_the_run(tmp_path: Path, control: tuple[str, ...]) -> None:
    write_extract(tmp_path, control=control)

    issues = run_issues(tmp_path)

    assert issues
    assert {rule for rule, _ in issues} == {"RUN-06"}


def test_byte_order_mark_and_crlf_are_accepted(tmp_path: Path) -> None:
    write_extract(tmp_path)
    path = tmp_path / BORROWERS
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes().replace(b"\n", b"\r\n"))

    plan = plan_conversion(tmp_path)

    assert result(plan, BORROWERS).outcome is Outcome.LOAD_ELIGIBLE
    assert result(plan, BORROWERS).raw == person()


# --- Source validation ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "line", ["00000001,I,,Doe,Jane,,A", '00000001,I,"Doe,Jane,,A,20260101', ""]
)
def test_malformed_row_is_rejected(tmp_path: Path, line: str) -> None:
    plan = plan_for(tmp_path, borrowers=(line, company()))

    assert rules(plan, BORROWERS) == ["SV-01"]
    assert result(plan, BORROWERS).raw == line


def test_whitespace_only_required_field_is_empty(tmp_path: Path) -> None:
    plan = plan_for(tmp_path, borrowers=(person(status="  "), company()))

    assert rules(plan, BORROWERS) == ["SV-02"]
    assert result(plan, BORROWERS).issues[0].field == "RECORD_STATUS"


@pytest.mark.parametrize("cust", ["1", "0000001", "000000001", " 00000001", "0000000A"])
def test_identifiers_are_never_trimmed_or_padded(tmp_path: Path, cust: str) -> None:
    plan = plan_for(tmp_path, borrowers=(person(cust), company()))

    assert rules(plan, BORROWERS) == ["SV-03"]
    assert result(plan, BORROWERS).key == cust
    assert result(plan, PARTIES).outcome is Outcome.REJECTED
    assert rules(plan, PARTIES) == ["RF-02"]


@pytest.mark.parametrize(
    "amount", ["$100.00", "100", "1,000.00", "-5.00", "100.0", "100.000", "1e5", "12345678901234.00"]
)
def test_amount_format_is_strict(tmp_path: Path, amount: str) -> None:
    plan = plan_for(tmp_path, applications=(application(amount=f'"{amount}"'),))

    assert rules(plan, APPLICATIONS) == ["SV-04"]


@pytest.mark.parametrize("rate", ["6.875", "0.06875", "6875", "6.875%", "0068750", ""])
def test_other_rate_notations_are_rejected_not_guessed(tmp_path: Path, rate: str) -> None:
    plan = plan_for(tmp_path, applications=(application(rate=rate),))

    expected = ["SV-02"] if not rate else ["SV-05"]
    assert rules(plan, APPLICATIONS) == expected
    assert plan.applications_to_load == ()


@pytest.mark.parametrize(
    ("rate", "expected"),
    [
        ("006500", Decimal("6.5000")),
        ("006875", Decimal("6.8750")),
        ("011990", Decimal("11.9900")),
        ("000000", Decimal("0.0000")),
        ("999999", Decimal("999.9990")),
    ],
)
def test_rate_mapping_is_exact(tmp_path: Path, rate: str, expected: Decimal) -> None:
    plan = plan_for(tmp_path, applications=(application(rate=rate),))

    mapped = plan.applications_to_load[0].interest_rate
    assert mapped == expected
    assert mapped.as_tuple() == expected.as_tuple()


def test_decimal_values_are_exact_under_a_reduced_context(tmp_path: Path) -> None:
    write_extract(tmp_path, applications=(application(amount="1234567890123.45", rate="012345"),))

    with localcontext() as context:
        context.prec = 3
        plan = plan_conversion(tmp_path)

    mapped = plan.applications_to_load[0]
    assert mapped.requested_amount == Decimal("1234567890123.45")
    assert mapped.interest_rate == Decimal("12.3450")
    assert plan.amounts.eligible == Decimal("1234567890123.45")


@pytest.mark.parametrize("term", ["abc", "1200", "6 0", "-1"])
def test_term_format(tmp_path: Path, term: str) -> None:
    plan = plan_for(tmp_path, applications=(application(term=term),))

    assert rules(plan, APPLICATIONS) == ["SV-06"]


@pytest.mark.parametrize(("term", "eligible"), [("0", False), ("1", True), ("600", True),
                                                 ("601", False)])
def test_term_range(tmp_path: Path, term: str, eligible: bool) -> None:
    plan = plan_for(tmp_path, applications=(application(term=term),))

    assert (rules(plan, APPLICATIONS) == []) is eligible
    if not eligible:
        assert rules(plan, APPLICATIONS) == ["MP-03"]


@pytest.mark.parametrize(
    ("file", "line_text"),
    [
        (BORROWERS, person(kind="X")),
        (BORROWERS, person(status="a")),
        (APPLICATIONS, application(product="999")),
        (APPLICATIONS, application(status="a")),
        (PARTIES, party(role="pri")),
    ],
)
def test_unknown_codes_are_rejected(tmp_path: Path, file: str, line_text: str) -> None:
    files = {BORROWERS: BASE_BORROWERS, APPLICATIONS: BASE_APPLICATIONS, PARTIES: BASE_PARTIES}
    files[file] = (line_text, *files[file][1:])
    plan = plan_for(tmp_path, borrowers=files[BORROWERS], applications=files[APPLICATIONS],
                    parties=files[PARTIES])

    assert rules(plan, file) == ["SV-07"]


@pytest.mark.parametrize(
    "line_text",
    [
        person(business="Doe Holdings"),
        person(initial="AB"),
        "00000001,B,Acme LLC,Doe,,,A,20260101",
        "00000001,T,Acme Family Trust,,Jane,,A,20260101",
    ],
)
def test_name_fields_must_match_customer_type(tmp_path: Path, line_text: str) -> None:
    plan = plan_for(tmp_path, borrowers=(line_text, company()))

    assert rules(plan, BORROWERS) == ["SV-08"]


@pytest.mark.parametrize(
    ("line_text", "missing"),
    [
        (person(last=""), "LAST_NAME"),
        (person(first=" "), "FIRST_NAME"),
        (company(name=""), "BUSINESS_NAME"),
    ],
)
def test_names_required_by_customer_type(tmp_path: Path, line_text: str, missing: str) -> None:
    plan = plan_for(tmp_path, borrowers=(line_text, company("00000009")))

    assert rules(plan, BORROWERS) == ["SV-02"]
    assert result(plan, BORROWERS).issues[0].field == missing


def test_one_row_reports_every_failure_at_its_stage(tmp_path: Path) -> None:
    plan = plan_for(tmp_path, applications=(application(amount="1", rate="6.5", term="x"),))

    assert rules(plan, APPLICATIONS) == ["SV-04", "SV-05", "SV-06"]


def test_duplicate_customer_rejects_every_copy(tmp_path: Path) -> None:
    plan = plan_for(
        tmp_path, borrowers=(person(), company(), person(last="Different", first="Name"))
    )

    assert rules(plan, BORROWERS, 2) == ["SV-09"]
    assert rules(plan, BORROWERS, 4) == ["SV-09"]
    assert rules(plan, PARTIES, 2) == ["RF-03"]
    assert [str(c) for c in result(plan, PARTIES, 2).root_causes] == [
        "SV-09 borrowers.csv:2",
        "SV-09 borrowers.csv:4",
    ]


def test_duplicate_wins_over_exclusion(tmp_path: Path) -> None:
    plan = plan_for(
        tmp_path,
        borrowers=(person(), company(), person("00000003"), person("00000003", status="D")),
    )

    assert rules(plan, BORROWERS, 4) == ["SV-09"]
    assert rules(plan, BORROWERS, 5) == ["SV-09"]


def test_duplicate_application_rejects_all_copies_and_their_relationships(tmp_path: Path) -> None:
    plan = plan_for(tmp_path, applications=(application(), application(amount="5.00")))

    assert rules(plan, APPLICATIONS, 2) == ["SV-09"]
    assert rules(plan, APPLICATIONS, 3) == ["SV-09"]
    party_row = result(plan, PARTIES, 2)
    assert party_row.outcome is Outcome.REJECTED_DEPENDENT
    assert [str(c) for c in party_row.root_causes] == [
        "SV-09 applications.csv:2",
        "SV-09 applications.csv:3",
    ]
    assert plan.units[0].disposition is Disposition.REJECTED


def test_duplicate_relationship_rejects_the_unit(tmp_path: Path) -> None:
    plan = plan_for(tmp_path, parties=(party(), party(), party(cust="00000002", role="GTR")))

    assert rules(plan, PARTIES, 2) == ["SV-10"]
    assert rules(plan, PARTIES, 3) == ["SV-10"]
    # One customer twice is a duplicate relationship, not conflicting primaries: no RF-08.
    assert rules(plan, APPLICATIONS) == ["RF-06", "RF-07"]
    assert [str(c) for c in result(plan, APPLICATIONS).root_causes] == [
        "SV-10 application_parties.csv:2",
        "SV-10 application_parties.csv:3",
    ]
    assert result(plan, PARTIES, 4).outcome is Outcome.REJECTED_DEPENDENT


def test_conflicting_primaries_keep_every_root_cause(tmp_path: Path) -> None:
    plan = plan_for(
        tmp_path,
        parties=(party(), party(cust="00000099"), party(cust="00000002", role="GTR")),
    )

    application_row = result(plan, APPLICATIONS)
    assert rules(plan, APPLICATIONS) == ["RF-07", "RF-08"]
    assert [str(c) for c in application_row.root_causes] == [
        "RF-02 application_parties.csv:3",
        "RF-08 applications.csv:2",
    ]
    assert "00000001, 00000099" in application_row.issues[1].message
    assert result(plan, PARTIES, 2).outcome is Outcome.REJECTED_DEPENDENT


# --- Mapping -------------------------------------------------------------------------------


def test_trust_is_rejected_and_never_mapped_to_business(tmp_path: Path) -> None:
    plan = plan_for(tmp_path, borrowers=(person(), company(kind="T", name="Doe Family Trust")))

    trust = result(plan, BORROWERS, 3)
    assert trust.rules == (Rule.MP_01,)
    assert trust.target is None
    assert [b.borrower_type for b in plan.borrowers_to_load] == [BorrowerType.INDIVIDUAL]
    assert rules(plan, PARTIES, 3) == ["RF-03"]


def test_zero_amount_is_rejected(tmp_path: Path) -> None:
    plan = plan_for(tmp_path, applications=(application(amount="0.00"),))

    assert rules(plan, APPLICATIONS) == ["MP-02"]
    assert plan.amounts.rejected == Decimal("0.00")


@pytest.mark.parametrize(
    ("line_text", "expected", "field"),
    [
        (person(last="L" * 100), [], None),
        (person(last="L" * 101), ["SV-11"], "LAST_NAME"),
        (person(first="F" * 100), [], None),
        (person(first="F" * 101), ["SV-11"], "FIRST_NAME"),
        (person(last="L" * 101, first="F" * 101), ["SV-11", "SV-11"], "LAST_NAME"),
        (company("00000001", name="B" * 200), [], None),
        (company("00000001", name="B" * 201), ["SV-11"], "BUSINESS_NAME"),
        # A trust within the limit still fails mapping; over the limit it stops at SV-11.
        (company("00000001", name="T" * 200, kind="T"), ["MP-01"], None),
        (company("00000001", name="T" * 201, kind="T"), ["SV-11"], "BUSINESS_NAME"),
    ],
)
def test_name_length_limits(
    tmp_path: Path, line_text: str, expected: list[str], field: str | None
) -> None:
    plan = plan_for(tmp_path, borrowers=(line_text, company()))

    row = result(plan, BORROWERS)
    assert rules(plan, BORROWERS) == expected
    if field is not None:
        issue = row.issues[0]
        assert issue.field == field
        assert issue.value == row.raw.split(",")[HEADERS[BORROWERS].index(field)]


def test_name_length_counts_unicode_characters_not_bytes(tmp_path: Path) -> None:
    accented = "é" * 100
    plan = plan_for(tmp_path, borrowers=(person(last=accented), person("00000002", last="é" * 101)))

    assert len(accented.encode("utf-8")) == 200
    assert result(plan, BORROWERS, 2).outcome is Outcome.LOAD_ELIGIBLE
    assert plan.borrowers_to_load[0].legal_name == f"Jane {accented}"
    assert rules(plan, BORROWERS, 3) == ["SV-11"]


def test_name_length_is_measured_after_csv_parsing(tmp_path: Path) -> None:
    # The raw CSV text is 204 characters; the parsed value is exactly 200.
    quoted = '"' + "A" * 195 + ', ""B""' + '"'
    plan = plan_for(tmp_path, borrowers=(person(), company(name=quoted)))

    assert len(quoted) == 204
    business = plan.borrowers_to_load[1]
    assert len(business.legal_name) == 200
    assert business.legal_name.endswith(', "B"')


def test_name_length_ignores_surrounding_whitespace_only(tmp_path: Path) -> None:
    plan = plan_for(tmp_path, borrowers=(person(last=f'"   {"L" * 100}   "'), company()))

    assert result(plan, BORROWERS).outcome is Outcome.LOAD_ELIGIBLE


def test_overlong_name_is_reported_in_full_not_truncated(tmp_path: Path) -> None:
    long_name = "Example " * 30
    plan = plan_for(tmp_path, borrowers=(person(), company(name=long_name)))

    issue = result(plan, BORROWERS, 3).issues[0]
    assert issue.rule is Rule.SV_11
    assert issue.value == long_name
    assert result(plan, BORROWERS, 3).target is None


def test_overlong_name_on_field_that_must_be_empty_is_sv08_only(tmp_path: Path) -> None:
    plan = plan_for(tmp_path, borrowers=(person(business="B" * 250), company()))

    assert rules(plan, BORROWERS) == ["SV-08"]


def test_composed_individual_name_length_limit(tmp_path: Path) -> None:
    plan = plan_for(
        tmp_path, borrowers=(person(last="L" * 100, first="F" * 100, initial="M"), company())
    )

    assert rules(plan, BORROWERS) == ["MP-04"]
    assert "204 characters" in result(plan, BORROWERS).issues[0].message


@pytest.mark.parametrize(
    ("line_text", "expected"),
    [
        (person(last="  de la   Cruz ", first=" Ana", initial="m"), "Ana M. de la Cruz"),
        (person(last="McAllister", first="ROBERT"), "ROBERT McAllister"),
        (company(name='"  Example   Holdings, LLC "'), "Example Holdings, LLC"),
    ],
)
def test_names_normalize_whitespace_only(tmp_path: Path, line_text: str, expected: str) -> None:
    plan = plan_for(tmp_path, borrowers=(line_text, company("00000009")),
                    parties=(party(cust=line_text[:8]),))

    assert plan.borrowers_to_load[0].legal_name == expected


# --- References and conversion units -------------------------------------------------------


def test_relationship_to_missing_application(tmp_path: Path) -> None:
    plan = plan_for(tmp_path, parties=(*BASE_PARTIES, party("0000000099")))

    orphan = result(plan, PARTIES, 4)
    assert orphan.rules == (Rule.RF_01,)
    assert orphan.unit_key is None
    assert plan.units[0].disposition is Disposition.ELIGIBLE


def test_missing_customer_rejects_the_unit_without_a_placeholder(tmp_path: Path) -> None:
    plan = plan_for(tmp_path, parties=(party(), party(cust="00000099", role="GTR")))

    assert rules(plan, PARTIES, 3) == ["RF-02"]
    assert rules(plan, APPLICATIONS) == ["RF-07"]
    assert result(plan, PARTIES, 2).outcome is Outcome.REJECTED_DEPENDENT
    assert [str(c) for c in result(plan, APPLICATIONS).root_causes] == [
        "RF-02 application_parties.csv:3"
    ]
    assert {b.source_system_id for b in plan.borrowers_to_load} == {"00000001", "00000002"}


def test_relationship_to_excluded_customer_is_rejected(tmp_path: Path) -> None:
    plan = plan_for(tmp_path, borrowers=(person(), company(status="D")))

    assert result(plan, BORROWERS, 3).outcome is Outcome.EXCLUDED
    assert rules(plan, PARTIES, 3) == ["RF-04"]
    assert [str(c) for c in result(plan, PARTIES, 3).root_causes] == ["EX-01 borrowers.csv:3"]
    assert rules(plan, APPLICATIONS) == ["RF-07"]


def test_application_without_primary_borrower(tmp_path: Path) -> None:
    plan = plan_for(tmp_path, parties=(party(role="COB"),))

    assert rules(plan, APPLICATIONS) == ["RF-06"]
    assert [str(c) for c in result(plan, APPLICATIONS).root_causes] == [
        "RF-06 applications.csv:2"
    ]
    assert result(plan, PARTIES).outcome is Outcome.REJECTED_DEPENDENT


def test_application_with_no_relationships_at_all(tmp_path: Path) -> None:
    plan = plan_for(tmp_path, parties=(party("0000000099"),))

    assert rules(plan, APPLICATIONS) == ["RF-06"]


def test_two_primary_borrowers_are_not_resolved_by_choice(tmp_path: Path) -> None:
    plan = plan_for(tmp_path, parties=(party(), party(cust="00000002")))

    assert rules(plan, APPLICATIONS) == ["RF-08"]
    assert result(plan, PARTIES, 2).outcome is Outcome.REJECTED_DEPENDENT
    assert result(plan, PARTIES, 3).outcome is Outcome.REJECTED_DEPENDENT


def test_rejected_primary_explains_missing_primary(tmp_path: Path) -> None:
    plan = plan_for(tmp_path, parties=(party(cust="1"), party(cust="00000002", role="GTR")))

    assert rules(plan, APPLICATIONS) == ["RF-06", "RF-07"]
    assert [str(c) for c in result(plan, APPLICATIONS).root_causes] == [
        "SV-03 application_parties.csv:2"
    ]
    guarantor = result(plan, PARTIES, 3)
    assert guarantor.outcome is Outcome.REJECTED_DEPENDENT
    assert [str(c) for c in guarantor.root_causes] == ["SV-03 application_parties.csv:2"]


def test_valid_customer_on_rejected_unit_still_loads_with_warning(tmp_path: Path) -> None:
    plan = plan_for(tmp_path, applications=(application(amount="0.00"),))

    customers = plan.customers_without_applications
    assert [row.key for row in customers] == ["00000001", "00000002"]
    assert all(row.disposition is Disposition.ELIGIBLE for row in customers)
    assert "0000000001 (rejected)" in customers[0].warnings[0].message


# --- Exclusions ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line_text", "rule"),
    [
        (application(product="330"), "EX-02"),
        (application(product="900"), "EX-02"),
        (application(status="X"), "EX-03"),
    ],
)
def test_excluded_application_and_dependent_relationships(
    tmp_path: Path, line_text: str, rule: str
) -> None:
    plan = plan_for(tmp_path, applications=(line_text,))

    assert result(plan, APPLICATIONS).outcome is Outcome.EXCLUDED
    assert rules(plan, APPLICATIONS) == [rule]
    for line in (2, 3):
        assert result(plan, PARTIES, line).outcome is Outcome.EXCLUDED_DEPENDENT
    assert plan.units[0].disposition is Disposition.EXCLUDED
    assert plan.amounts.excluded == Decimal("100000.00")


def test_excluded_rows_are_not_validated_further(tmp_path: Path) -> None:
    plan = plan_for(
        tmp_path,
        applications=(application(status="X", rate="bad", amount="0.00"),),
        parties=(party(cust="1"),),
    )

    assert rules(plan, APPLICATIONS) == ["EX-03"]
    assert rules(plan, PARTIES) == ["EX-05"]


def test_signer_is_excluded_before_reference_checks(tmp_path: Path) -> None:
    plan = plan_for(tmp_path, parties=(*BASE_PARTIES, party(cust="00000099", role="SGN")))

    assert rules(plan, PARTIES, 4) == ["EX-04"]
    assert result(plan, PARTIES, 4).unit_key == "0000000001"
    assert plan.units[0].disposition is Disposition.ELIGIBLE


def test_unmapped_fields_never_reject(tmp_path: Path) -> None:
    plan = plan_for(tmp_path, applications=(application(date="20261399", branch="1"),))

    assert result(plan, APPLICATIONS).outcome is Outcome.LOAD_ELIGIBLE
    inventory = {f.field: f for f in plan.unmapped_fields}
    assert inventory["APPL_DATE"].nonconforming == 1
    assert inventory["BRANCH_NO"].nonconforming == 1
