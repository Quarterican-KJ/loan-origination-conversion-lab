"""Independent, source-driven eligibility evaluation (spec section 12.3).

:func:`evaluate_source` decides, from the archived source lines alone, what should have happened to
every data line of an extract: its disposition, rule codes, dependency, unit key, immediate causes,
expected report rows, and WN-01 warning. It is the expected answer that reconciliation compares
with the converter's recorded evidence and the target database (RC-11, RC-12).

It never consults the converter: no planner, plan, source reader, transformation, report builder,
or ``contract`` table. Its rules come from :mod:`independent_rules`, and it parses CSV itself, so a
converter defect cannot become the expected answer. The evaluation is pure and deterministic.
"""

import csv
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType

from loan_lab.conversion.legacy import independent_rules as rules
from loan_lab.conversion.legacy.independent_rules import APPLICATIONS, BORROWERS, PARTIES

_BOM = "\ufeff"


class Disposition(StrEnum):
    LOADED = "loaded"
    EXCLUDED = "excluded"
    REJECTED = "rejected"


@dataclass(frozen=True, order=True)
class Cause:
    """A rule at a source line, written ``RULE file:line`` in ``ROOT_CAUSE`` (spec 12.3.4)."""

    rule: str
    file: str
    line: int

    def __str__(self) -> str:
        return f"{self.rule} {self.file}:{self.line}"


@dataclass(frozen=True)
class ReportRow:
    """One expected report row: its rule code, ``FIELD``, and ``SOURCE_VALUE`` (spec 12.4)."""

    rule: str
    field: str
    source_value: str

    @property
    def stage(self) -> str:
        return rules.stage_of(self.rule)


@dataclass(frozen=True)
class ExpectedLine:
    file: str
    line: int
    raw: str
    # CUST_NO, APPL_NO, or APPL_NO/CUST_NO as exact source text; empty for an SV-01 line.
    key: str
    disposition: Disposition
    rules: frozenset[str]
    dependent: bool
    # APPL_NO of the line's conversion unit; empty for borrowers and lines in no unit.
    unit_key: str
    # Immediate causes of each rule code on the line.
    causes: Mapping[str, frozenset[Cause]]
    # exceptions.csv or exclusions.csv rows, ordered by rule then column.
    report_rows: tuple[ReportRow, ...]
    # WN-01 causes when the warning applies to this loaded borrower line; None otherwise.
    warning: frozenset[Cause] | None


@dataclass(frozen=True)
class EligibilityResult:
    lines: tuple[ExpectedLine, ...]

    def line(self, file: str, number: int) -> ExpectedLine:
        for candidate in self.lines:
            if candidate.file == file and candidate.line == number:
                return candidate
        raise KeyError(f"{file}:{number}")

    def file_lines(self, file: str) -> tuple[ExpectedLine, ...]:
        return tuple(line for line in self.lines if line.file == file)

    def counts(self, file: str) -> Mapping[Disposition, int]:
        lines = self.file_lines(file)
        return MappingProxyType(
            {d: sum(1 for line in lines if line.disposition == d) for d in Disposition}
        )

    def dependent_count(self, file: str) -> int:
        return sum(1 for line in self.file_lines(file) if line.dependent)

    @property
    def warnings(self) -> tuple[ExpectedLine, ...]:
        return tuple(line for line in self.lines if line.warning is not None)


class SourceLayoutError(ValueError):
    """A source file is missing or its header does not match the layout (a run-level failure)."""


def read_source_lines(directory: Path) -> dict[str, tuple[str, ...]]:
    """The data lines of the three data files in ``directory``, without their headers.

    Lines are split on LF with a trailing CR removed, after a leading byte-order mark (spec 3).
    The run-level checks (spec 9.1) are not repeated here; a missing file or a header that does not
    match its layout raises :class:`SourceLayoutError`.
    """
    files: dict[str, tuple[str, ...]] = {}
    for name in rules.DATA_FILES:
        path = directory / name
        try:
            text = path.read_bytes().decode("utf-8").removeprefix(_BOM)
        except (OSError, UnicodeDecodeError) as error:
            raise SourceLayoutError(f"{name} cannot be read: {error}") from error
        lines = [line.removesuffix("\r") for line in text.split("\n")]
        if lines and lines[-1] == "":
            lines.pop()
        if not lines or _parse(lines[0]) != rules.HEADERS[name]:
            raise SourceLayoutError(f"{name} does not have the header {rules.HEADERS[name]}.")
        files[name] = tuple(lines[1:])
    return files


