"""Durable per-run evidence around validation and the Phase 2 load (spec sections 2.2, 11 and 13).

Every run reserves a new ``output/conversion/<run_id>/`` directory before anything else, archives
the source files, and records its lifecycle in ``manifest.json``. Once a plan exists, the
exception, exclusion, and warning reports are written to ``reports/`` before any load, with their
checksums in the manifest; ``reports/load_result.json`` follows once a load was attempted. The
run's status, the state of the database transaction, and the
state of the evidence itself are recorded separately, so a reporting failure can never make a
committed load look as if it never happened:

* ``run_conversion`` validates and plans inside the reserved run, then loads.
* ``run_load`` loads a plan that was built beforehand.
* ``recover_run`` settles a run whose evidence was left incomplete, by inspecting the target
  database read-only. It never reloads data or writes to the database.
* ``fail_run`` formally fails a run that cannot or should not be recovered.
* ``check_ready`` says whether a run's evidence allows reconciliation to start.

Reconciliation itself lives in ``reconcile.py`` and records its outcome here. Release approval is
not implemented.
"""

import copy
import hashlib
import json
import os
import secrets
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.pool import NullPool

from loan_lab.conversion.legacy import contract
from loan_lab.conversion.legacy.contract import Rule
from loan_lab.conversion.legacy.loader import (
    DATABASE_NAME,
    DEFAULT_LOAD_BATCH_SIZE,
    RUN_ID_PATTERN,
    LoadCounts,
    LoadFailedError,
    LoadResult,
    LoadStep,
    TargetPreconditionError,
    load_plan,
)
from loan_lab.conversion.legacy.plan import ConversionPlan, Disposition, Outcome
from loan_lab.conversion.legacy.planner import plan_conversion
from loan_lab.conversion.legacy.reports import (
    COLUMNS,
    REPORT_KINDS,
    ReportState,
    build_reports,
    summary_problems,
)
from loan_lab.conversion.legacy.source import RunIssue, SourceValidationError
from loan_lab.models import ApplicationParty, Borrower, LoanApplication
from loan_lab.paths import default_conversion_root, default_evidence_root

MANIFEST_VERSION = 3
# Runs recorded with an earlier manifest version predate the exception, exclusion, and warning
# reports; their evidence is complete without them.
REPORTS_MANIFEST_VERSION = 3
REPORT_VERSION = 2
SPECIFICATION = "docs/conversion-specification.md (v1 draft)"
MANIFEST_NAME = "manifest.json"
SOURCE_DIRECTORY = "source"
REPORTS_DIRECTORY = "reports"
LOAD_REPORT_NAME = "load_result.json"
SOURCE_FILES = (*contract.DATA_FILES, contract.CONTROL_FILE)

_CHUNK_SIZE = 1 << 20
_LOS_TABLES = {
    "borrowers": Borrower.__table__,
    "applications": LoanApplication.__table__,
    "parties": ApplicationParty.__table__,
}
# SQLite files that exist only while a write transaction is open or was interrupted.
_TRANSACTION_FILES = ("-journal", "-wal")


class RunStatus(StrEnum):
    """Run status (spec section 2.2). Only reconciliation moves a run on from ``LOADED``."""

    STARTED = "STARTED"
    VALIDATED = "VALIDATED"
    # The load may have begun; its outcome is not yet recorded. Needs recover_run if left behind.
    LOADING = "LOADING"
    LOADED = "LOADED"
    # Every reconciliation rule passed; awaiting a human release decision.
    RECONCILED = "RECONCILED"
    FAILED = "FAILED"
    # Recovery could not verify whether the load committed. Needs fail_run.
    UNKNOWN = "UNKNOWN"


# Statuses that recover_run and fail_run never change.
_SETTLED = frozenset({RunStatus.LOADED, RunStatus.RECONCILED, RunStatus.FAILED})


class ReconciliationState(StrEnum):
    """``manifest.reconciliation.state`` (spec section 12.2). The status stays LOADED until FINAL."""

    # Recorded before the report is written: an attempt has started and may have written one.
    IN_PROGRESS = "in_progress"
    # The report was written, but the manifest could not be finalized.
    UNFINALIZED = "unfinalized"
    # Existing reconciliation evidence contradicts the run's evidence. Needs fail_run.
    CONFLICT = "conflict"
    FINAL = "final"


# A LOADED run in one of these states is neither ready nor reconciled.
UNSETTLED_RECONCILIATION = frozenset({
    ReconciliationState.IN_PROGRESS, ReconciliationState.UNFINALIZED, ReconciliationState.CONFLICT,
})


