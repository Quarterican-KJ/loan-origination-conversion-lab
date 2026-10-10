"""Exception, exclusion, and warning reports (spec section 13).

The reports are derived only from the immutable :class:`ConversionPlan`: one row per rejection
reason (``exceptions.csv``), per exclusion reason (``exclusions.csv``), and per warning
(``warnings.csv``). Identifiers, field values, and raw source lines are written exactly as
extracted; nothing is trimmed, padded, or repaired. Before a report is written, its rows are
checked against the plan's row accounting, so a report can never add, drop, or reclassify a row.

The files are exact evidence, not spreadsheet exports: a value that starts with ``=`` is written
as it is. :func:`spreadsheet_safe` neutralizes such values for downloads.
"""

import csv
import io
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from loan_lab.conversion.legacy import contract
from loan_lab.conversion.legacy.contract import Rule
from loan_lab.conversion.legacy.plan import ConversionPlan, Disposition, Issue, RowResult


class ReportKind(StrEnum):
    EXCEPTIONS = "exceptions"
    EXCLUSIONS = "exclusions"
    WARNINGS = "warnings"

    @property
    def file_name(self) -> str:
        return f"{self.value}.csv"


class ReportState(StrEnum):
    """``manifest.reports.state``. Only COMPLETE means all three reports were written and checked."""

    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    FAILED = "failed"


REPORT_KINDS = tuple(ReportKind)
REPORTS_DIRECTORY = "reports"
# The validation summary's row counts that each report must agree with.
_ACCOUNTING = {
    ReportKind.EXCEPTIONS: ("rejected", "rejected_dependent"),
    ReportKind.EXCLUSIONS: ("excluded", "excluded_dependent"),
}
COLUMNS = (
    "FILE_NAME", "LINE_NO", "SOURCE_KEY", "UNIT_KEY", "STAGE", "RULE_CODE", "DEPENDENT",
    "ROOT_CAUSE", "FIELD", "SOURCE_VALUE", "MESSAGE", "REMEDIATION", "SOURCE_LINE",
)
# Values starting with these characters can be run as formulas by spreadsheet programs.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")

REMEDIATION: Mapping[Rule, str] = {
    Rule.SV_01: "Re-extract the row with the same number of fields as the header and no embedded "
                "line breaks.",
    Rule.SV_02: "Populate the required field in the legacy system and re-extract.",
    Rule.SV_03: "Re-extract the identifier as text with its full width; leading zeros must be "
                "kept. The converter never pads identifiers.",
    Rule.SV_04: "Correct REQ_AMT to digits with exactly two decimal places and no separators.",
    Rule.SV_05: "Correct INT_RATE to six digits with an implied four decimal places "
                "(006500 = 6.5000%).",
    Rule.SV_06: "Correct TERM_MOS to one to three digits.",
    Rule.SV_07: "Correct the code to a value in its source code table.",
    Rule.SV_08: "Make the name fields match CUST_TYPE, and MIDDLE_INIT a single letter.",
    Rule.SV_09: "Resolve the duplicate key in the legacy system; the converter never picks one "
                "of the rows.",
    Rule.SV_10: "Remove the duplicate relationship row in the legacy system.",
    Rule.SV_11: "Shorten the name in the legacy system; the converter never truncates it.",
    Rule.MP_01: "The code has no target mapping in v1; decide its treatment before converting "
                "this record.",
    Rule.MP_02: "Correct the requested amount to a value greater than zero.",
    Rule.MP_03: "Correct the term to between 1 and 600 months.",
    Rule.MP_04: "Shorten the name so the composed legal name is at most 200 characters.",
    Rule.RF_01: "Add the application to the extract, or correct APPL_NO on the relationship.",
    Rule.RF_02: "Add the customer to the extract, or correct CUST_NO; no placeholder is created.",
    Rule.RF_03: "Correct the referenced customer's own failure (see ROOT_CAUSE).",
    Rule.RF_04: "Reactivate the customer, or remove the relationship from the application.",
    Rule.RF_05: "No correction to this row; correct the root cause on its application unit.",
    Rule.RF_06: "Provide exactly one loadable PRI relationship; none is promoted or created.",
    Rule.RF_07: "Correct the rejected relationship rows listed in ROOT_CAUSE.",
    Rule.RF_08: "Keep one primary customer on the application in the legacy system.",
    Rule.EX_01: "None: deliberate scope exclusion of a logically deleted customer.",
    Rule.EX_02: "None: deliberate scope exclusion of an out-of-scope product.",
    Rule.EX_03: "None: deliberate scope exclusion of a voided application.",
    Rule.EX_04: "None: deliberate scope exclusion; signers are not liable parties.",
    Rule.EX_05: "None: follows its excluded application (see ROOT_CAUSE).",
    Rule.WN_01: "Review before release: the customer loads but has no loaded relationship.",
}


