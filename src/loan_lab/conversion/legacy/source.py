"""Read a Legacy LOS extract and apply the run-level checks (spec section 9.1).

A failure here stops the run before any record is dispositioned: :func:`read_extract` raises
:class:`SourceValidationError` carrying every run-level issue it found.
"""

import csv
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

from loan_lab.conversion.legacy import contract
from loan_lab.conversion.legacy.contract import Rule
from loan_lab.conversion.legacy.transforms import exact_sum, parse_amount

_BOM = b"\xef\xbb\xbf"


@dataclass(frozen=True, order=True)
class SourceRef:
    """A physical line in a source file. The header is line 1."""

    file: str
    line: int

    def __str__(self) -> str:
        return f"{self.file}:{self.line}"


@dataclass(frozen=True)
class SourceRow:
    ref: SourceRef
    raw: str
    header: tuple[str, ...]
    # None when the line is not valid CSV (for example an unterminated quote).
    fields: tuple[str, ...] | None

    @property
    def well_formed(self) -> bool:
        return self.fields is not None and len(self.fields) == len(self.header)

    def value(self, name: str) -> str:
        """The field's text exactly as extracted. Only valid for well-formed rows."""
        if not self.well_formed:
            raise ValueError(f"{self.ref} is not well formed")
        assert self.fields is not None
        return self.fields[self.header.index(name)]


@dataclass(frozen=True)
class SourceFile:
    name: str
    header: tuple[str, ...]
    rows: tuple[SourceRow, ...]
    sha256: str


@dataclass(frozen=True)
class ControlTotals:
    extract_date: str
    record_counts: Mapping[str, int]
    amount_total: Decimal
    # False when some REQ_AMT values could not be parsed, so the total could not be compared.
    amount_total_verified: bool
    unparseable_amounts: int


@dataclass(frozen=True)
class SourceExtract:
    directory: Path
    files: Mapping[str, SourceFile]
    control: ControlTotals

    @property
    def borrowers(self) -> SourceFile:
        return self.files[contract.BORROWERS_FILE]

    @property
    def applications(self) -> SourceFile:
        return self.files[contract.APPLICATIONS_FILE]

    @property
    def parties(self) -> SourceFile:
        return self.files[contract.PARTIES_FILE]

    @property
    def checksums(self) -> Mapping[str, str]:
        return MappingProxyType({name: f.sha256 for name, f in self.files.items()})


@dataclass(frozen=True)
class RunIssue:
    rule: Rule
    file: str
    message: str
    line: int | None = None

    def __str__(self) -> str:
        where = self.file if self.line is None else f"{self.file}:{self.line}"
        return f"{self.rule} {where}: {self.message}"


class SourceValidationError(Exception):
    """The extract failed a run-level check. Nothing may be dispositioned or loaded."""

    def __init__(self, issues: tuple[RunIssue, ...]) -> None:
        self.issues = issues
        super().__init__("\n".join(str(issue) for issue in issues))


def read_extract(directory: Path) -> SourceExtract:
    """Read and structurally validate the four extract files in ``directory``."""
    issues: list[RunIssue] = []
    files: dict[str, SourceFile] = {}
    for name in (*contract.DATA_FILES, contract.CONTROL_FILE):
        source = _read_file(directory / name, name, issues)
        if source is not None:
            files[name] = source
    if issues:
        raise SourceValidationError(tuple(issues))

    control = _check_control(files, issues)
    if issues or control is None:
        raise SourceValidationError(tuple(issues))

    return SourceExtract(
        directory=directory,
        files=MappingProxyType({name: files[name] for name in contract.DATA_FILES}),
        control=control,
    )


def _read_file(path: Path, name: str, issues: list[RunIssue]) -> SourceFile | None:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        issues.append(RunIssue(Rule.RUN_01, name, "File is missing."))
        return None
    except OSError as error:
        issues.append(RunIssue(Rule.RUN_01, name, f"File cannot be read: {error.strerror}."))
        return None

    try:
        text = data.removeprefix(_BOM).decode("utf-8")
    except UnicodeDecodeError as error:
        issues.append(RunIssue(Rule.RUN_02, name, f"Not valid UTF-8 at byte {error.start}."))
        return None

    lines = [line.removesuffix("\r") for line in text.split("\n")]
    if lines and lines[-1] == "":
        lines.pop()

    expected = contract.HEADERS[name]
    header = _parse_line(lines[0]) if lines else None
    if header != expected:
        found = "nothing" if not lines else lines[0]
        issues.append(
            RunIssue(Rule.RUN_03, name, f"Header must be {','.join(expected)}; found {found}.", 1)
        )
        return None

    rows = tuple(
        SourceRow(SourceRef(name, number), line, expected, _parse_line(line))
        for number, line in enumerate(lines[1:], start=2)
    )
    return SourceFile(name, expected, rows, hashlib.sha256(data).hexdigest())