class TransactionState(StrEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    # Verified by inspection: no business rows were committed.
    NOT_COMMITTED = "not_committed"
    UNKNOWN = "unknown"


class EvidenceState(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    # Writing the evidence failed; the manifest or report may not describe the database.
    INCOMPLETE = "incomplete"
    # Recovery inspected the database but could not establish the outcome.
    UNVERIFIED = "unverified"


class LoadOutcome(StrEnum):
    NOT_STARTED = "not_started"
    # A precondition (RUN-07, RUN-08) failed before anything was written to a database.
    REFUSED = "refused"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    # Stopped by an unexpected error; see database_transaction for what inspection found.
    INTERRUPTED = "interrupted"
    UNKNOWN = "unknown"


class RunFailedError(Exception):
    """The run did not load. Its evidence directory records why.

    ``status`` is FAILED, or UNKNOWN when the database did not confirm the rollback.
    """

    def __init__(
        self, run_id: str, evidence_directory: Path, stage: str, step: str, rule: Rule | None,
        reason: str, status: RunStatus = RunStatus.FAILED,
    ) -> None:
        self.run_id = run_id
        self.evidence_directory = evidence_directory
        self.stage = stage
        self.step = step
        self.rule = rule
        self.reason = reason
        self.status = status
        code = f"{rule} " if rule else ""
        super().__init__(
            f"Run {run_id} {status} at stage {stage} ({step}): {code}{reason} "
            f"Evidence: {evidence_directory}"
        )


class SourceRunFailedError(RunFailedError):
    """Source validation failed (RUN-01 to RUN-06). No plan and no database exist for the run."""

    def __init__(self, run_id: str, evidence_directory: Path, issues: tuple[RunIssue, ...]) -> None:
        self.issues = issues
        super().__init__(
            run_id, evidence_directory, "validation", "validate_source", issues[0].rule,
            "; ".join(str(issue) for issue in issues),
        )


class LoadRunFailedError(RunFailedError):
    """The run is FAILED at stage ``load``. ``__cause__`` is the underlying error."""

    def __init__(
        self, run_id: str, evidence_directory: Path, step: str, rule: Rule | None, reason: str,
        status: RunStatus = RunStatus.FAILED,
    ) -> None:
        super().__init__(run_id, evidence_directory, "load", step, rule, reason, status)


class ReportRunFailedError(RunFailedError):
    """The exception, exclusion, or warning reports could not be written; nothing was loaded."""

    def __init__(self, run_id: str, evidence_directory: Path, reason: str) -> None:
        super().__init__(run_id, evidence_directory, "reports", "write_reports", None, reason)


class EvidenceIncompleteError(Exception):
    """The load committed, but its evidence could not be completed.

    The run is not ready for reconciliation. Run :func:`recover_run` (or :func:`fail_run`).
    """

    def __init__(self, run_id: str, evidence_directory: Path, error: BaseException) -> None:
        self.run_id = run_id
        self.evidence_directory = evidence_directory
        self.error = f"{type(error).__name__}: {error}"
        super().__init__(
            f"Run {run_id} committed its load, but writing the evidence failed ({self.error}). "
            f"The run is not ready for reconciliation; recover it. Evidence: {evidence_directory}"
        )


class EvidenceUnreadableError(Exception):
    """A run's manifest cannot be read, so its state cannot be established."""


class SourceChangedError(Exception):
    """RUN-08: a source file differs from the bytes the plan was built from."""

    def __init__(self, issues: tuple[RunIssue, ...]) -> None:
        self.issues = issues
        super().__init__("\n".join(str(issue) for issue in issues))


@dataclass(frozen=True)
class ArchivedSource:
    name: str
    # None when no plan exists (source validation failed).
    planned_sha256: str | None
    # None when the file could not be archived.
    source_sha256: str | None
    archived_sha256: str | None
    size: int | None
    error: str | None = None

    @property
    def verified(self) -> bool:
        return (
            self.planned_sha256 is not None
            and self.planned_sha256 == self.source_sha256 == self.archived_sha256
        )


@dataclass(frozen=True)
class DatabaseState:
    """What the conversion database holds, read back through a read-only connection."""

    path: Path
    exists: bool
    tables: tuple[str, ...]
    # None unless all three LOS tables exist.
    counts: LoadCounts | None
    requested_amount: Decimal | None
    sha256: str | None

    @property
    def business_rows(self) -> int:
        if self.counts is None:
            return 0
        return self.counts.borrowers + self.counts.applications + self.counts.parties


@dataclass(frozen=True)
class LoadRun:
    """A committed, fully evidenced load. Its status is LOADED; reconciliation has not run."""

    run_id: str
    evidence_directory: Path
    sources: tuple[ArchivedSource, ...]
    result: LoadResult
    database: DatabaseState
    plan: ConversionPlan
    status: RunStatus = RunStatus.LOADED

    @property
    def manifest_path(self) -> Path:
        return self.evidence_directory / MANIFEST_NAME

    @property
    def report_path(self) -> Path:
        return self.evidence_directory / REPORTS_DIRECTORY / LOAD_REPORT_NAME


@dataclass(frozen=True)
class RecoveryResult:
    run_id: str
    status: RunStatus
    database_transaction: TransactionState
    explanation: str
    # False when the run was already final and nothing was written.
    changed: bool


@dataclass(frozen=True)
class Readiness:
    run_id: str
    problems: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.problems


# --- Entry points --------------------------------------------------------------------------


def run_conversion(
    source_directory: Path,
    run_id: str,
    *,
    conversion_root: Path | None = None,
    evidence_root: Path | None = None,
    batch_size: int = DEFAULT_LOAD_BATCH_SIZE,
) -> LoadRun:
    """Reserve the run, archive the source, validate and plan it, write its reports, then load it.

    A source validation failure raises :class:`SourceRunFailedError` after recording the
    run-level errors; no target database is created for an invalid extract. A failure to write
    the reports raises :class:`ReportRunFailedError` before any database is created.
    """
    _check_arguments(run_id, batch_size)
    evidence = _Evidence.reserve(_root(evidence_root, default_evidence_root), run_id,
                                 source_directory)
    evidence.save_manifest()
    step = "archive_source"
    try:
        sources = _archive_sources(source_directory, evidence.directory / SOURCE_DIRECTORY)
        evidence.record_sources(sources)
        evidence.save_manifest()
        step = "validate_source"
        try:
            plan = plan_conversion(source_directory)
        except SourceValidationError as error:
            evidence.fail_validation(error.issues)
            raise SourceRunFailedError(run_id, evidence.directory, error.issues) from error
        evidence.validated(plan)
        evidence.save_manifest()
    except RunFailedError:
        raise
    except BaseException as error:
        evidence.fail_unexpected("validation", step, error)
        raise
    _write_reports(evidence, plan)
    return _verify_and_load(evidence, plan, sources, conversion_root, batch_size)


def run_load(
    plan: ConversionPlan,
    source_directory: Path,
    run_id: str,
    *,
    conversion_root: Path | None = None,
    evidence_root: Path | None = None,
    batch_size: int = DEFAULT_LOAD_BATCH_SIZE,
) -> LoadRun:
    """Load a plan built beforehand from ``source_directory``, keeping durable evidence.

    The source must still match the plan byte for byte (RUN-08). Raises
    :class:`TargetPreconditionError` without writing anything if the evidence directory already
    exists, :class:`ReportRunFailedError` if the reports cannot be written (nothing is loaded),
    :class:`LoadRunFailedError` after recording a failure, and
    :class:`EvidenceIncompleteError` if the load committed but its evidence could not be written.
    """
    _check_arguments(run_id, batch_size)
    evidence = _Evidence.reserve(_root(evidence_root, default_evidence_root), run_id,
                                 source_directory)
    evidence.validated(plan)
    evidence.save_manifest()
    _write_reports(evidence, plan)
    try:
        sources = _archive_sources(source_directory, evidence.directory / SOURCE_DIRECTORY)
    except BaseException as error:
        evidence.fail_unexpected("load", "archive_source", error)
        raise
    return _verify_and_load(evidence, plan, sources, conversion_root, batch_size)


def recover_run(run_id: str, *, evidence_root: Path | None = None) -> RecoveryResult:
    """Settle a run left in a non-final state, from a read-only inspection of its database.

    Never reloads data, modifies the target database, or touches another run. Only rewrites the
    run's own manifest and load report.
    """
    evidence = _Evidence.open(_run_directory(run_id, evidence_root))
    manifest = evidence.manifest
    status = RunStatus(manifest["status"])
    recorded = TransactionState(manifest["database_transaction"])
    if status in _SETTLED:
        return RecoveryResult(run_id, status, recorded, "The run is already final.", False)

    if status is RunStatus.STARTED:
        reason = "Source validation was interrupted. No database is created before it passes."
        evidence.recovered_failure("validation", reason, TransactionState.NOT_STARTED, None)
        return RecoveryResult(run_id, RunStatus.FAILED, TransactionState.NOT_STARTED, reason, True)
    if status is RunStatus.VALIDATED:
        # LOADING is always recorded before the load starts, so no load was attempted.
        reason = "The run was interrupted before its load started."
        evidence.recovered_failure("load", reason, TransactionState.NOT_STARTED, None)
        return RecoveryResult(run_id, RunStatus.FAILED, TransactionState.NOT_STARTED, reason, True)

    state, database, explanation = evidence.classify(recorded)
    if state is TransactionState.COMMITTED:
        assert database is not None
        evidence.recovered_loaded(database, explanation)
        return RecoveryResult(run_id, RunStatus.LOADED, state, explanation, True)
    if state is TransactionState.NOT_COMMITTED:
        evidence.recovered_failure("load", explanation, state, database)
        return RecoveryResult(run_id, RunStatus.FAILED, state, explanation, True)
    evidence.recovered_unknown(database, explanation)
    return RecoveryResult(run_id, RunStatus.UNKNOWN, state, explanation, True)


def fail_run(run_id: str, reason: str, *, evidence_root: Path | None = None) -> None:
    """Formally fail a non-final run (for example UNKNOWN). The database is left untouched.

    A LOADED run whose reconciliation is unsettled (in progress, unfinalized, or in conflict) can
    also be failed, at stage ``reconciliation``; its reconciliation evidence is kept as it is.
    """
    evidence = _Evidence.open(_run_directory(run_id, evidence_root))
    status = RunStatus(evidence.manifest["status"])
    unsettled = (
        status is RunStatus.LOADED
        and reconciliation_state(evidence.manifest) in UNSETTLED_RECONCILIATION
    )
    if status in _SETTLED and not unsettled:
        raise ValueError(f"Run {run_id} is already {status}; a final run is never changed.")
    stage = (
        "validation" if status is RunStatus.STARTED
        else "reconciliation" if unsettled
        else "load"
    )
    evidence.formally_failed(stage, reason)


def reconciliation_state(manifest: Mapping[str, Any]) -> ReconciliationState | None:
    record = manifest.get("reconciliation")
    if not isinstance(record, Mapping) or "state" not in record:
        return None
    return ReconciliationState(record["state"])


def check_ready(run_id: str, *, evidence_root: Path | None = None) -> Readiness:
    """Whether the run's evidence is complete and consistent enough for reconciliation to start."""
    directory = _run_directory(run_id, evidence_root)
    try:
        manifest = _read_json(directory / MANIFEST_NAME)
    except EvidenceUnreadableError as error:
        return Readiness(run_id, (str(error),))
    problems = list(load_evidence_problems(run_id, directory, manifest))
    if manifest.get("ready_for_reconciliation") is not True:
        problems.append("The manifest does not mark the run ready for reconciliation.")
    state = reconciliation_state(manifest)
    if state is not None:
        problems.append(f"A reconciliation attempt exists (state {state}).")
    return Readiness(run_id, tuple(problems))


def load_evidence_problems(
    run_id: str, directory: Path, manifest: Mapping[str, Any]
) -> tuple[str, ...]:
    """Problems with the run's load evidence: status, transaction, source, report, database."""
    problems = []
    if manifest.get("status") != RunStatus.LOADED:
        problems.append(f"Status is {manifest.get('status')}, not LOADED.")
    if manifest.get("database_transaction") != TransactionState.COMMITTED:
        problems.append(
            f"Database transaction is {manifest.get('database_transaction')}, not committed."
        )
    if (manifest.get("evidence") or {}).get("state") != EvidenceState.COMPLETE:
        problems.append("Evidence is not complete.")
    if (manifest.get("source") or {}).get("verified") is not True:
        problems.append("The archived source is not verified against the plan.")

    recorded_sha = (manifest.get("database") or {}).get("sha256")
    try:
        report = _read_json(directory / REPORTS_DIRECTORY / LOAD_REPORT_NAME)
    except EvidenceUnreadableError as error:
        problems.append(str(error))
    else:
        if report.get("success") is not True or report.get("run_id") != run_id:
            problems.append("The load report does not record a successful load of this run.")
        if (report.get("database") or {}).get("sha256") != recorded_sha:
            problems.append("The load report and manifest disagree about the database checksum.")

    database_path = manifest.get("database_path")
    if not database_path or not Path(database_path).is_file():
        problems.append("The conversion database is missing.")
    elif recorded_sha is None or _sha256(Path(database_path)) != recorded_sha:
        problems.append("The conversion database no longer matches its recorded checksum.")
    problems.extend(report_evidence_problems(directory, manifest))
    return tuple(problems)


def report_evidence_problems(directory: Path, manifest: Mapping[str, Any]) -> list[str]:
    """Problems with the exception, exclusion, and warning reports of a run that requires them."""
    version = manifest.get("manifest_version")
    if type(version) is not int or version < REPORTS_MANIFEST_VERSION:
        return []
    record = manifest.get("reports")
    problems = summary_problems(record, manifest.get("validation"))
    if record is None or not isinstance(record.get("files"), Mapping):
        return problems
    for kind in REPORT_KINDS:
        entry = record["files"].get(kind)
        path = directory / REPORTS_DIRECTORY / kind.file_name
        if not isinstance(entry, Mapping):
            continue
        if not path.is_file() or path.is_symlink():
            problems.append(f"{REPORTS_DIRECTORY}/{kind.file_name} is missing.")
        elif _sha256(path) != entry.get("sha256"):
            problems.append(
                f"{REPORTS_DIRECTORY}/{kind.file_name} does not match its recorded checksum."
            )
    return problems


# --- Reports -------------------------------------------------------------------------------


def _write_reports(evidence: "_Evidence", plan: ConversionPlan) -> None:
    """Write the reports before any load. A failure fails the run; nothing is loaded."""
    try:
        evidence.write_reports(plan)
    except BaseException as error:
        reason = f"{type(error).__name__}: {error}"
        evidence.reports_failed(reason)
        if not isinstance(error, Exception):
            raise
        raise ReportRunFailedError(evidence.run_id, evidence.directory, reason) from error


# --- Load with evidence --------------------------------------------------------------------


def _verify_and_load(
    evidence: "_Evidence",
    plan: ConversionPlan,
    copies: tuple[ArchivedSource, ...],
    conversion_root: Path | None,
    batch_size: int,
) -> LoadRun:
    run_id, directory = evidence.run_id, evidence.directory
    step = "verify_source"
    try:
        sources = tuple(
            ArchivedSource(
                c.name, plan.source_checksums[c.name], c.source_sha256, c.archived_sha256, c.size,
                c.error,
            )
            for c in copies
        )
        evidence.record_sources(sources)
        if issues := _source_issues(sources):
            error = SourceChangedError(issues)
            evidence.fail("verify_source", Rule.RUN_08, str(error), LoadOutcome.REFUSED)
            raise LoadRunFailedError(
                run_id, directory, "verify_source", Rule.RUN_08, str(error)
            ) from error

        # Recorded before the load starts, so the manifest never says VALIDATED afterwards.
        root = _root(conversion_root, default_conversion_root)
        evidence.loading(root / run_id / DATABASE_NAME)
        step = "load"
        try:
            result = load_plan(plan, run_id, conversion_root=root, batch_size=batch_size)
        except TargetPreconditionError as error:
            # The existing directory or file belongs to someone else: never inspect or hash it.
            evidence.fail(
                "reserve_database", Rule.RUN_07, error.issue.message, LoadOutcome.REFUSED,
                database_path=None,
            )
            raise LoadRunFailedError(
                run_id, directory, "reserve_database", Rule.RUN_07, error.issue.message
            ) from error
        except LoadFailedError as error:
            evidence.load_failed(error)
            raise LoadRunFailedError(
                run_id, directory, str(error.step), None, error.error,
                RunStatus(evidence.manifest["status"]),
            ) from error
    except LoadRunFailedError:
        raise
    except BaseException as error:
        evidence.fail_unexpected("load", step, error)
        raise

    # The load committed. From here on, an evidence failure must not hide that.
    try:
        database = inspect_database(result.database_path)
        evidence.succeed(result, database)
    except Exception as error:
        evidence.post_commit_failure(error)
        raise EvidenceIncompleteError(run_id, directory, error) from error
    return LoadRun(run_id, directory, sources, result, database, plan)


def _check_arguments(run_id: str, batch_size: int) -> None:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError(f"Invalid run ID {run_id!r}: use 1-64 letters, digits, '-' or '_'.")
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")


def _root(root: Path | None, default: Any) -> Path:
    return default() if root is None else root


def _run_directory(run_id: str, evidence_root: Path | None) -> Path:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError(f"Invalid run ID {run_id!r}: use 1-64 letters, digits, '-' or '_'.")
    return _root(evidence_root, default_evidence_root) / run_id


# --- Source archive ------------------------------------------------------------------------


def _archive_sources(source_directory: Path, archive: Path) -> tuple[ArchivedSource, ...]:
    """Copy each regular source file byte for byte, hashing what was read and what was written.

    Missing, non-regular, or unreadable files are recorded with an error instead of a copy.
    """
    archive.mkdir()
    archived = []
    for name in SOURCE_FILES:
        source = source_directory / name
        source_sha256 = archived_sha256 = size = error = None
        if not source.exists():
            error = "missing"
        elif not source.is_file():
            error = "not a regular file"
        else:
            try:
                source_sha256, size = _copy(source, archive / name)
                archived_sha256 = _sha256(archive / name)
            except OSError as exc:
                source_sha256 = size = None
                error = f"unreadable: {exc.strerror or exc}"
        archived.append(ArchivedSource(name, None, source_sha256, archived_sha256, size, error))
    return tuple(archived)


def _copy(source: Path, target: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
    try:
        with source.open("rb") as reader, temporary.open("xb") as writer:
            while chunk := reader.read(_CHUNK_SIZE):
                digest.update(chunk)
                writer.write(chunk)
                size += len(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return digest.hexdigest(), size


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as reader:
        while chunk := reader.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _source_issues(sources: tuple[ArchivedSource, ...]) -> tuple[RunIssue, ...]:
    issues = []
    for source in sources:
        if source.source_sha256 is None:
            message = f"The file could not be read at load time ({source.error})."
        elif source.source_sha256 != source.planned_sha256:
            message = (
                f"Changed since planning: SHA-256 {source.source_sha256}, "
                f"planned {source.planned_sha256}."
            )
        elif source.archived_sha256 != source.source_sha256:
            message = f"The archived copy does not match the source ({source.archived_sha256})."
        else:
            continue
        issues.append(RunIssue(Rule.RUN_08, source.name, message))
    return tuple(issues)


# --- Database read-back --------------------------------------------------------------------


def inspect_database(path: Path) -> DatabaseState:
    """Count LOS rows and sum requested amounts without any possibility of writing."""
    if not path.is_file():
        return DatabaseState(path, False, (), None, None, None)

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        connection.execute("PRAGMA query_only = ON")
        return connection

    engine = create_engine("sqlite://", creator=connect, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            tables = tuple(sorted(inspect(conn).get_table_names()))
            counts = amount = None
            if all(table.name in tables for table in _LOS_TABLES.values()):
                counts = LoadCounts(**{
                    name: conn.execute(select(func.count()).select_from(table)).scalar_one()
                    for name, table in _LOS_TABLES.items()
                })
                total = conn.execute(select(func.sum(LoanApplication.requested_amount))).scalar()
                amount = Decimal("0.00") if total is None else total
    finally:
        engine.dispose()
    return DatabaseState(path, True, tables, counts, amount, _sha256(path))


def classify_database(
    path: Path | None, expected: LoadCounts, expected_amount: Decimal, recorded: TransactionState
) -> tuple[TransactionState, DatabaseState | None, str]:
    """Establish from a read-only inspection whether a load committed.

    Returns COMMITTED, NOT_COMMITTED, or UNKNOWN; anything that cannot be verified is UNKNOWN.
    """
    if path is None:
        return TransactionState.NOT_COMMITTED, None, "No conversion database exists for the run."
    if not path.exists():
        if recorded is TransactionState.COMMITTED:
            return TransactionState.UNKNOWN, None, (
                f"The load was recorded as committed, but {path} is missing."
            )
        return TransactionState.NOT_COMMITTED, None, "The conversion database was never created."
    leftovers = [
        path.name + suffix
        for suffix in _TRANSACTION_FILES
        if path.with_name(path.name + suffix).exists()
    ]
    if leftovers:
        return TransactionState.UNKNOWN, None, (
            f"An interrupted transaction left {', '.join(leftovers)}; resolving it would require "
            "writing to the database."
        )
    try:
        database = inspect_database(path)
    except Exception as error:
        return TransactionState.UNKNOWN, None, (
            f"The database could not be read: {type(error).__name__}: {error}"
        )
    planned_rows = expected.borrowers + expected.applications + expected.parties
    if database.counts is None or (database.business_rows == 0 and planned_rows > 0):
        if recorded is TransactionState.COMMITTED:
            return TransactionState.UNKNOWN, database, (
                "The load was recorded as committed, but the database holds no business rows."
            )
        return TransactionState.NOT_COMMITTED, database, (
            "The database holds no business rows, so the load did not commit."
        )
    if planned_rows and database.counts == expected and database.requested_amount == expected_amount:
        return TransactionState.COMMITTED, database, (
            "The database holds exactly the planned rows and requested amount."
        )
    return TransactionState.UNKNOWN, database, (
        f"The database holds {_describe(database.counts, database.requested_amount)}; the plan "
        f"expected {_describe(expected, expected_amount)}."
    )


def _describe(counts: LoadCounts | None, amount: Decimal | None) -> str:
    if counts is None:
        return "no LOS tables"
    return (
        f"{counts.borrowers} borrowers, {counts.applications} applications, {counts.parties} "
        f"parties, requested amount {amount}"
    )


# --- Manifest and load report --------------------------------------------------------------


class _Evidence:
    """Builds manifest.json and reports/load_result.json, replacing each file atomically."""

    def __init__(self, directory: Path, manifest: dict[str, Any]) -> None:
        self.directory = directory
        self.manifest = manifest
        self.run_id: str = manifest["run_id"]

    @classmethod
    def reserve(cls, root: Path, run_id: str, source_directory: Path) -> "_Evidence":
        directory = root / run_id
        try:
            directory.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            raise TargetPreconditionError(
                directory,
                f"Evidence directory {directory} already exists; a run ID is never reused.",
            ) from None
        now = _utc_now()
        return cls(directory, {
            "manifest_version": MANIFEST_VERSION,
            "specification": SPECIFICATION,
            "run_id": run_id,
            "status": RunStatus.STARTED,
            "failure": None,
            "started_at": now,
            "updated_at": now,
            "finished_at": None,
            "database_transaction": TransactionState.NOT_STARTED,
            "evidence": {"state": EvidenceState.IN_PROGRESS, "error": None, "recovered_at": None},
            "ready_for_reconciliation": False,
            "source": {
                "directory": str(source_directory.resolve()),
                "archive": SOURCE_DIRECTORY,
                "verified": None,
                "files": {},
            },
            "validation": {"run_level_checks": "not_run"},
            "expected_target": None,
            "database_path": None,
            "database": None,
            "load": {"outcome": LoadOutcome.NOT_STARTED, "report": None},
            "reports": {"state": ReportState.NOT_STARTED, "files": {}, "error": None},
            "reconciliation": None,
            "release": None,
        })

    @classmethod
    def open(cls, directory: Path) -> "_Evidence":
        return cls(directory, _read_json(directory / MANIFEST_NAME))

    # Lifecycle steps.

    def validated(self, plan: ConversionPlan) -> None:
        counts = LoadCounts(
            len(plan.borrowers_to_load), len(plan.applications_to_load), len(plan.parties_to_load)
        )
        self.manifest.update(
            status=RunStatus.VALIDATED,
            validation=_validation_summary(plan),
            expected_target={**_counts(counts), "requested_amount": str(plan.amounts.eligible)},
        )
        for name in SOURCE_FILES:
            entry = self.manifest["source"]["files"].setdefault(name, {})
            entry["planned_sha256"] = plan.source_checksums[name]
        self.manifest["reports"] = {"state": ReportState.IN_PROGRESS, "files": {}, "error": None}

    def write_reports(self, plan: ConversionPlan) -> None:
        """Write each report atomically, verify it on disk, then record the set as complete."""
        record = self.manifest["reports"]
        reports = build_reports(plan)
        directory = self.directory / REPORTS_DIRECTORY
        directory.mkdir(exist_ok=True)
        for report in reports:
            relative = f"{REPORTS_DIRECTORY}/{report.kind.file_name}"
            write_bytes_atomic(directory / report.kind.file_name, report.data)
            expected = hashlib.sha256(report.data).hexdigest()
            if _sha256(directory / report.kind.file_name) != expected:
                raise OSError(f"{relative} does not read back as written.")
            record["files"][report.kind] = {
                "path": relative, "sha256": expected, "bytes": len(report.data),
                **report.summary(),
            }
        record.update(state=ReportState.COMPLETE, columns=list(COLUMNS))
        self.save_manifest()

    def reports_failed(self, reason: str) -> None:
        """Fail the run before any load; reports already written stay as evidence. Best effort."""
        try:
            self.manifest["reports"].update(state=ReportState.FAILED, error=reason)
            self._final_manifest(
                RunStatus.FAILED,
                {"stage": "reports", "step": "write_reports", "rule": None, "reason": reason},
                evidence=EvidenceState.INCOMPLETE, evidence_error=reason,
            )
        except Exception:  # noqa: BLE001, S110
            # The manifest still says VALIDATED with reports in progress, which is not complete.
            pass

    def record_sources(self, sources: tuple[ArchivedSource, ...]) -> None:
        planned = all(s.planned_sha256 is not None for s in sources)
        self.manifest["source"]["verified"] = all(s.verified for s in sources) if planned else None
        for source in sources:
            entry = {
                "planned_sha256": source.planned_sha256,
                "source_sha256": source.source_sha256,
                "archived_sha256": source.archived_sha256,
                "bytes": source.size,
                "verified": source.verified,
            }
            if source.error:
                entry["error"] = source.error
            self.manifest["source"]["files"][source.name] = entry

    def loading(self, database_path: Path) -> None:
        self.manifest.update(
            status=RunStatus.LOADING,
            database_transaction=TransactionState.IN_PROGRESS,
            database_path=str(database_path),
            load={"outcome": LoadOutcome.NOT_STARTED, "report": None},
        )
        self.save_manifest()

    def fail_validation(self, issues: tuple[RunIssue, ...]) -> None:
        self.manifest["validation"] = {
            "run_level_checks": "failed",
            "issues": [
                {"rule": i.rule, "file": i.file, "line": i.line, "message": i.message}
                for i in issues
            ],
        }
        reason = "; ".join(str(issue) for issue in issues)
        self._final_manifest(
            RunStatus.FAILED,
            {"stage": "validation", "step": "validate_source", "rule": issues[0].rule,
             "reason": reason},
        )

    def fail(
        self,
        step: str,
        rule: Rule | None,
        reason: str,
        outcome: LoadOutcome,
        *,
        database_path: Path | None | str = "unchanged",
    ) -> None:
        """A load-stage failure before any database transaction began."""
        if database_path != "unchanged":
            self.manifest["database_path"] = database_path
        self.manifest["database_transaction"] = TransactionState.NOT_STARTED
        failure = {"stage": "load", "step": step, "rule": rule, "reason": reason}
        self._write_report(
            False, failure, None,
            {"schema": TransactionState.NOT_STARTED, "load": TransactionState.NOT_STARTED},
        )
        self._final_manifest(RunStatus.FAILED, failure, outcome=outcome, database=None)

    def load_failed(self, error: LoadFailedError) -> None:
        schema = (
            TransactionState.COMMITTED if error.schema_committed else TransactionState.ROLLED_BACK
        )
        load = (
            TransactionState.NOT_STARTED if error.step is LoadStep.CREATE_SCHEMA
            else TransactionState.ROLLED_BACK
        )
        state, database, explanation = self.classify(TransactionState.ROLLED_BACK)
        failure = {"stage": "load", "step": str(error.step), "rule": None, "reason": error.error}
        if state is not TransactionState.NOT_COMMITTED:
            # The loader reported a rollback, but the database does not confirm it.
            self.manifest["database_transaction"] = TransactionState.UNKNOWN
            failure["reason"] += f" Unverified: {explanation}"
            self._write_report(None, failure, database, {"schema": schema, "load": load})
            self._final_manifest(
                RunStatus.UNKNOWN, None, outcome=LoadOutcome.UNKNOWN, database=database,
                evidence=EvidenceState.UNVERIFIED, evidence_error=failure["reason"],
            )
            return
        self.manifest["database_transaction"] = TransactionState.ROLLED_BACK
        self._write_report(False, failure, database, {"schema": schema, "load": load})
        self._final_manifest(
            RunStatus.FAILED, failure, outcome=LoadOutcome.ROLLED_BACK, database=database
        )

    def fail_unexpected(self, stage: str, step: str, error: BaseException) -> None:
        """Best effort, so the original error always propagates."""
        reason = f"{type(error).__name__}: {error}"
        try:
            if stage == "load" and step == "load":
                self._settle_interrupted_load(reason)
            elif stage == "validation":
                self._final_manifest(
                    RunStatus.FAILED,
                    {"stage": stage, "step": step, "rule": None, "reason": reason},
                )
            else:
                self.fail(step, None, reason, LoadOutcome.NOT_STARTED)
        except Exception:  # noqa: BLE001, S110
            pass

    def succeed(self, result: LoadResult, database: DatabaseState) -> None:
        self.manifest["database_transaction"] = TransactionState.COMMITTED
        self._write_report(
            True, None, database,
            {"schema": TransactionState.COMMITTED, "load": TransactionState.COMMITTED},
            inserted=_counts(result.counts),
        )
        self._final_manifest(
            RunStatus.LOADED, None, outcome=LoadOutcome.COMMITTED, database=database
        )

    def post_commit_failure(self, error: BaseException) -> None:
        """Record, if at all possible, that the load committed but its evidence is incomplete."""
        try:
            self.manifest.update(
                status=RunStatus.LOADING,
                database_transaction=TransactionState.COMMITTED,
                evidence={
                    "state": EvidenceState.INCOMPLETE,
                    "error": f"{type(error).__name__}: {error}",
                    "recovered_at": None,
                },
                ready_for_reconciliation=False,
                load={"outcome": LoadOutcome.COMMITTED, "report": None},
            )
            self.save_manifest()
        except Exception:  # noqa: BLE001, S110
            # The manifest still says LOADING from before the load, which is not misleading.
            pass

    # Recovery.

    def classify(
        self, recorded: TransactionState
    ) -> tuple[TransactionState, DatabaseState | None, str]:
        expected = self.manifest.get("expected_target")
        if not expected:
            return TransactionState.UNKNOWN, None, "The manifest has no expected target counts."
        path = self.manifest.get("database_path")
        return classify_database(
            Path(path) if path else None,
            LoadCounts(expected["borrowers"], expected["applications"], expected["parties"]),
            Decimal(expected["requested_amount"]),
            recorded,
        )

    def recovered_loaded(self, database: DatabaseState, explanation: str) -> None:
        self.manifest["database_transaction"] = TransactionState.COMMITTED
        self._write_report(
            True, None, database,
            {"schema": TransactionState.COMMITTED, "load": TransactionState.COMMITTED},
            recovered=explanation,
        )
        self._final_manifest(
            RunStatus.LOADED, None, outcome=LoadOutcome.COMMITTED, database=database,
            recovered=True,
        )

    def recovered_failure(
        self, stage: str, reason: str, state: TransactionState, database: DatabaseState | None
    ) -> None:
        self.manifest["database_transaction"] = state
        failure = {"stage": stage, "step": "recovered", "rule": None, "reason": reason}
        outcome = (
            LoadOutcome.NOT_STARTED if state is TransactionState.NOT_STARTED
            else LoadOutcome.INTERRUPTED
        )
        if stage == "load":
            schema = (
                TransactionState.COMMITTED if database is not None and database.counts is not None
                else TransactionState.NOT_COMMITTED if state is not TransactionState.NOT_STARTED
                else TransactionState.NOT_STARTED
            )
            self._write_report(
                False, failure, database, {"schema": schema, "load": state}, recovered=reason
            )
        self._final_manifest(
            RunStatus.FAILED, failure, outcome=outcome, database=database, recovered=True
        )

    def recovered_unknown(self, database: DatabaseState | None, explanation: str) -> None:
        self.manifest["database_transaction"] = TransactionState.UNKNOWN
        self._write_report(
            None, None, database,
            {"schema": TransactionState.UNKNOWN, "load": TransactionState.UNKNOWN},
            recovered=explanation,
        )
        self._final_manifest(
            RunStatus.UNKNOWN, None, outcome=LoadOutcome.UNKNOWN, database=database,
            evidence=EvidenceState.UNVERIFIED, evidence_error=explanation, recovered=True,
        )

    def formally_failed(self, stage: str, reason: str) -> None:
        failure = {"stage": stage, "step": "formally_failed", "rule": None, "reason": reason}
        evidence = self.manifest["evidence"]
        self._final_manifest(
            RunStatus.FAILED, failure, evidence=EvidenceState(evidence["state"]),
            evidence_error=evidence.get("error"),
        )

    # Reconciliation.

    def reconciliation_started(self, attempt: str) -> dict[str, Any]:
        """Mark the attempt before any report exists. Returns the manifest as it was, for undo."""
        before = copy.deepcopy(self.manifest)
        self.manifest.update(
            reconciliation={
                "state": ReconciliationState.IN_PROGRESS,
                "attempt": attempt,
                "started_at": _utc_now(),
            },
            ready_for_reconciliation=False,
        )
        try:
            self.save_manifest()
        except BaseException:
            self.manifest = before
            raise
        return before

    def reconciliation_abandoned(self, before: dict[str, Any]) -> None:
        """Undo :meth:`reconciliation_started` when no report was written. Best effort."""
        try:
            write_json_atomic(self.directory / MANIFEST_NAME, before)
            self.manifest = before
        except Exception:  # noqa: BLE001, S110
            # The in-progress marker stays; a retry finds no report and reconciles afresh.
            pass

    def reconciled(
        self, passed: bool, summary: dict[str, Any], failure: dict[str, Any] | None
    ) -> None:
        """Finalize a reconciliation. The load evidence and the database are untouched.

        If the manifest cannot be written, records the attempt as ``unfinalized`` (best effort)
        and re-raises; the run stays LOADED and not ready.
        """
        before = copy.deepcopy(self.manifest)
        self.manifest["reconciliation"] = {**summary, "state": ReconciliationState.FINAL}
        try:
            self._final_manifest(RunStatus.RECONCILED if passed else RunStatus.FAILED, failure)
        except BaseException as error:
            self.manifest = before
            self._record_unfinalized(summary, error)
            raise

    def _record_unfinalized(self, summary: dict[str, Any], error: BaseException) -> None:
        try:
            self.manifest["reconciliation"] = {
                **self.manifest["reconciliation"],
                "state": ReconciliationState.UNFINALIZED,
                "report": summary["report"],
                "report_sha256": summary["report_sha256"],
                "result": summary["result"],
                "error": f"{type(error).__name__}: {error}",
            }
            self.manifest["ready_for_reconciliation"] = False
            self.save_manifest()
        except Exception:  # noqa: BLE001, S110
            # The manifest still says in_progress, which is not misleading either.
            pass

    def reconciliation_conflict(self, problems: list[str], report_sha256: str | None) -> None:
        """Record contradictory reconciliation evidence. Best effort; the run stays LOADED."""
        try:
            self.manifest["reconciliation"] = {
                **(self.manifest.get("reconciliation") or {}),
                "state": ReconciliationState.CONFLICT,
                "detected_at": _utc_now(),
                "problems": problems,
                "existing_report_sha256": report_sha256,
            }
            self.manifest["ready_for_reconciliation"] = False
            self.save_manifest()
        except Exception:  # noqa: BLE001, S110
            pass

    def _settle_interrupted_load(self, reason: str) -> None:
        state, database, explanation = self.classify(TransactionState.IN_PROGRESS)
        failure = {"stage": "load", "step": "load", "rule": None, "reason": reason}
        self.manifest["database_transaction"] = state
        transactions = {"schema": TransactionState.UNKNOWN, "load": state}
        if state is TransactionState.NOT_COMMITTED:
            self._write_report(False, failure, database, transactions)
            self._final_manifest(
                RunStatus.FAILED, failure, outcome=LoadOutcome.INTERRUPTED, database=database
            )
        elif state is TransactionState.COMMITTED:
            # Committed, but interrupted before the evidence was finished: needs recover_run.
            self.manifest.update(
                evidence={"state": EvidenceState.INCOMPLETE, "error": reason,
                          "recovered_at": None},
                load={"outcome": LoadOutcome.COMMITTED, "report": None},
            )
            self.save_manifest()
        else:
            self._write_report(None, failure, database, transactions)
            self._final_manifest(
                RunStatus.UNKNOWN, None, outcome=LoadOutcome.UNKNOWN, database=database,
                evidence=EvidenceState.UNVERIFIED, evidence_error=f"{reason} {explanation}",
            )

    # Writing.

    def save_manifest(self) -> None:
        self.manifest["updated_at"] = _utc_now()
        write_json_atomic(self.directory / MANIFEST_NAME, self.manifest)

    def _write_report(
        self,
        success: bool | None,
        failure: dict[str, Any] | None,
        database: DatabaseState | None,
        transactions: dict[str, TransactionState],
        *,
        inserted: dict[str, int] | None = None,
        recovered: str | None = None,
    ) -> None:
        state = TransactionState(self.manifest["database_transaction"])
        loaded = None
        if database is not None and database.counts is not None:
            loaded = {
                **_counts(database.counts), "requested_amount": str(database.requested_amount)
            }
        report = {
            "report_version": REPORT_VERSION,
            "run_id": self.run_id,
            "generated_at": _utc_now(),
            # None when the outcome could not be verified.
            "success": success,
            "status": {True: RunStatus.LOADED, False: RunStatus.FAILED}.get(
                success, RunStatus.UNKNOWN  # type: ignore[arg-type]
            ),
            "expected": self.manifest["expected_target"],
            # Read back from the database after the load, not taken from the plan.
            "loaded": loaded,
            "inserted": inserted,
            "transactions": transactions,
            "database_transaction": state,
            "business_rows_committed": (
                None if state is TransactionState.UNKNOWN
                else state is TransactionState.COMMITTED
            ),
            "database": _database_summary(database),
            "failure": failure,
            "recovered": recovered,
        }
        (self.directory / REPORTS_DIRECTORY).mkdir(exist_ok=True)
        write_json_atomic(self.directory / REPORTS_DIRECTORY / LOAD_REPORT_NAME, report)
        self.manifest["load"] = {
            **self.manifest["load"], "report": f"{REPORTS_DIRECTORY}/{LOAD_REPORT_NAME}"
        }

    def _final_manifest(
        self,
        status: RunStatus,
        failure: dict[str, Any] | None,
        *,
        outcome: LoadOutcome | None = None,
        database: DatabaseState | None | str = "unchanged",
        evidence: EvidenceState = EvidenceState.COMPLETE,
        evidence_error: str | None = None,
        recovered: bool = False,
    ) -> None:
        reports = (self.manifest.get("reports") or {}).get("state")
        if evidence is EvidenceState.COMPLETE and reports in (
            ReportState.IN_PROGRESS, ReportState.FAILED
        ):
            evidence = EvidenceState.INCOMPLETE
            evidence_error = "The exception, exclusion, and warning reports are not complete."
        now = _utc_now()
        self.manifest.update(
            status=status,
            failure=failure,
            finished_at=now,
            evidence={
                "state": evidence,
                "error": evidence_error,
                "recovered_at": now if recovered else self.manifest["evidence"]["recovered_at"],
            },
            ready_for_reconciliation=(
                status is RunStatus.LOADED and evidence is EvidenceState.COMPLETE
                and self.manifest["database_transaction"] == TransactionState.COMMITTED
            ),
        )
        if outcome is not None:
            self.manifest["load"] = {**self.manifest["load"], "outcome": outcome}
        if not isinstance(database, str):
            self.manifest["database"] = _database_summary(database)
        self.save_manifest()


def write_json_atomic(path: Path, data: Mapping[str, Any]) -> None:
    """Write to a new temporary file in the same directory, flush to disk, then replace."""
    write_bytes_atomic(path, (json.dumps(data, indent=2, default=str) + "\n").encode("utf-8"))


def write_bytes_atomic(path: Path, payload: bytes) -> None:
    """Write to a new temporary file in the same directory, flush to disk, then replace."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        with temporary.open("xb") as writer:
            writer.write(payload)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as error:
        raise EvidenceUnreadableError(f"{path} cannot be read: {error}") from error
    if not isinstance(data, dict):
        raise EvidenceUnreadableError(f"{path} does not hold a JSON object.")
    return data


def _validation_summary(plan: ConversionPlan) -> dict[str, Any]:
    control = plan.control
    return {
        "run_level_checks": "passed",
        "extract_date": control.extract_date,
        "control": {
            "record_counts": dict(control.record_counts),
            "amount_total": str(control.amount_total),
            "amount_total_verified": control.amount_total_verified,
        },
        "rows": {
            file: {
                "read": len(plan.rows(file)),
                **{str(d): n for d, n in plan.disposition_counts(file).items()},
                **{str(o): n for o, n in plan.outcome_counts(file).items()
                   if o in (Outcome.EXCLUDED_DEPENDENT, Outcome.REJECTED_DEPENDENT)},
            }
            for file in contract.DATA_FILES
        },
        "requested_amount": {
            str(Disposition.ELIGIBLE): str(plan.amounts.eligible),
            str(Disposition.EXCLUDED): str(plan.amounts.excluded),
            str(Disposition.REJECTED): str(plan.amounts.rejected),
            "unparseable": plan.amounts.unparseable,
        },
        "rejections": len(plan.issues),
        "customers_without_applications": [row.key for row in plan.customers_without_applications],
    }


def _database_summary(database: DatabaseState | None) -> dict[str, Any] | None:
    if database is None:
        return None
    return {
        "path": str(database.path),
        "exists": database.exists,
        "tables": list(database.tables),
        "sha256": database.sha256,
    }


def _counts(counts: LoadCounts) -> dict[str, int]:
    return {
        "borrowers": counts.borrowers,
        "applications": counts.applications,
        "parties": counts.parties,
    }


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