def evaluate_source(lines: Mapping[str, Sequence[str]]) -> EligibilityResult:
    """Evaluate every data line. ``lines[file]`` holds the file's data lines, from line 2."""
    extract = _Extract(lines)
    extract.structure()
    extract.duplicate_keys()
    extract.exclusions()
    extract.early_dependent_exclusions()
    extract.field_validation()
    extract.mapping()
    extract.references()
    extract.unit_checks()
    extract.dependent_rejections()
    extract.warnings()
    return extract.result()


def evaluate_directory(directory: Path) -> EligibilityResult:
    return evaluate_source(read_source_lines(directory))


def _parse(line: str) -> tuple[str, ...] | None:
    try:
        return tuple(next(csv.reader([line], strict=True), []))
    except csv.Error:
        return None


def _blank(value: str) -> bool:
    return not value.strip()


def _normalize(value: str) -> str:
    return " ".join(value.split())


@dataclass(eq=False)
class _Line:
    file: str
    line: int
    raw: str
    fields: Mapping[str, str] | None
    # Rule code -> reported fields ("" for a rule with no field), in the order found.
    rejections: dict[str, list[str]] = field(default_factory=dict)
    exclusions: dict[str, list[str]] = field(default_factory=dict)
    causes: dict[str, set[Cause]] = field(default_factory=dict)
    dependent: bool = False
    unit_key: str = ""
    warning: frozenset[Cause] | None = None

    @property
    def readable(self) -> bool:
        return self.fields is not None

    @property
    def open(self) -> bool:
        return not self.rejections and not self.exclusions

    def value(self, name: str) -> str:
        assert self.fields is not None
        return self.fields[name]

    def at(self, rule: str) -> Cause:
        return Cause(rule, self.file, self.line)

    def reject(self, rule: str, column: str = "", causes: Iterable[Cause] | None = None) -> None:
        self._record(self.rejections, rule, column, causes)

    def exclude(self, rule: str, column: str = "", causes: Iterable[Cause] | None = None) -> None:
        self._record(self.exclusions, rule, column, causes)

    def _record(
        self,
        target: dict[str, list[str]],
        rule: str,
        column: str,
        causes: Iterable[Cause] | None,
    ) -> None:
        target.setdefault(rule, []).append(column)
        found = {self.at(rule)} if causes is None else set(causes)
        self.causes.setdefault(rule, set()).update(found)

    def own(self, rule_codes: Iterable[str]) -> set[Cause]:
        """Each of the given rule codes at this line."""
        return {self.at(rule) for rule in rule_codes}

    def all_causes(self) -> set[Cause]:
        return {cause for found in self.causes.values() for cause in found}

    @property
    def key(self) -> str:
        if not self.readable:
            return ""
        if self.file == BORROWERS:
            return self.value("CUST_NO")
        if self.file == APPLICATIONS:
            return self.value("APPL_NO")
        return f"{self.value('APPL_NO')}/{self.value('CUST_NO')}"


