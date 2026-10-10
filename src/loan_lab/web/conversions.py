"""Read-only conversion run evidence for the web interface (spec sections 2.2, 12.2, and 13).

Only files inside the configured evidence root are opened: each run's ``manifest.json``,
``reports/load_result.json``, ``reports/reconciliation.json``, the exception, exclusion, and
warning reports (``reports/*.csv``), and the archived source files.
Paths recorded inside the evidence, such as the conversion database path, are never followed, so
the conversion databases and the development database are never opened. Nothing is written.

Evidence is treated as untrusted input. Every file is size-capped and parsed strictly (no floats,
no NaN), and every value is type-checked before it is displayed. Anything missing or malformed
becomes ``None``, which the pages render as "Not available", never as zero or PASS. A run is
presented as reconciled only when its manifest, load report, and reconciliation report agree.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from loan_lab.conversion.legacy import contract
from loan_lab.conversion.legacy.loader import RUN_ID_PATTERN
from loan_lab.conversion.legacy.reconcile import (
    RECONCILIATION_REPORT_NAME,
    RULE_TITLES,
    ReconciliationRule,
    report_rules,
    report_version_problems,
)
from loan_lab.conversion.legacy.reports import (
    COLUMNS as REPORT_COLUMNS,
    REPORT_KINDS,
    ReportKind,
    ReportState,
    spreadsheet_safe,
    summary_problems,
)
from loan_lab.conversion.legacy.run import (
    LOAD_REPORT_NAME,
    MANIFEST_NAME,
    REPORTS_DIRECTORY,
    REPORTS_MANIFEST_VERSION,
    SOURCE_DIRECTORY,
    SOURCE_FILES,
)

# Directory entries examined when listing runs, and runs shown.
MAX_SCANNED_ENTRIES = 1000
MAX_LISTED_RUNS = 200
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_LOAD_REPORT_BYTES = 1024 * 1024
MAX_RECONCILIATION_REPORT_BYTES = 16 * 1024 * 1024
# Archived source files larger than this are not re-hashed on page views.
MAX_ARCHIVE_HASH_BYTES = 64 * 1024 * 1024
MAX_DISCREPANCIES_SHOWN = 500
MAX_ISSUES_SHOWN = 200
MAX_LIST_ITEMS_SHOWN = 200
MAX_TEXT_LENGTH = 2000
MAX_REPORT_BYTES = 16 * 1024 * 1024
MAX_REPORT_ROWS_SHOWN = 500
MAX_REPORT_QUERY_LENGTH = 100

MANIFEST_FILE = MANIFEST_NAME
LOAD_REPORT_FILE = f"{REPORTS_DIRECTORY}/{LOAD_REPORT_NAME}"
RECONCILIATION_REPORT_FILE = f"{REPORTS_DIRECTORY}/{RECONCILIATION_REPORT_NAME}"
RULES = tuple(ReconciliationRule)
DATA_FILES = tuple(contract.DATA_FILES)
TARGET_TABLES = ("borrowers", "applications", "parties")

# Windows device names cannot be used as directory names.
_RESERVED_NAMES = re.compile(r"(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_AMOUNT = re.compile(r"-?[0-9]{1,18}(\.[0-9]{1,6})?")
_KNOWN_STATUSES = (
    "STARTED", "VALIDATED", "LOADING", "LOADED", "RECONCILED", "FAILED", "UNKNOWN",
)
_IN_PROGRESS_STATUSES = ("STARTED", "VALIDATED", "LOADING")


class InvalidRunId(ValueError):
    """The requested run ID is not a valid run ID. It is never used to build a path."""


def valid_run_id(run_id: str) -> bool:
    return bool(RUN_ID_PATTERN.fullmatch(run_id)) and not _RESERVED_NAMES.fullmatch(run_id)


# --- Conditions ------------------------------------------------------------------------------


class Condition(StrEnum):
    """What the evidence supports, as opposed to what the manifest status claims."""

    RECONCILED = "reconciled"
    LOADED = "loaded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    IN_PROGRESS = "in_progress"
    UNTRUSTED = "untrusted"
    UNAVAILABLE = "unavailable"


CONDITION_LABELS = {
    # RECONCILED is never "released": release approval is a separate human decision.
    Condition.RECONCILED: "Reconciled · awaiting release approval",
    Condition.LOADED: "Loaded · not reconciled",
    Condition.FAILED: "Failed",
    Condition.UNKNOWN: "Outcome unknown",
    Condition.IN_PROGRESS: "Interrupted or in progress",
    Condition.UNTRUSTED: "Untrusted evidence",
    Condition.UNAVAILABLE: "Evidence not available",
}


class FileState(StrEnum):
    OK = "ok"
    MISSING = "missing"
    TOO_LARGE = "too_large"
    MALFORMED = "malformed"
    UNREADABLE = "unreadable"
    UNSAFE = "unsafe"


FILE_STATE_LABELS = {
    FileState.OK: "Read",
    FileState.MISSING: "Not available: missing",
    FileState.TOO_LARGE: "Not available: too large to read",
    FileState.MALFORMED: "Not available: malformed",
    FileState.UNREADABLE: "Not available: unreadable",
    FileState.UNSAFE: "Not available: not a regular file inside the run directory",
}


@dataclass(frozen=True)
class EvidenceFile:
    path: str
    state: FileState
    data: Mapping[str, Any] | None = None
    sha256: str | None = None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.state is FileState.OK

    @property
    def label(self) -> str:
        return FILE_STATE_LABELS[self.state]


# --- Safe file access -----------------------------------------------------------------------


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _resolved_root(root: Path | None) -> Path | None:
    if root is None:
        return None
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return resolved if resolved.is_dir() else None


def _contained(base: Path, path: Path) -> Path | None:
    """``path`` resolved, or None if it is a link or resolves outside ``base``."""
    try:
        if _is_link(path):
            return None
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return resolved if resolved.is_relative_to(base) else None


def run_directory(root: Path | None, run_id: str) -> Path | None:
    """The run's evidence directory, or None if it does not exist as a plain subdirectory."""
    if not valid_run_id(run_id):
        raise InvalidRunId(run_id)
    base = _resolved_root(root)
    if base is None:
        return None
    candidate = base / run_id
    if not os.path.lexists(candidate):
        return None
    resolved = _contained(base, candidate)
    # The exact name is required, so a case-insensitive file system cannot alias another run.
    if resolved is None or resolved.parent != base or resolved.name != run_id:
        return None
    return resolved if resolved.is_dir() else None


