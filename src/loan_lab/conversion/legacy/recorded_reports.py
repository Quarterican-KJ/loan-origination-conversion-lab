"""Read a run's record reports as evidence for reconciliation (spec sections 12.1 and 12.4).

``exceptions.csv``, ``exclusions.csv``, and ``warnings.csv`` are the converter's recorded decisions.
Reconciliation compares them with its independent answer; they are never part of that answer.
Each file is read once, its SHA-256 is checked against the manifest's ``reports`` record from the
same bytes, and only then are those bytes parsed. Nothing is ever written.
"""

import csv
import hashlib
import io
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loan_lab.conversion.legacy import run as runs

# Spec section 13.1, restated so a changed report builder cannot change how evidence is read.
COLUMNS = (
    "FILE_NAME", "LINE_NO", "SOURCE_KEY", "UNIT_KEY", "STAGE", "RULE_CODE", "DEPENDENT",
    "ROOT_CAUSE", "FIELD", "SOURCE_VALUE", "MESSAGE", "REMEDIATION", "SOURCE_LINE",
)
EXCEPTIONS, EXCLUSIONS, WARNINGS = "exceptions", "exclusions", "warnings"
KINDS = (EXCEPTIONS, EXCLUSIONS, WARNINGS)
# Record reports first exist in manifest version 3 (spec 12.6).
REQUIRED_MANIFEST_VERSION = 3


def report_path(kind: str) -> str:
    return f"{runs.REPORTS_DIRECTORY}/{kind}.csv"


@dataclass(frozen=True)
class RecordedRow:
    """One data row of a record report, as decoded by the CSV reader (no trimming)."""

    kind: str
    # Physical line of the report file on which the row starts (the header is line 1).
    report_line: int
    values: Mapping[str, str]

    @property
    def evidence(self) -> str:
        return f"{report_path(self.kind)}:{self.report_line}"

    @property
    def rule(self) -> str:
        return self.values["RULE_CODE"]

    @property
    def file(self) -> str:
        return self.values["FILE_NAME"]

    @property
    def line(self) -> int | None:
        """``LINE_NO`` as a number, or None if it is not plain decimal digits."""
        text = self.values["LINE_NO"]
        if not text.isascii() or not text.isdigit() or (len(text) > 1 and text[0] == "0"):
            return None
        return int(text)


@dataclass(frozen=True)
class RecordedReports:
    rows: Mapping[str, tuple[RecordedRow, ...]]

    def all_rows(self) -> tuple[RecordedRow, ...]:
        return tuple(row for kind in KINDS for row in self.rows[kind])


class RecordedReportsError(ValueError):
    """The run lacks usable record reports. Reconciliation is refused (spec 12.1)."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = tuple(problems)
        super().__init__(" ".join(self.problems))


def read_recorded_reports(directory: Path, manifest: Mapping[str, Any]) -> RecordedReports:
    version = manifest.get("manifest_version")
    if type(version) is not int or version < REQUIRED_MANIFEST_VERSION:
        raise RecordedReportsError([
            f"The run predates row-level reports (manifest version {version}); RC-11 and RC-12 "
            "cannot be evaluated. Convert the archived source again in a new run."
        ])
    record = manifest.get("reports")
    if not isinstance(record, Mapping) or record.get("state") != "complete":
        raise RecordedReportsError(["The manifest does not record complete row-level reports."])
    files = record.get("files")
    problems: list[str] = []
    rows: dict[str, tuple[RecordedRow, ...]] = {}
    for kind in KINDS:
        name = report_path(kind)
        entry = files.get(kind) if isinstance(files, Mapping) else None
        path = directory / name
        if not isinstance(entry, Mapping):
            problems.append(f"The manifest records no checksum for {name}.")
            continue
        if path.is_symlink() or not path.is_file():
            problems.append(f"{name} is missing or is not a regular file.")
            continue
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != entry.get("sha256"):
            problems.append(f"{name} does not match its recorded checksum.")
            continue
        try:
            rows[kind] = _parse(kind, data)
        except (UnicodeDecodeError, csv.Error, ValueError) as error:
            problems.append(f"{name} cannot be parsed: {error}")
    if problems:
        raise RecordedReportsError(problems)
    return RecordedReports(rows)


def _parse(kind: str, data: bytes) -> tuple[RecordedRow, ...]:
    reader = csv.reader(io.StringIO(data.decode("utf-8"), newline=""), strict=True)
    if tuple(next(reader, ())) != COLUMNS:
        raise ValueError("the header is not the documented column list")
    rows = []
    start = reader.line_num + 1
    for fields in reader:
        if len(fields) != len(COLUMNS):
            raise ValueError(f"line {start} has {len(fields)} fields, not {len(COLUMNS)}")
        rows.append(RecordedRow(kind, start, dict(zip(COLUMNS, fields, strict=True))))
        start = reader.line_num + 1
    return tuple(rows)