class _Extract:
    def __init__(self, lines: Mapping[str, Sequence[str]]) -> None:
        self.files: dict[str, list[_Line]] = {}
        for name in rules.DATA_FILES:
            header = rules.HEADERS[name]
            parsed = []
            for number, raw in enumerate(lines.get(name, ()), start=2):
                values = _parse(raw)
                fields = (
                    MappingProxyType(dict(zip(header, values, strict=True)))
                    if values is not None and len(values) == len(header)
                    else None
                )
                parsed.append(_Line(name, number, raw, fields))
            self.files[name] = parsed
        self.application_index: dict[str, list[_Line]] = defaultdict(list)
        self.borrower_index: dict[str, list[_Line]] = defaultdict(list)

    @property
    def borrowers(self) -> list[_Line]:
        return self.files[BORROWERS]

    @property
    def applications(self) -> list[_Line]:
        return self.files[APPLICATIONS]

    @property
    def parties(self) -> list[_Line]:
        return self.files[PARTIES]

    def _open(self, name: str) -> list[_Line]:
        return [line for line in self.files[name] if line.open]

    # Step 1 (spec 12.3.2), with the indexes and unit keys of 12.3.3.
    def structure(self) -> None:
        for lines in self.files.values():
            for line in lines:
                if not line.readable:
                    line.reject("SV-01")
        for line in self.applications:
            if line.readable and not _blank(line.value("APPL_NO")):
                self.application_index[line.value("APPL_NO")].append(line)
                line.unit_key = line.value("APPL_NO")
        for line in self.borrowers:
            if line.readable and not _blank(line.value("CUST_NO")):
                self.borrower_index[line.value("CUST_NO")].append(line)
        for line in self.parties:
            if line.readable and line.value("APPL_NO") in self.application_index:
                line.unit_key = line.value("APPL_NO")

    # Step 2: SV-09 and SV-10 on every readable line, including lines that would be excluded.
    def duplicate_keys(self) -> None:
        for index, column in ((self.borrower_index, "CUST_NO"), (self.application_index, "APPL_NO")):
            for group in index.values():
                if len(group) > 1:
                    for line in group:
                        line.reject("SV-09", column)
        pairs: dict[tuple[str, str], list[_Line]] = defaultdict(list)
        for line in self.parties:
            if not line.readable:
                continue
            appl_no, cust_no = line.value("APPL_NO"), line.value("CUST_NO")
            if not _blank(appl_no) and not _blank(cust_no):
                pairs[(appl_no, cust_no)].append(line)
        for group in pairs.values():
            if len(group) > 1:
                for line in group:
                    line.reject("SV-10")

    # Step 3a: EX-01 to EX-04, each on a single exact code.
    def exclusions(self) -> None:
        for name in rules.DATA_FILES:
            for line in self._open(name):
                for rule, column, codes in rules.EXCLUSIONS[name]:
                    if line.value(column) in codes:
                        line.exclude(rule, column)

    # Step 3b: EX-05 before field validation, when every indexed application line is excluded.
    def early_dependent_exclusions(self) -> None:
        for line in self._open(PARTIES):
            applications = self.application_index.get(line.value("APPL_NO"), [])
            if applications and all(a.exclusions and not a.rejections for a in applications):
                causes = {c for a in applications for c in a.own(a.exclusions)}
                line.exclude("EX-05", "APPL_NO", causes)
                line.dependent = True

    # Step 4: SV-02 to SV-08 and SV-11.
    def field_validation(self) -> None:
        for name in rules.DATA_FILES:
            for line in self._open(name):
                for column in rules.REQUIRED[name]:
                    if _blank(line.value(column)):
                        line.reject("SV-02", column)
                for column, pattern, rule in rules.FORMATS[name]:
                    value = line.value(column)
                    if not _blank(value) and not pattern.fullmatch(value):
                        line.reject(rule, column)
                for column, table in rules.CODE_FIELDS[name]:
                    value = line.value(column)
                    if not _blank(value) and value not in table:
                        line.reject("SV-07", column)
                if name == BORROWERS:
                    self._validate_names(line)

    @staticmethod
    def _validate_names(line: _Line) -> None:
        customer_type = line.value("CUST_TYPE")
        known = customer_type in rules.CUSTOMER_TYPES
        if known:
            expected = rules.TYPE_REQUIRED_NAMES[customer_type]
            for column in expected:
                if _blank(line.value(column)):
                    line.reject("SV-02", column)
            for column in rules.TYPE_EMPTY_NAMES[customer_type]:
                if not _blank(line.value(column)):
                    line.reject("SV-08", column)
            for column in expected:
                value = line.value(column).strip()
                if value and len(value) > rules.NAME_LIMITS[column]:
                    line.reject("SV-11", column)
        # The MIDDLE_INIT format does not depend on the type (spec 12.3.2, "Unknown or blank
        # CUST_TYPE"); for a business or trust a populated initial is already SV-08 above.
        if not known or "MIDDLE_INIT" not in rules.TYPE_EMPTY_NAMES[customer_type]:
            initial = line.value("MIDDLE_INIT").strip()
            if initial and not rules.MIDDLE_INITIAL.fullmatch(initial):
                line.reject("SV-08", "MIDDLE_INIT")

    # Step 5: MP-01 to MP-04. Borrower dispositions are final afterwards.
    def mapping(self) -> None:
        for name in rules.DATA_FILES:
            for line in self._open(name):
                for column, table in rules.MAPPED_CODE_FIELDS[name]:
                    if table[line.value(column)] is None:
                        line.reject("MP-01", column)
        for line in self._open(BORROWERS):
            if len(self._legal_name(line)) > rules.LEGAL_NAME_LIMIT:
                line.reject("MP-04")
        for line in self._open(APPLICATIONS):
            if Decimal(line.value("REQ_AMT")) <= 0:
                line.reject("MP-02", "REQ_AMT")
            term = int(line.value("TERM_MOS"))
            if not rules.TERM_MONTHS_MIN <= term <= rules.TERM_MONTHS_MAX:
                line.reject("MP-03", "TERM_MOS")

    @staticmethod
    def _legal_name(line: _Line) -> str:
        # Section 7: T-NAME-B and T-NAME-I.
        if line.value("CUST_TYPE") != rules.INDIVIDUAL:
            return _normalize(line.value("BUSINESS_NAME"))
        parts = [_normalize(line.value("FIRST_NAME"))]
        initial = line.value("MIDDLE_INIT").strip()
        if initial:
            parts.append(f"{initial.upper()}.")
        parts.append(_normalize(line.value("LAST_NAME")))
        return " ".join(parts)

    # Step 6: RF-01, and one of RF-02, RF-03, RF-04.
    def references(self) -> None:
        for line in self._open(PARTIES):
            if line.value("APPL_NO") not in self.application_index:
                line.reject("RF-01", "APPL_NO")
            borrowers = self.borrower_index.get(line.value("CUST_NO"), [])
            if not borrowers:
                line.reject("RF-02", "CUST_NO")
            elif rejected := [b for b in borrowers if b.rejections]:
                causes = {c for b in rejected for c in b.own(b.rejections)}
                line.reject("RF-03", "CUST_NO", causes)
            elif all(b.exclusions for b in borrowers):
                causes = {c for b in borrowers for c in b.own(b.exclusions)}
                line.reject("RF-04", "CUST_NO", causes)

    # Step 7: RF-07, RF-08, and RF-06 on a unit's single open application line (spec 12.3.3).
    def unit_checks(self) -> None:
        members: dict[str, list[_Line]] = defaultdict(list)
        for line in self.parties:
            if line.unit_key:
                members[line.unit_key].append(line)
        for appl_no, applications in self.application_index.items():
            if len(applications) != 1 or not applications[0].open:
                continue
            application = applications[0]
            required = [p for p in members[appl_no] if not p.exclusions]
            rejected = [p for p in required if p.rejections]
            primaries = [p for p in required if p.value("REL_CD") == rules.PRIMARY]
            customers = {p.value("CUST_NO") for p in primaries if not _blank(p.value("CUST_NO"))}
            if rejected:
                application.reject("RF-07", causes={c for p in rejected for c in p.own(p.rejections)})
            if len(customers) > 1:
                application.reject("RF-08")
            if not any(p.open for p in primaries):
                causes = {c for p in primaries for c in p.own(p.rejections)} or None
                application.reject("RF-06", causes=causes)

    # Step 8: RF-05 on open party lines of a rejected application, through its immediate causes.
    def dependent_rejections(self) -> None:
        for line in self._open(PARTIES):
            applications = self.application_index.get(line.unit_key, [])
            if line.unit_key and any(a.rejections for a in applications):
                causes = {c for a in applications for c in a.all_causes()}
                line.reject("RF-05", causes=causes)
                line.dependent = True

    # Step 9: WN-01 on loaded borrower lines with no loaded party line (spec 12.3.5).
    def warnings(self) -> None:
        named: dict[str, list[_Line]] = defaultdict(list)
        for line in self.parties:
            if line.readable:
                named[line.value("CUST_NO")].append(line)
        for line in self._open(BORROWERS):
            relationships = named.get(line.value("CUST_NO"), [])
            if not any(p.open for p in relationships):
                line.warning = frozenset(
                    c for p in relationships for c in p.own([*p.rejections, *p.exclusions])
                )

    def result(self) -> EligibilityResult:
        return EligibilityResult(
            tuple(_expected(line) for name in rules.DATA_FILES for line in self.files[name])
        )


def _expected(line: _Line) -> ExpectedLine:
    assert not (line.rejections and line.exclusions), f"{line.file}:{line.line} has both"
    if line.rejections:
        disposition, found = Disposition.REJECTED, line.rejections
    elif line.exclusions:
        disposition, found = Disposition.EXCLUDED, line.exclusions
    else:
        disposition, found = Disposition.LOADED, {}
    header = rules.HEADERS[line.file]
    rows = sorted(
        (
            ReportRow(rule, column, line.value(column) if column else "")
            for rule, columns in found.items()
            for column in dict.fromkeys(columns)
        ),
        key=lambda row: (row.rule, header.index(row.field) if row.field else -1),
    )
    return ExpectedLine(
        file=line.file,
        line=line.line,
        raw=line.raw,
        key=line.key,
        disposition=disposition,
        rules=frozenset(found),
        dependent=line.dependent,
        unit_key=line.unit_key,
        causes=MappingProxyType({rule: frozenset(line.causes[rule]) for rule in found}),
        report_rows=tuple(rows),
        warning=line.warning,
    )