def _evidence_path(run_dir: Path, relative: str) -> tuple[Path | None, FileState | None]:
    path = run_dir.joinpath(*relative.split("/"))
    if not os.path.lexists(path):
        return None, FileState.MISSING
    resolved = _contained(run_dir, path)
    if resolved is None or not resolved.is_file():
        return None, FileState.UNSAFE
    return resolved, None


def read_evidence_json(run_dir: Path, relative: str, limit: int) -> EvidenceFile:
    path, problem = _evidence_path(run_dir, relative)
    if path is None:
        return EvidenceFile(relative, problem or FileState.MISSING)
    try:
        size = path.stat().st_size
        if size > limit:
            return EvidenceFile(
                relative, FileState.TOO_LARGE, detail=f"{size:,} bytes; the limit is {limit:,}."
            )
        with path.open("rb") as handle:
            raw = handle.read(limit + 1)
    except OSError as error:
        return EvidenceFile(relative, FileState.UNREADABLE, detail=type(error).__name__)
    if len(raw) > limit:
        return EvidenceFile(relative, FileState.TOO_LARGE, detail=f"The limit is {limit:,} bytes.")
    digest = hashlib.sha256(raw).hexdigest()
    try:
        data = json.loads(
            raw.decode("utf-8"), parse_float=Decimal, parse_constant=_reject_constant
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        return EvidenceFile(relative, FileState.MALFORMED, sha256=digest, detail="Not valid JSON.")
    if not isinstance(data, dict):
        return EvidenceFile(
            relative, FileState.MALFORMED, sha256=digest, detail="Not a JSON object."
        )
    return EvidenceFile(relative, FileState.OK, data, digest)


def _reject_constant(name: str) -> None:
    raise ValueError(f"{name} is not allowed in evidence.")


def _hash_evidence_file(run_dir: Path, relative: str) -> tuple[FileState, str | None]:
    path, problem = _evidence_path(run_dir, relative)
    if path is None:
        return problem or FileState.MISSING, None
    digest = hashlib.sha256()
    try:
        if path.stat().st_size > MAX_ARCHIVE_HASH_BYTES:
            return FileState.TOO_LARGE, None
        read = 0
        with path.open("rb") as handle:
            while chunk := handle.read(1 << 20):
                read += len(chunk)
                if read > MAX_ARCHIVE_HASH_BYTES:
                    return FileState.TOO_LARGE, None
                digest.update(chunk)
    except OSError:
        return FileState.UNREADABLE, None
    return FileState.OK, digest.hexdigest()


# --- Typed access to untrusted values ----------------------------------------------------------


def _dig(data: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(data, Mapping):
            return None
        data = data.get(key)
    return data


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value if len(value) <= MAX_TEXT_LENGTH else value[:MAX_TEXT_LENGTH] + "…"


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= 10**15 else None


def _line(value: Any) -> int | None:
    count = _count(value)
    return count if count else None


def _amount(value: Any) -> Decimal | None:
    if isinstance(value, str) and _AMOUNT.fullmatch(value):
        return Decimal(value)
    return None


def _flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _sha(value: Any) -> str | None:
    return value if isinstance(value, str) and _SHA256.fullmatch(value) else None


def _texts(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(t for t in (_text(v) for v in value[:MAX_LIST_ITEMS_SHOWN]) if t is not None)


# --- View models ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Failure:
    stage: str | None
    step: str | None
    rule: str | None
    reason: str | None


@dataclass(frozen=True)
class RowCounts:
    read: int | None
    loaded: int | None
    excluded: int | None
    rejected: int | None
    excluded_dependent: int | None = None
    rejected_dependent: int | None = None


@dataclass(frozen=True)
class TargetCounts:
    borrowers: int | None
    applications: int | None
    parties: int | None
    requested_amount: Decimal | None


@dataclass(frozen=True)
class SourceFile:
    name: str
    recorded: bool
    bytes: int | None
    planned_sha256: str | None
    archived_sha256: str | None
    verified: bool | None
    error: str | None
    # "matches", "differs", or a FileState value; None when the archive was not re-hashed.
    archive_check: str | None
    current_sha256: str | None


@dataclass(frozen=True)
class Issue:
    rule: str | None
    file: str | None
    line: int | None
    message: str | None


@dataclass(frozen=True)
class Reconciliation:
    state: str | None
    result: str | None
    release_review: str | None
    reconciled_at: str | None
    attempt: str | None
    rules_failed: tuple[str, ...]
    discrepancies: int | None
    problems: tuple[str, ...]
    error: str | None
    # Plain-language outcome; never "released".
    outcome: str
    # Rules that left comparisons unmade (report version 2).
    rules_incomplete: tuple[str, ...] = ()


REPORT_TITLES = {
    ReportKind.EXCEPTIONS: "Exceptions",
    ReportKind.EXCLUSIONS: "Exclusions",
    ReportKind.WARNINGS: "Warnings",
}
REPORT_DESCRIPTIONS = {
    ReportKind.EXCEPTIONS: "Rejected source rows, one row per rule failure, including rows "
                           "rejected only because their conversion unit was rejected.",
    ReportKind.EXCLUSIONS: "Rows deliberately left out of the conversion scope. Not errors.",
    ReportKind.WARNINGS: "Nonblocking warnings. The rows still load.",
}


@dataclass(frozen=True)
class ReportFile:
    kind: ReportKind
    path: str
    recorded: bool
    rows: int | None
    sha256: str | None
    # "matches", "differs", or a FileState value; None when not checked.
    check: str | None

    @property
    def title(self) -> str:
        return REPORT_TITLES[self.kind]


@dataclass(frozen=True)
class Reports:
    """The run's exception, exclusion, and warning reports, as recorded and as found."""

    # A ReportState value, "not_produced" for runs that predate reports, or None if unreadable.
    state: str | None
    error: str | None
    files: tuple[ReportFile, ...]
    problems: tuple[str, ...]

    @property
    def available(self) -> bool:
        """Complete, consistent with the manifest, and (when checked) matching on disk."""
        return (
            self.state == ReportState.COMPLETE
            and not self.problems
            and all(f.check in (None, "matches") for f in self.files)
        )


@dataclass(frozen=True)
class RunView:
    run_id: str
    manifest: EvidenceFile
    load_report: EvidenceFile
    reconciliation_report: EvidenceFile
    status: str | None
    condition: Condition
    problems: tuple[str, ...]
    started_at: str | None = None
    updated_at: str | None = None
    finished_at: str | None = None
    failure: Failure | None = None
    database_transaction: str | None = None
    evidence_state: str | None = None
    evidence_error: str | None = None
    recovered_at: str | None = None
    ready_for_reconciliation: bool | None = None
    load_outcome: str | None = None
    database_sha256: str | None = None
    run_level_checks: str | None = None
    extract_date: str | None = None
    issues: tuple[Issue, ...] = ()
    issues_total: int = 0
    source_verified: bool | None = None
    source_files: tuple[SourceFile, ...] = ()
    archive_checked: bool = False
    rows: Mapping[str, RowCounts] = field(default_factory=dict)
    control_counts: Mapping[str, int | None] = field(default_factory=dict)
    control_amount: Decimal | None = None
    control_amount_verified: bool | None = None
    amounts: Mapping[str, Decimal | None] = field(default_factory=dict)
    unparseable_amounts: int | None = None
    rejections: int | None = None
    customers_without_applications: tuple[str, ...] | None = None
    expected_target: TargetCounts | None = None
    loaded_target: TargetCounts | None = None
    reconciliation: Reconciliation | None = None
    release_recorded: bool = False
    reports: Reports | None = None

    @property
    def condition_label(self) -> str:
        return CONDITION_LABELS[self.condition]

    @property
    def status_recognized(self) -> bool:
        return self.status in _KNOWN_STATUSES


@dataclass(frozen=True)
class RuleView:
    rule: str
    title: str
    result: str | None
    checked: int | None
    discrepancies: int | None
    note: str | None
    # Lines whose row details the rule did not compare (report version 2, RC-12 only).
    unexamined_lines: int | None = None


@dataclass(frozen=True)
class DiscrepancyView:
    rule: str | None
    check: str | None
    file: str | None
    line: int | None
    source_key: str | None
    target_table: str | None
    target_id: int | None
    field: str | None
    expected: str | None
    actual: str | None
    message: str | None
    # Report version 2 only; absent from earlier reports.
    unit_key: str | None = None
    evidence: str | None = None


@dataclass(frozen=True)
class ReconciliationView:
    run: RunView
    report_verified: bool
    report_problems: tuple[str, ...]
    generated_at: str | None
    result: str | None
    rules: tuple[RuleView, ...]
    discrepancies_total: int | None
    discrepancies: tuple[DiscrepancyView, ...]
    by_rule: Mapping[str, tuple[DiscrepancyView, ...]]
    other_discrepancies: tuple[DiscrepancyView, ...]
    rows: Mapping[str, RowCounts]
    amounts: Mapping[str, Decimal | None]
    target: TargetCounts | None
    report_version: int | None
    # Set when no reconciliation has been attempted: why, for this run's status.
    not_performed: str | None = None

    @property
    def unexamined_rules(self) -> tuple[RuleView, ...]:
        return tuple(rule for rule in self.rules if rule.unexamined_lines)


@dataclass(frozen=True)
class RunListing:
    root: Path | None
    root_available: bool
    runs: tuple[RunView, ...]
    skipped_entries: int
    truncated: bool


# --- Listing -----------------------------------------------------------------------------------


def list_runs(root: Path | None) -> RunListing:
    """Assess up to MAX_LISTED_RUNS runs, most recently modified first. Scans are bounded."""
    base = _resolved_root(root)
    if base is None:
        return RunListing(root, False, (), 0, False)
    candidates: list[tuple[float, str]] = []
    skipped = 0
    truncated = False
    try:
        with os.scandir(base) as entries:
            for examined, entry in enumerate(entries):
                if examined >= MAX_SCANNED_ENTRIES:
                    truncated = True
                    break
                try:
                    plain_directory = entry.is_dir(follow_symlinks=False) and not (
                        entry.is_symlink() or entry.is_junction()
                    )
                    modified = entry.stat(follow_symlinks=False).st_mtime
                except OSError:
                    plain_directory = False
                if not plain_directory or not valid_run_id(entry.name):
                    skipped += 1
                    continue
                candidates.append((modified, entry.name))
    except OSError:
        return RunListing(root, False, (), 0, False)
    candidates.sort(key=lambda item: (-item[0], item[1]))
    if len(candidates) > MAX_LISTED_RUNS:
        truncated = True
    runs = []
    for _, name in candidates[:MAX_LISTED_RUNS]:
        directory = run_directory(base, name)
        if directory is not None:
            runs.append(assess_run(name, directory, check_archive=False))
    runs.sort(key=lambda run: (run.started_at or "", run.run_id), reverse=True)
    return RunListing(root, True, tuple(runs), skipped, truncated)


def load_run(root: Path | None, run_id: str) -> RunView | None:
    directory = run_directory(root, run_id)
    if directory is None:
        return None
    return assess_run(run_id, directory, check_archive=True)


# --- Assessment ---------------------------------------------------------------------------------


def assess_run(run_id: str, run_dir: Path, *, check_archive: bool) -> RunView:
    manifest_file = read_evidence_json(run_dir, MANIFEST_FILE, MAX_MANIFEST_BYTES)
    load_file = read_evidence_json(run_dir, LOAD_REPORT_FILE, MAX_LOAD_REPORT_BYTES)
    report_file = read_evidence_json(
        run_dir, RECONCILIATION_REPORT_FILE, MAX_RECONCILIATION_REPORT_BYTES
    )
    if not manifest_file.ok:
        return RunView(
            run_id, manifest_file, load_file, report_file, None, Condition.UNAVAILABLE,
            (f"{MANIFEST_FILE}: {manifest_file.label}.",),
        )
    manifest = manifest_file.data
    problems: list[str] = []

    status = _text(manifest.get("status"))
    manifest_run = _text(manifest.get("run_id"))
    if manifest_run != run_id:
        problems.append("The manifest names a different run than its directory.")
    if status not in _KNOWN_STATUSES:
        problems.append("The manifest status is missing or not a recognized run status.")
    release_recorded = manifest.get("release") is not None
    if release_recorded:
        problems.append(
            "The manifest records a release decision, but release approval is not implemented."
        )

    evidence_state = _text(_dig(manifest, "evidence", "state"))
    transaction = _text(manifest.get("database_transaction"))
    database_sha = _sha(_dig(manifest, "database", "sha256"))
    failure = _failure(manifest.get("failure"))
    source_files = _source_files(run_dir, manifest, check_archive)
    expected_target = _target_counts(manifest.get("expected_target"))
    loaded_target = None
    if load_file.ok:
        loaded_target = _target_counts(load_file.data.get("loaded"))
    reconciliation = _reconciliation(run_id, manifest, report_file, database_sha)

    reports = _reports(run_dir, manifest, check_archive)
    if status in ("LOADED", "RECONCILED"):
        problems.extend(_load_problems(
            run_id, manifest, load_file, transaction, evidence_state, database_sha,
            expected_target, loaded_target, source_files,
        ))
        if reports.state != "not_produced":
            problems.extend(reports.problems)
            problems.extend(
                f"{f.path} does not match its recorded checksum ({f.check})."
                for f in reports.files if f.check not in (None, "matches")
            )
    elif status in _IN_PROGRESS_STATUSES and evidence_state in ("incomplete", "unverified"):
        problems.append(f"The evidence is not complete (state: {evidence_state}).")
    if status == "LOADED":
        if reconciliation.state in ("in_progress", "unfinalized", "conflict"):
            problems.append(
                f"A reconciliation attempt is {reconciliation.state.replace('_', ' ')}; "
                "the run is not reconciled."
            )
        elif reconciliation.state is not None:
            problems.append("The manifest records a reconciliation outcome but is still LOADED.")
        elif manifest.get("ready_for_reconciliation") is not True:
            problems.append("The manifest does not mark the run ready for reconciliation.")
    if status == "RECONCILED":
        problems.extend(reconciliation.problems)
        if reconciliation.state != "final" or reconciliation.result != "passed":
            problems.append("The manifest does not record a final, passed reconciliation.")

    condition = _condition(status, evidence_state, problems)
    validation = manifest.get("validation")
    issues_raw = _dig(validation, "issues")
    issues_list = issues_raw if isinstance(issues_raw, list) else []
    return RunView(
        run_id=run_id,
        manifest=manifest_file,
        load_report=load_file,
        reconciliation_report=report_file,
        status=status,
        condition=condition,
        problems=tuple(dict.fromkeys(problems)),
        started_at=_text(manifest.get("started_at")),
        updated_at=_text(manifest.get("updated_at")),
        finished_at=_text(manifest.get("finished_at")),
        failure=failure,
        database_transaction=transaction,
        evidence_state=evidence_state,
        evidence_error=_text(_dig(manifest, "evidence", "error")),
        recovered_at=_text(_dig(manifest, "evidence", "recovered_at")),
        ready_for_reconciliation=_flag(manifest.get("ready_for_reconciliation")),
        load_outcome=_text(_dig(manifest, "load", "outcome")),
        database_sha256=database_sha,
        run_level_checks=_text(_dig(validation, "run_level_checks")),
        extract_date=_text(_dig(validation, "extract_date")),
        issues=tuple(_issue(item) for item in issues_list[:MAX_ISSUES_SHOWN]),
        issues_total=len(issues_list),
        source_verified=_flag(_dig(manifest, "source", "verified")),
        source_files=source_files,
        archive_checked=check_archive,
        rows={file: _row_counts(_dig(validation, "rows", file), "eligible") for file in DATA_FILES},
        control_counts={
            file: _count(_dig(validation, "control", "record_counts", file))
            for file in DATA_FILES
        },
        control_amount=_amount(_dig(validation, "control", "amount_total")),
        control_amount_verified=_flag(_dig(validation, "control", "amount_total_verified")),
        amounts={
            name: _amount(_dig(validation, "requested_amount", name))
            for name in ("eligible", "excluded", "rejected")
        },
        unparseable_amounts=_count(_dig(validation, "requested_amount", "unparseable")),
        rejections=_count(_dig(validation, "rejections")),
        customers_without_applications=(
            _texts(_dig(validation, "customers_without_applications"))
            if isinstance(_dig(validation, "customers_without_applications"), list) else None
        ),
        expected_target=expected_target,
        loaded_target=loaded_target,
        reconciliation=reconciliation,
        release_recorded=release_recorded,
        reports=reports,
    )


def _reports(run_dir: Path, manifest: Mapping[str, Any], check: bool) -> Reports:
    version = manifest.get("manifest_version")
    if type(version) is int and version < REPORTS_MANIFEST_VERSION:
        return Reports("not_produced", None, (), ())
    record = manifest.get("reports")
    state = _text(_dig(record, "state"))
    if state not in tuple(ReportState):
        state = None
    files = []
    for kind in REPORT_KINDS:
        entry = _dig(record, "files", kind)
        relative = f"{REPORTS_DIRECTORY}/{kind.file_name}"
        sha = _sha(_dig(entry, "sha256"))
        result = None
        if check and sha is not None:
            file_state, current = _hash_evidence_file(run_dir, relative)
            if file_state is FileState.OK:
                result = "matches" if current == sha else "differs"
            else:
                result = str(file_state)
        files.append(ReportFile(
            kind, relative, isinstance(entry, Mapping), _count(_dig(entry, "rows")), sha, result,
        ))
    problems = summary_problems(record, manifest.get("validation"))
    return Reports(state, _text(_dig(record, "error")), tuple(files), tuple(problems))


def _condition(status: str | None, evidence_state: str | None, problems: list[str]) -> Condition:
    if status == "FAILED":
        return Condition.FAILED
    if status == "UNKNOWN":
        return Condition.UNKNOWN
    if status in _IN_PROGRESS_STATUSES:
        if evidence_state in ("incomplete", "unverified"):
            return Condition.UNTRUSTED
        return Condition.IN_PROGRESS
    if problems or status not in ("LOADED", "RECONCILED"):
        return Condition.UNTRUSTED
    return Condition.RECONCILED if status == "RECONCILED" else Condition.LOADED


def _load_problems(
    run_id: str,
    manifest: Mapping[str, Any],
    load_file: EvidenceFile,
    transaction: str | None,
    evidence_state: str | None,
    database_sha: str | None,
    expected: TargetCounts | None,
    loaded: TargetCounts | None,
    source_files: tuple[SourceFile, ...],
) -> list[str]:
    problems = []
    if evidence_state != "complete":
        problems.append(f"The evidence is not complete (state: {evidence_state or 'missing'}).")
    if transaction != "committed":
        problems.append("The manifest does not record a committed database transaction.")
    if _dig(manifest, "source", "verified") is not True:
        problems.append("The archived source is not recorded as verified against the plan.")
    if database_sha is None:
        problems.append("The manifest records no database checksum.")
    if not load_file.ok:
        problems.append(f"{LOAD_REPORT_FILE}: {load_file.label}.")
    else:
        report = load_file.data
        if report.get("run_id") != run_id or report.get("success") is not True:
            problems.append("The load report does not record a successful load of this run.")
        if _sha(_dig(report, "database", "sha256")) != database_sha:
            problems.append("The load report and manifest disagree about the database checksum.")
        if loaded is None or expected is None or loaded != expected:
            problems.append(
                "The counts and amount read back after the load do not match the expected target."
            )
    for source in source_files:
        if source.archive_check not in (None, "matches"):
            problems.append(f"The archived {source.name} does not match its recorded checksum.")
    return problems


def _failure(value: Any) -> Failure | None:
    if not isinstance(value, Mapping):
        return None
    return Failure(
        _text(value.get("stage")),
        _text(value.get("step")),
        _text(value.get("rule")),
        _text(value.get("reason")),
    )


def _issue(value: Any) -> Issue:
    return Issue(
        _text(_dig(value, "rule")),
        _text(_dig(value, "file")),
        _line(_dig(value, "line")),
        _text(_dig(value, "message")),
    )


def _row_counts(value: Any, loaded_key: str) -> RowCounts:
    return RowCounts(
        read=_count(_dig(value, "read")),
        loaded=_count(_dig(value, loaded_key)),
        excluded=_count(_dig(value, "excluded")),
        rejected=_count(_dig(value, "rejected")),
        excluded_dependent=_count(_dig(value, "excluded_dependent")),
        rejected_dependent=_count(_dig(value, "rejected_dependent")),
    )


def _target_counts(value: Any) -> TargetCounts | None:
    if not isinstance(value, Mapping):
        return None
    return TargetCounts(
        _count(value.get("borrowers")),
        _count(value.get("applications")),
        _count(value.get("parties")),
        _amount(value.get("requested_amount")),
    )


def _source_files(
    run_dir: Path, manifest: Mapping[str, Any], check_archive: bool
) -> tuple[SourceFile, ...]:
    files = []
    for name in SOURCE_FILES:
        entry = _dig(manifest, "source", "files", name)
        recorded = isinstance(entry, Mapping)
        archived = _sha(_dig(entry, "archived_sha256"))
        planned = _sha(_dig(entry, "planned_sha256"))
        check = current = None
        if check_archive and recorded and archived is not None:
            state, current = _hash_evidence_file(run_dir, f"{SOURCE_DIRECTORY}/{name}")
            if state is FileState.OK:
                matches = current == archived and planned in (None, archived)
                check = "matches" if matches else "differs"
            else:
                check = str(state)
        files.append(SourceFile(
            name=name,
            recorded=recorded,
            bytes=_count(_dig(entry, "bytes")),
            planned_sha256=planned,
            archived_sha256=archived,
            verified=_flag(_dig(entry, "verified")),
            error=_text(_dig(entry, "error")),
            archive_check=check,
            current_sha256=current,
        ))
    return tuple(files)


_RECONCILIATION_STATES = ("in_progress", "unfinalized", "conflict", "final")


def _reconciliation(
    run_id: str, manifest: Mapping[str, Any], report_file: EvidenceFile, database_sha: str | None
) -> Reconciliation:
    record = manifest.get("reconciliation")
    report_problems = _report_problems(run_id, record, report_file, database_sha)
    if not isinstance(record, Mapping):
        outcome = "Not run" if record is None else "Not available"
        if record is None and report_file.state is not FileState.MISSING:
            outcome = "Not available: a report exists but the manifest records no attempt"
        return Reconciliation(
            None, None, None, None, None, (), None, tuple(report_problems), None, outcome
        )
    state = _text(record.get("state"))
    result = _text(record.get("result"))
    if state not in _RECONCILIATION_STATES:
        state = None
    if state == "final" and result == "passed":
        outcome = "Passed" if not report_problems else "Unverified: the report does not verify"
    elif state == "final" and result == "failed":
        outcome = "Failed"
    elif state == "in_progress":
        outcome = "In progress or interrupted (not reconciled)"
    elif state == "unfinalized":
        outcome = "Report written but not finalized (not reconciled)"
    elif state == "conflict":
        outcome = "Conflicting evidence (not reconciled)"
    else:
        outcome = "Not available"
    rules_failed = record.get("rules_failed")
    return Reconciliation(
        state=state,
        result=result if result in ("passed", "failed") else None,
        release_review=_text(record.get("release_review")),
        reconciled_at=_text(record.get("reconciled_at")),
        attempt=_text(record.get("attempt")),
        rules_failed=_texts(rules_failed),
        discrepancies=_count(record.get("discrepancies")),
        problems=tuple(report_problems) + _texts(record.get("problems")),
        error=_text(record.get("error")),
        outcome=outcome,
        rules_incomplete=_texts(record.get("rules_incomplete")),
    )


def _report_problems(
    run_id: str, record: Any, report_file: EvidenceFile, database_sha: str | None
) -> list[str]:
    """Why the reconciliation report does not support a final PASS for this run."""
    if not isinstance(record, Mapping) or record.get("state") != "final":
        return []
    problems = []
    if not report_file.ok:
        problems.append(f"{RECONCILIATION_REPORT_FILE}: {report_file.label}.")
        return problems
    report = report_file.data
    if _sha(record.get("report_sha256")) != report_file.sha256:
        problems.append(f"{RECONCILIATION_REPORT_FILE} does not match the checksum in the manifest.")
    if report.get("run_id") != run_id or report.get("attempt") != record.get("attempt"):
        problems.append(f"{RECONCILIATION_REPORT_FILE} is not the report of the finalized attempt.")
    if _sha(_dig(report, "database", "sha256")) != database_sha:
        problems.append("The reconciliation report and manifest disagree about the database.")
    if record.get("result") == "passed":
        if report.get("result") != "PASS":
            problems.append(f"{RECONCILIATION_REPORT_FILE} does not record a PASS.")
        # Each report is held to the rules of its own report_version (spec 12.2).
        problems.extend(report_version_problems(report, record))
        if report.get("discrepancies") != []:
            problems.append("The report lists discrepancies, or none can be read.")
    return problems


# INCOMPLETE: no discrepancies, but comparisons were left unmade (report version 2, spec 12.4).
_RULE_RESULTS = ("PASS", "FAIL", "INCOMPLETE")


def _rule_results(report: Mapping[str, Any]) -> dict[str, tuple[str | None, Mapping[str, Any]]]:
    results: dict[str, tuple[str | None, Mapping[str, Any]]] = {}
    rules = report.get("rules")
    if not isinstance(rules, list):
        return results
    for item in rules:
        rule = _dig(item, "rule")
        if rule not in RULES or rule in results:
            # A duplicated rule cannot be trusted either way.
            if rule in results:
                results[rule] = (None, {})
            continue
        result = _dig(item, "result")
        results[rule] = (result if result in _RULE_RESULTS else None, item)
    return results


# --- Reconciliation page --------------------------------------------------------------------------


def load_reconciliation(root: Path | None, run_id: str) -> ReconciliationView | None:
    run = load_run(root, run_id)
    if run is None:
        return None
    report_file = run.reconciliation_report
    report = report_file.data if report_file.ok else None
    report_problems = list(run.reconciliation.problems) if run.reconciliation else []
    if report is not None and run.reconciliation and run.reconciliation.state != "final":
        report_problems.append(
            "The manifest has not finalized this report, so it is provisional evidence only."
        )
    if report is not None and run.reconciliation is not None and run.reconciliation.state is None:
        report_problems.append("The manifest records no reconciliation attempt for this report.")
    verified = (
        report is not None
        and run.reconciliation is not None
        and run.reconciliation.state == "final"
        and not run.reconciliation.problems
        and run.condition in (Condition.RECONCILED, Condition.FAILED)
    )
    if report is None:
        # Without a report no rule has a result to show (spec 12.2).
        return ReconciliationView(
            run, False, tuple(report_problems), None, None, (), None, (), {}, (), {}, {}, None,
            None, not_performed=_not_performed(run),
        )
    version = report.get("report_version")
    # A version 1 report is shown without RC-11 and RC-12, which it never evaluated.
    shown_rules = report_rules(report) or RULES
    rule_results = _rule_results(report)
    rules = tuple(
        RuleView(
            rule=str(rule),
            title=RULE_TITLES[rule],
            result=rule_results[rule][0] if rule in rule_results else None,
            checked=_count(_dig(rule_results.get(rule, (None, {}))[1], "checked")),
            discrepancies=_count(_dig(rule_results.get(rule, (None, {}))[1], "discrepancies")),
            note=_text(_dig(rule_results.get(rule, (None, {}))[1], "note")),
            unexamined_lines=_count(
                _dig(rule_results.get(rule, (None, {}))[1], "not_evaluated", "lines")
            ),
        )
        for rule in shown_rules
    )
    raw = report.get("discrepancies")
    items = raw if isinstance(raw, list) else []
    shown = tuple(_discrepancy(item) for item in items[:MAX_DISCREPANCIES_SHOWN])
    by_rule = {
        str(rule): tuple(d for d in shown if d.rule == rule) for rule in shown_rules
    }
    other = tuple(d for d in shown if d.rule not in shown_rules)
    totals = _dig(report, "totals")
    result = report.get("result")
    return ReconciliationView(
        run=run,
        report_verified=verified,
        report_problems=tuple(dict.fromkeys(report_problems)),
        generated_at=_text(report.get("generated_at")),
        result=result if result in ("PASS", "FAIL") else None,
        rules=rules,
        discrepancies_total=len(items) if isinstance(raw, list) else None,
        discrepancies=shown,
        by_rule=by_rule,
        other_discrepancies=other,
        rows={file: _row_counts(_dig(totals, "rows", file), "loaded") for file in DATA_FILES},
        amounts={
            name: _amount(_dig(totals, "requested_amount", name))
            for name in (
                "source_total", "control_total", "loaded", "excluded", "rejected", "target_total",
            )
        },
        target=_target_counts(_dig(totals, "target")),
        report_version=version if type(version) is int else None,
    )


def _not_performed(run: RunView) -> str | None:
    """Why no reconciliation exists yet, or None if a missing report is itself a problem."""
    record = run.reconciliation
    if (
        not run.manifest.ok
        or record is None
        or record.outcome != "Not run"
        or run.reconciliation_report.state is not FileState.MISSING
        or run.status == "RECONCILED"
        or (run.failure is not None and run.failure.stage == "reconciliation")
    ):
        return None
    if run.status == "LOADED":
        version = _dig(run.manifest.data, "manifest_version")
        if type(version) is int and version < 3:
            return (
                f"The run is loaded but predates row-level record reports (manifest version "
                f"{version}). Reconciling it would be refused, because RC-11 and RC-12 cannot be "
                "evaluated without them; the archived source must be converted again in a new run."
            )
        if run.ready_for_reconciliation:
            return (
                "The run is loaded and ready. An operator reconciles it from the command line "
                f"(python -m loan_lab.conversion.legacy.reconcile_cli {run.run_id}); this page "
                "never starts one."
            )
        return "The run is loaded but not ready for reconciliation; the run summary shows why."
    if run.status == "FAILED":
        stage = run.failure.stage if run.failure is not None else None
        where = f" at stage {stage}" if stage else ""
        return (
            f"The run failed{where} before reconciliation, so it is never reconciled. A corrected "
            "source is converted in a new run."
        )
    return (
        f"The run's status is {run.status or 'not recorded'}. Only a LOADED run can be "
        "reconciled."
    )


def _discrepancy(value: Any) -> DiscrepancyView:
    def text(key: str) -> str | None:
        item = _dig(value, key)
        if isinstance(item, (int, Decimal)) and not isinstance(item, bool):
            return str(item)
        return _text(item)

    return DiscrepancyView(
        rule=text("rule"),
        check=text("check"),
        file=text("file"),
        line=_line(_dig(value, "line")),
        source_key=text("source_key"),
        target_table=text("target_table"),
        target_id=_line(_dig(value, "target_id")),
        field=text("field"),
        expected=text("expected"),
        actual=text("actual"),
        message=text("message"),
        unit_key=text("unit_key"),
        evidence=text("evidence"),
    )


# --- Exception, exclusion, and warning reports ----------------------------------------------------


def report_kind(value: str) -> ReportKind | None:
    try:
        return ReportKind(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class ReportFilters:
    q: str = ""
    file: str = ""
    rule: str = ""
    dependent: str = ""

    @property
    def active(self) -> bool:
        return bool(self.q or self.file or self.rule or self.dependent)


def report_filters(q: str = "", file: str = "", rule: str = "", dependent: str = "") -> ReportFilters:
    """Filters from the query string. Values outside the allowed sets are ignored."""
    return ReportFilters(
        q=q.strip()[:MAX_REPORT_QUERY_LENGTH],
        file=file if file in DATA_FILES else "",
        rule=rule if re.fullmatch(r"[A-Z]{2,3}-[0-9]{2}", rule) else "",
        dependent=dependent if dependent in ("Y", "N") else "",
    )


@dataclass(frozen=True)
class ReportRow:
    file: str
    line: str
    source_key: str
    unit_key: str
    stage: str
    rule: str
    dependent: str
    root_cause: str
    field: str
    source_value: str
    message: str
    remediation: str
    source_line: str


@dataclass(frozen=True)
class ReportView:
    run: RunView
    kind: ReportKind
    file: ReportFile
    # Why the report cannot be shown; empty when it was read and verified.
    problems: tuple[str, ...]
    total: int | None
    rows: tuple[ReportRow, ...]
    matched: int
    filters: ReportFilters
    rules: tuple[str, ...]

    @property
    def title(self) -> str:
        return REPORT_TITLES[self.kind]

    @property
    def description(self) -> str:
        return REPORT_DESCRIPTIONS[self.kind]

    @property
    def available(self) -> bool:
        return not self.problems

    @property
    def truncated(self) -> bool:
        return self.matched > len(self.rows)


def load_report(
    root: Path | None, run_id: str, kind: ReportKind, filters: ReportFilters
) -> ReportView | None:
    """The report's rows matching ``filters``, at most MAX_REPORT_ROWS_SHOWN of them."""
    run = load_run(root, run_id)
    if run is None:
        return None
    file = _report_file(run, kind)
    rows, problems = _read_report(root, run, file)
    matched = [row for row in rows if _matches(row, filters)]
    return ReportView(
        run=run,
        kind=kind,
        file=file,
        problems=tuple(problems),
        total=len(rows) if not problems else None,
        rows=tuple(matched[:MAX_REPORT_ROWS_SHOWN]),
        matched=len(matched),
        filters=filters,
        rules=tuple(sorted({row.rule for row in rows})),
    )


def report_download(root: Path | None, run_id: str, kind: ReportKind) -> bytes | None:
    """A spreadsheet-safe copy of a verified report, or None if it cannot be shown.

    Values that a spreadsheet could run as formulas are prefixed with ``'``. The archived report
    in the evidence directory is never changed and keeps the exact source values.
    """
    run = load_run(root, run_id)
    if run is None:
        return None
    rows, problems = _read_report(root, run, _report_file(run, kind))
    if problems:
        return None
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(REPORT_COLUMNS)
    for row in rows:
        writer.writerow(spreadsheet_safe(value) for value in _values(row))
    # A byte order mark, so spreadsheet programs read the file as UTF-8.
    return "\ufeff".encode() + buffer.getvalue().encode("utf-8")


def _report_file(run: RunView, kind: ReportKind) -> ReportFile:
    recorded = [f for f in run.reports.files if f.kind is kind] if run.reports else []
    if recorded:
        return recorded[0]
    return ReportFile(kind, f"{REPORTS_DIRECTORY}/{kind.file_name}", False, None, None, None)


def _read_report(
    root: Path | None, run: RunView, file: ReportFile
) -> tuple[list[ReportRow], list[str]]:
    reports = run.reports
    if reports is None:
        return [], [f"{MANIFEST_FILE}: {run.manifest.label}."]
    if reports.state == "not_produced":
        return [], ["This run predates the exception, exclusion, and warning reports."]
    if reports.state != ReportState.COMPLETE:
        return [], [f"The reports are not complete (state: {reports.state or 'not recorded'})."]
    if reports.problems:
        return [], list(reports.problems)
    if file.sha256 is None or file.rows is None:
        return [], [f"The manifest does not record {file.path}."]
    run_dir = run_directory(root, run.run_id)
    if run_dir is None:
        return [], ["The run directory is not available."]
    path, state = _evidence_path(run_dir, file.path)
    if path is None:
        return [], [f"{file.path}: {FILE_STATE_LABELS[state or FileState.MISSING]}."]
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_REPORT_BYTES + 1)
    except OSError as error:
        return [], [f"{file.path}: {FILE_STATE_LABELS[FileState.UNREADABLE]} ({type(error).__name__})."]
    if len(raw) > MAX_REPORT_BYTES:
        return [], [f"{file.path} is larger than {MAX_REPORT_BYTES:,} bytes."]
    if hashlib.sha256(raw).hexdigest() != file.sha256:
        return [], [f"{file.path} does not match the checksum recorded in the manifest."]
    try:
        records = list(csv.reader(io.StringIO(raw.decode("utf-8"), newline=""), strict=True))
    except (UnicodeDecodeError, csv.Error):
        return [], [f"{file.path} is not valid UTF-8 CSV."]
    if not records or tuple(records[0]) != REPORT_COLUMNS:
        return [], [f"{file.path} does not have the expected columns."]
    body = records[1:]
    if any(len(record) != len(REPORT_COLUMNS) for record in body):
        return [], [f"{file.path} has a row with the wrong number of fields."]
    if len(body) != file.rows:
        return [], [f"{file.path} holds {len(body):,} rows; the manifest records {file.rows:,}."]
    return [ReportRow(*record) for record in body], []


def _values(row: ReportRow) -> tuple[str, ...]:
    return (
        row.file, row.line, row.source_key, row.unit_key, row.stage, row.rule, row.dependent,
        row.root_cause, row.field, row.source_value, row.message, row.remediation,
        row.source_line,
    )


def _matches(row: ReportRow, filters: ReportFilters) -> bool:
    if filters.file and row.file != filters.file:
        return False
    if filters.rule and row.rule != filters.rule:
        return False
    if filters.dependent and row.dependent != filters.dependent:
        return False
    if filters.q:
        needle = filters.q.casefold()
        return any(needle in value.casefold() for value in _values(row))
    return True