@dataclass(frozen=True)
class ReportRow:
    file: str
    line: int
    source_key: str
    unit_key: str
    stage: str
    rule: str
    dependent: bool
    root_cause: str
    field: str
    source_value: str
    message: str
    remediation: str
    source_line: str

    def values(self) -> tuple[str, ...]:
        return (
            self.file, str(self.line), self.source_key, self.unit_key, self.stage, self.rule,
            "Y" if self.dependent else "N", self.root_cause, self.field, self.source_value,
            self.message, self.remediation, self.source_line,
        )


@dataclass(frozen=True)
class Report:
    kind: ReportKind
    rows: tuple[ReportRow, ...]
    data: bytes

    def summary(self) -> dict[str, Any]:
        """Counts recorded in the manifest and checked against the validation summary."""
        sources = {(row.file, row.line) for row in self.rows}
        dependent = {(row.file, row.line) for row in self.rows if row.dependent}
        return {
            "rows": len(self.rows),
            "source_rows": {f: sum(1 for s in sources if s[0] == f) for f in contract.DATA_FILES},
            "dependent_source_rows": {
                f: sum(1 for s in dependent if s[0] == f) for f in contract.DATA_FILES
            },
            "rules": dict(sorted(Counter(row.rule for row in self.rows).items())),
        }


class ReportAccountingError(Exception):
    """A report disagrees with the plan's row accounting. It is never written."""


def build_reports(plan: ConversionPlan) -> tuple[Report, ...]:
    """All three reports, checked against the plan. Raises :class:`ReportAccountingError`."""
    rows = {kind: _rows(plan, kind) for kind in REPORT_KINDS}
    if problems := accounting_problems(plan, rows):
        raise ReportAccountingError("; ".join(problems))
    return tuple(Report(kind, rows[kind], render(rows[kind])) for kind in REPORT_KINDS)


def render(rows: Iterable[ReportRow]) -> bytes:
    """UTF-8 CSV with a header row and CRLF line endings, values exactly as given."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(COLUMNS)
    writer.writerows(row.values() for row in rows)
    return buffer.getvalue().encode("utf-8")


def spreadsheet_safe(value: str) -> str:
    """``value`` prefixed with ``'`` if a spreadsheet could treat it as a formula."""
    return f"'{value}" if value.startswith(FORMULA_PREFIXES) else value


def accounting_problems(
    plan: ConversionPlan, reports: Mapping[ReportKind, tuple[ReportRow, ...]]
) -> list[str]:
    """How the report rows disagree with the plan; empty when every row is accounted for."""
    problems = []
    for kind, disposition in (
        (ReportKind.EXCEPTIONS, Disposition.REJECTED),
        (ReportKind.EXCLUSIONS, Disposition.EXCLUDED),
    ):
        rows = reports[kind]
        for file in contract.DATA_FILES:
            expected = {r.ref.line: r for r in plan.rows(file) if r.disposition is disposition}
            reported: dict[int, list[ReportRow]] = {}
            for row in rows:
                if row.file == file:
                    reported.setdefault(row.line, []).append(row)
            if set(reported) != set(expected):
                problems.append(
                    f"{kind.file_name} lists {file} lines {sorted(reported)}, but the plan "
                    f"{disposition} lines {sorted(expected)}."
                )
                continue
            for line, items in reported.items():
                source = expected[line]
                if [i.rule for i in items] != [str(i.rule) for i in source.issues]:
                    problems.append(f"{kind.file_name} rules for {file}:{line} differ from the plan.")
                if any(i.dependent != source.dependent for i in items):
                    problems.append(f"{kind.file_name} marks {file}:{line} dependent wrongly.")
        if any(row.file not in contract.DATA_FILES for row in rows):
            problems.append(f"{kind.file_name} names a file outside the extract.")
    exceptions = {(r.file, r.line) for r in reports[ReportKind.EXCEPTIONS]}
    exclusions = {(r.file, r.line) for r in reports[ReportKind.EXCLUSIONS]}
    if exceptions & exclusions:
        problems.append("A source row is reported as both rejected and excluded.")
    if len(reports[ReportKind.EXCEPTIONS]) != len(plan.issues):
        problems.append("exceptions.csv does not hold one row per rejection reason.")

    warnings = reports[ReportKind.WARNINGS]
    expected_warnings = [
        (str(r.ref), str(w.rule))
        for file in contract.DATA_FILES for r in plan.rows(file) for w in r.warnings
    ]
    if [(f"{w.file}:{w.line}", w.rule) for w in warnings] != expected_warnings:
        problems.append("warnings.csv does not hold exactly the plan's warnings.")
    wn01 = [w.source_key for w in warnings if w.rule == Rule.WN_01]
    if wn01 != [row.key for row in plan.customers_without_applications]:
        problems.append("warnings.csv WN-01 customers differ from the plan.")
    return problems