def _parse_line(line: str) -> tuple[str, ...] | None:
    try:
        return tuple(next(csv.reader([line], strict=True), []))
    except csv.Error:
        return None


def _check_control(files: dict[str, SourceFile], issues: list[RunIssue]) -> ControlTotals | None:
    control = files[contract.CONTROL_FILE]
    listed: set[str] = set()
    counts: dict[str, int] = {}
    amount_total: Decimal | None = None
    dates: set[str] = set()

    for row in control.rows:
        if not row.well_formed:
            issues.append(RunIssue(Rule.RUN_06, control.name, "Row is malformed.", row.ref.line))
            continue
        file_name = row.value("FILE_NAME")
        count = row.value("RECORD_COUNT")
        amount = row.value("AMOUNT_TOTAL")
        date = row.value("EXTRACT_DATE")
        line = row.ref.line
        if file_name not in contract.DATA_FILES:
            issues.append(RunIssue(Rule.RUN_06, control.name, f"Unknown file {file_name!r}.", line))
            continue
        if file_name in listed:
            issues.append(RunIssue(Rule.RUN_06, control.name, f"{file_name} is listed twice.", line))
            continue
        listed.add(file_name)
        if not contract.COUNT_PATTERN.fullmatch(count):
            issues.append(
                RunIssue(Rule.RUN_06, control.name, f"RECORD_COUNT {count!r} is not a count.", line)
            )
            continue
        counts[file_name] = int(count)
        if not contract.DATE_PATTERN.fullmatch(date):
            issues.append(
                RunIssue(Rule.RUN_06, control.name, f"EXTRACT_DATE {date!r} is not YYYYMMDD.", line)
            )
        dates.add(date)
        if file_name == contract.APPLICATIONS_FILE:
            if contract.AMOUNT_PATTERN.fullmatch(amount):
                amount_total = parse_amount(amount)
            else:
                issues.append(
                    RunIssue(
                        Rule.RUN_06, control.name, f"AMOUNT_TOTAL {amount!r} is not an amount.", line
                    )
                )
        elif amount:
            issues.append(
                RunIssue(
                    Rule.RUN_06,
                    control.name,
                    f"AMOUNT_TOTAL is only allowed for {contract.APPLICATIONS_FILE}.",
                    line,
                )
            )

    for name in contract.DATA_FILES:
        if name not in listed:
            issues.append(RunIssue(Rule.RUN_06, control.name, f"{name} is not listed."))
    if len(dates) > 1:
        issues.append(
            RunIssue(Rule.RUN_06, control.name, f"EXTRACT_DATE values differ: {sorted(dates)}.")
        )
    if issues or amount_total is None:
        return None

    for name in contract.DATA_FILES:
        actual = len(files[name].rows)
        if actual != counts[name]:
            issues.append(
                RunIssue(
                    Rule.RUN_04,
                    name,
                    f"File has {actual} data rows; the control file says {counts[name]}.",
                )
            )

    amounts: list[Decimal] = []
    unparseable = 0
    for row in files[contract.APPLICATIONS_FILE].rows:
        text = row.value("REQ_AMT") if row.well_formed else ""
        if contract.AMOUNT_PATTERN.fullmatch(text):
            amounts.append(parse_amount(text))
        else:
            unparseable += 1
    total = exact_sum(amounts)
    verified = unparseable == 0
    if verified and total != amount_total:
        issues.append(
            RunIssue(
                Rule.RUN_05,
                contract.APPLICATIONS_FILE,
                f"REQ_AMT total is {total}; the control file says {amount_total}.",
            )
        )
    if issues:
        return None

    return ControlTotals(
        extract_date=dates.pop(),
        record_counts=MappingProxyType(counts),
        amount_total=amount_total,
        amount_total_verified=verified,
        unparseable_amounts=unparseable,
    )