def summary_problems(record: Any, validation: Any) -> list[str]:
    """How a manifest's report record disagrees with its validation summary.

    Both arguments are untrusted manifest values. Checksums are compared by the caller.
    """
    if not isinstance(record, Mapping) or record.get("state") != ReportState.COMPLETE:
        state = record.get("state") if isinstance(record, Mapping) else None
        shown = state if state in tuple(ReportState) else "not recorded"
        return [f"The exception, exclusion, and warning reports are not complete (state: {shown})."]
    files = record.get("files")
    rows = validation.get("rows") if isinstance(validation, Mapping) else None
    if not isinstance(files, Mapping) or not isinstance(rows, Mapping):
        return ["The manifest does not record the reports or the row accounting."]
    problems = []
    for kind in REPORT_KINDS:
        entry = files.get(kind)
        if not isinstance(entry, Mapping):
            problems.append(f"The manifest does not record {kind.file_name}.")
            continue
        if entry.get("path") != f"{REPORTS_DIRECTORY}/{kind.file_name}":
            problems.append(f"The manifest records an unexpected path for {kind.file_name}.")
        if kind not in _ACCOUNTING:
            continue
        total, dependent = _ACCOUNTING[kind]
        for file in contract.DATA_FILES:
            counts = rows.get(file)
            if not isinstance(counts, Mapping) or (
                _dig(entry, "source_rows", file) != counts.get(total)
                or _dig(entry, "dependent_source_rows", file) != counts.get(dependent, 0)
            ):
                problems.append(f"{kind.file_name} disagrees with the {file} row accounting.")
    exceptions = files.get(ReportKind.EXCEPTIONS)
    if isinstance(exceptions, Mapping) and exceptions.get("rows") != validation.get("rejections"):
        problems.append("exceptions.csv does not hold one row per recorded rejection reason.")
    warnings = files.get(ReportKind.WARNINGS)
    standalone = validation.get("customers_without_applications")
    if isinstance(warnings, Mapping):
        wn01 = _dig(warnings, "rules", str(Rule.WN_01))
        wn01 = 0 if wn01 is None else wn01
        if not isinstance(standalone, list) or type(wn01) is not int or wn01 != len(standalone):
            problems.append("warnings.csv disagrees with the recorded WN-01 customers.")
    return problems


def _dig(data: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(data, Mapping):
            return None
        data = data.get(key)
    return data


def _rows(plan: ConversionPlan, kind: ReportKind) -> tuple[ReportRow, ...]:
    rows = []
    for file in contract.DATA_FILES:
        for source in plan.rows(file):
            if kind is ReportKind.EXCEPTIONS and source.disposition is Disposition.REJECTED:
                issues = source.issues
            elif kind is ReportKind.EXCLUSIONS and source.disposition is Disposition.EXCLUDED:
                issues = source.issues
            elif kind is ReportKind.WARNINGS:
                issues = source.warnings
            else:
                continue
            rows.extend(_row(source, issue, kind) for issue in issues)
    return tuple(rows)


def _row(source: RowResult, issue: Issue, kind: ReportKind) -> ReportRow:
    causes = issue.causes or source.root_causes
    return ReportRow(
        file=source.ref.file,
        line=source.ref.line,
        source_key=source.key,
        unit_key=source.unit_key or "",
        stage=issue.rule.stage,
        rule=str(issue.rule),
        dependent=source.dependent and kind is not ReportKind.WARNINGS,
        root_cause="; ".join(str(cause) for cause in causes),
        field=issue.field or "",
        source_value="" if issue.value is None else issue.value,
        message=issue.message,
        remediation=REMEDIATION.get(issue.rule, ""),
        source_line=source.raw,
    )
