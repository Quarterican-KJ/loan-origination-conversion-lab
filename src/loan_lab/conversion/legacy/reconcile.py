"""Phase 3: independent reconciliation of a loaded run (spec section 12).

Reconciliation proves that the target database holds exactly what the archived source says it
should. To catch defects in the planner and the loader, it shares as little with them as possible:

* The archived CSV files are read again with this module's own reader, and the control file is
  checked again against them.
* Every expected target value is recomputed from the raw source text with this module's own code
  tables and section 7 transformations. The conversion plan's mapped values are never used.
* The target is queried through a raw, read-only SQLite connection (``mode=ro`` and
  ``query_only``). Stored amounts and rates are read as their scaled integers and converted with
  ``Decimal``, never ``float``.
* Which lines *should* have loaded, and every expected report row, cause, and warning, come from
  :mod:`eligibility`, an independent determination from the archived source (spec 12.3). The
  planner is never run. Its recorded decisions (the record reports and the manifest's counts)
  are actual evidence: RC-11 compares dispositions and target membership with the independent
  answer, and RC-12 compares the report rows themselves.

A run passes only if every rule RC-01 to RC-12 matches. The outcome is written atomically to
``reports/reconciliation.json``, and the run becomes ``RECONCILED`` (awaiting a human release
decision) or ``FAILED`` at stage ``reconciliation``. The manifest records the attempt before the
report is written and is finalized after it, so a report never stands for a finalized outcome on
its own. Reconciliation never writes to the database, never reloads, and never releases or
declines a run.
"""

import csv
import hashlib
import json
import re
import secrets
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass
from decimal import Context, Decimal, Inexact, Rounded, localcontext
from enum import StrEnum
from pathlib import Path
from typing import Any

from loan_lab.conversion.legacy import contract
from loan_lab.conversion.legacy import recorded_reports as recorded_evidence
from loan_lab.conversion.legacy import run as runs
from loan_lab.conversion.legacy.eligibility import (
    Cause,
    Disposition,
    EligibilityResult,
    ExpectedLine,
    ReportRow,
    evaluate_source,
)
from loan_lab.conversion.legacy.independent_rules import STAGES
from loan_lab.conversion.legacy.recorded_reports import (
    EXCEPTIONS,
    EXCLUSIONS,
    WARNINGS,
    RecordedReports,
    RecordedReportsError,
    RecordedRow,
    read_recorded_reports,
)
from loan_lab.conversion.legacy.run import RunStatus, check_ready

REPORT_VERSION = 2
RECONCILIATION_REPORT_NAME = "reconciliation.json"

BORROWERS, APPLICATIONS, PARTIES = contract.DATA_FILES
CONTROL = contract.CONTROL_FILE
_TABLES = {BORROWERS: "borrower", APPLICATIONS: "loan_application", PARTIES: "application_party"}

# Independent restatement of spec sections 4 and 5. Deliberately not taken from contract.py, so a
# wrong code table or format there is detected rather than repeated.
_SOURCE_SYSTEM = "LEGACY_LOS"
_CUST_NO = re.compile(r"[0-9]{8}")
_APPL_NO = re.compile(r"[0-9]{10}")
_AMOUNT = re.compile(r"[0-9]{1,13}\.[0-9]{2}")
_RATE = re.compile(r"[0-9]{6}")
_TERM = re.compile(r"[0-9]{1,3}")
_COUNT = re.compile(r"[0-9]{1,9}")
_INITIAL = re.compile(r"[A-Za-z]")
_BORROWER_TYPES = {"I": "individual", "B": "business"}
_PRODUCTS = {
    "110": "consumer_auto",
    "120": "consumer_personal",
    "210": "residential_mortgage",
    "220": "home_equity",
    "310": "commercial_term",
    "320": "commercial_real_estate",
}
_STATUSES = {
    "P": "draft",
    "S": "submitted",
    "U": "in_review",
    "A": "approved",
    "D": "declined",
    "W": "withdrawn",
}
_ROLES = {"PRI": "primary_borrower", "COB": "co_borrower", "GTR": "guarantor"}
_PRIMARY = "primary_borrower"
_MAX_NAME = 200
_MAX_TERM = 600

# Sums of exact two-place amounts can never round; if one would, raise instead.
_EXACT = Context(prec=60, traps=[Inexact, Rounded])
_ZERO = Decimal("0.00")


class ReconciliationRule(StrEnum):
    RC_01 = "RC-01"
    RC_02 = "RC-02"
    RC_03 = "RC-03"
    RC_04 = "RC-04"
    RC_05 = "RC-05"
    RC_06 = "RC-06"
    RC_07 = "RC-07"
    RC_08 = "RC-08"
    RC_09 = "RC-09"
    RC_10 = "RC-10"
    RC_11 = "RC-11"
    RC_12 = "RC-12"


RC = ReconciliationRule
RULE_TITLES: Mapping[ReconciliationRule, str] = {
    RC.RC_01: "Disposition accounting",
    RC.RC_02: "Control file agreement",
    RC.RC_03: "Target counts",
    RC.RC_04: "Key completeness",
    RC.RC_05: "Amount control totals",
    RC.RC_06: "Distributions",
    RC.RC_07: "Field-level comparison",
    RC.RC_08: "Relationships",
    RC.RC_09: "Primary borrower",
    RC.RC_10: "Customers without converted applications",
    RC.RC_11: "Independent dispositions and target membership",
    RC.RC_12: "Independent report rows and warnings",
}
# The rules a report of each version must record as PASS for a passed result (spec 12.2). A
# version 1 report predates RC-11 and RC-12; it is neither failed for lacking them nor shown as
# having passed them. Any other version does not verify.
REPORT_RULES: Mapping[int, tuple[ReconciliationRule, ...]] = {
    1: tuple(RC)[:10],
    2: tuple(RC),
}


def report_rules(report: Mapping[str, Any]) -> tuple[ReconciliationRule, ...] | None:
    """The rules ``report`` is held to under its own ``report_version``; None if unknown."""
    version = report.get("report_version")
    return REPORT_RULES.get(version) if type(version) is int else None


class ReconciliationRefusedError(Exception):
    """Reconciliation did not start. Nothing was written and the run's status is unchanged."""

    def __init__(self, run_id: str, problems: Iterable[str]) -> None:
        self.run_id = run_id
        self.problems = tuple(problems)
        super().__init__(
            f"Reconciliation of run {run_id} refused: " + " ".join(self.problems)
        )


@dataclass(frozen=True)
class Discrepancy:
    """One reconciliation failure, with the evidence needed to trace it."""

    rule: ReconciliationRule
    # Stable identifier of the specific check within the rule, such as ``field_mismatch``.
    check: str
    message: str
    file: str | None = None
    line: int | None = None
    source_key: str | None = None
    # The line's conversion unit (APPL_NO), when it has one (spec 12.3.3).
    unit_key: str | None = None
    # The recorded evidence holding the disputed value, such as ``reports/exceptions.csv:7``.
    evidence: str | None = None
    target_table: str | None = None
    target_id: int | None = None
    field: str | None = None
    expected: str | None = None
    actual: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "check": self.check,
            "file": self.file,
            "line": self.line,
            "source_key": self.source_key,
            "unit_key": self.unit_key,
            "evidence": self.evidence,
            "target_table": self.target_table,
            "target_id": self.target_id,
            "field": self.field,
            "expected": self.expected,
            "actual": self.actual,
            "message": self.message,
        }


@dataclass(frozen=True)
class RuleResult:
    rule: ReconciliationRule
    # How many items (rows, keys, fields, totals) the rule compared.
    checked: int
    discrepancies: int
    note: str | None = None
    # Comparisons the rule did not make, by kind (RC-12 only, spec 12.4). A rule that left any
    # unmade is never PASS, even with no discrepancies.
    not_evaluated: Mapping[str, int] | None = None

    @property
    def complete(self) -> bool:
        return not any((self.not_evaluated or {}).values())

    @property
    def result(self) -> str:
        """``FAIL`` with any discrepancy; else ``INCOMPLETE`` if comparisons were left unmade."""
        if self.discrepancies:
            return "FAIL"
        return "PASS" if self.complete else "INCOMPLETE"

    @property
    def passed(self) -> bool:
        return self.result == "PASS"

    @property
    def title(self) -> str:
        return RULE_TITLES[self.rule]


@dataclass(frozen=True)
class ReconciliationResult:
    run_id: str
    status: RunStatus
    rules: tuple[RuleResult, ...]
    discrepancies: tuple[Discrepancy, ...]
    report_path: Path
    report: Mapping[str, Any]

    @property
    def passed(self) -> bool:
        return self.status is RunStatus.RECONCILED

    @property
    def failed_rules(self) -> tuple[ReconciliationRule, ...]:
        return tuple(result.rule for result in self.rules if result.result == "FAIL")

    @property
    def incomplete_rules(self) -> tuple[ReconciliationRule, ...]:
        """Rules, failed or not, that left comparisons unmade (spec 12.4)."""
        return tuple(result.rule for result in self.rules if not result.complete)


class ReconciliationNotFinalizedError(Exception):
    """The report was written but the manifest could not be finalized.

    The run stays ``LOADED`` and is not ready, reconciled, or releasable. The provisional report
    is preserved, and :func:`reconcile_run` can be retried to verify and finalize it.
    """

    def __init__(self, run_id: str, report_path: Path, cause: BaseException) -> None:
        self.run_id = run_id
        self.report_path = report_path
        super().__init__(
            f"Run {run_id}: {report_path.name} was written but the manifest could not be "
            f"finalized ({type(cause).__name__}: {cause}). The run is not reconciled; "
            "retry reconciliation to verify and finalize the report."
        )


class ReconciliationConflictError(Exception):
    """Existing reconciliation evidence contradicts the run. It is preserved, never replaced.

    The run stays ``LOADED`` and not ready; resolve it by investigation and ``fail_run``.
    """

    def __init__(self, run_id: str, problems: Iterable[str]) -> None:
        self.run_id = run_id
        self.problems = tuple(problems)
        super().__init__(
            f"Reconciliation evidence for run {run_id} is in conflict: " + " ".join(self.problems)
        )


@dataclass(frozen=True)
class ReconciliationCheck:
    """Result of :func:`verify_reconciliation`. ``verified`` is not release approval."""

    run_id: str
    problems: tuple[str, ...]

    @property
    def verified(self) -> bool:
        return not self.problems


# --- Entry point -----------------------------------------------------------------------------


_REPORT_RELATIVE = f"{runs.REPORTS_DIRECTORY}/{RECONCILIATION_REPORT_NAME}"
# Spec section 9: the only rules applied before exclusions.
_REJECTED_BEFORE_EXCLUSION = frozenset({"SV-01", "SV-09", "SV-10"})


def reconcile_run(run_id: str, *, evidence_root: Path | None = None) -> ReconciliationResult:
    """Reconcile a ``LOADED`` run against its archived source.

    Raises :class:`ReconciliationRefusedError`, writing nothing, if the run is not ready, an
    archived source file no longer matches its recorded checksum, the database differs from
    the checksum recorded when it was loaded, or the run lacks verified row-level record reports
    (manifest version below 3, for example). Otherwise writes ``reports/reconciliation.json``
    and marks the run ``RECONCILED`` (every rule passed) or ``FAILED`` at stage
    ``reconciliation``. Never writes to the database, reloads, releases, or declines.

    Finalization is two-phase: the manifest records the attempt (``in_progress``) before the
    report is written, and the status changes only when the manifest is finalized. If that fails,
    raises :class:`ReconciliationNotFinalizedError`. Calling again on an ``in_progress`` or
    ``unfinalized`` attempt re-checks the load evidence, source, and database, re-runs the
    comparison, and finalizes the existing report only if it matches; contradictory evidence
    raises :class:`ReconciliationConflictError` and is never overwritten.
    """
    directory = runs._run_directory(run_id, evidence_root)
    try:
        evidence = runs._Evidence.open(directory)
    except runs.EvidenceUnreadableError as error:
        raise ReconciliationRefusedError(run_id, [str(error)]) from error
    manifest = evidence.manifest
    record = dict(manifest.get("reconciliation") or {})
    state = runs.reconciliation_state(manifest)
    report_path = directory / runs.REPORTS_DIRECTORY / RECONCILIATION_REPORT_NAME

    retrying = state in (runs.ReconciliationState.IN_PROGRESS, runs.ReconciliationState.UNFINALIZED)
    if state is runs.ReconciliationState.CONFLICT:
        raise ReconciliationConflictError(
            run_id,
            ["The manifest already records conflicting reconciliation evidence.",
             *record.get("problems", [])],
        )
    if retrying:
        problems = runs.load_evidence_problems(run_id, directory, manifest)
    else:
        problems = check_ready(run_id, evidence_root=evidence_root).problems
    if problems:
        raise ReconciliationRefusedError(run_id, problems)

    database = Path(manifest["database_path"])
    recorded_sha = manifest["database"]["sha256"]
    archive = directory / runs.SOURCE_DIRECTORY
    if problems := _precondition_problems(archive, manifest, database):
        raise ReconciliationRefusedError(run_id, problems)
    try:
        recorded = read_recorded_reports(directory, manifest)
    except (OSError, RecordedReportsError) as error:
        raise ReconciliationRefusedError(
            run_id, getattr(error, "problems", None) or [f"The record reports cannot be read: {error}"]
        ) from error

    if not retrying and report_path.exists():
        _conflict(evidence, report_path, [
            f"{_REPORT_RELATIVE} exists but the manifest records no reconciliation attempt."
        ])

    try:
        source = read_archive(archive)
        expected = evaluate_source(
            {name: [line.raw for line in source[name]] for name in contract.DATA_FILES}
        )
    except (OSError, ValueError) as error:
        raise ReconciliationRefusedError(
            run_id, [f"The archived source cannot be read: {type(error).__name__}: {error}"]
        ) from error
    target = read_target(database)
    if runs._sha256(database) != recorded_sha:
        raise ReconciliationRefusedError(
            run_id, ["The conversion database changed while it was being reconciled."]
        )

    comparison = _Comparison(source, expected, recorded, target, manifest)
    comparison.run()
    rules = comparison.rule_results()
    discrepancies = tuple(_sorted(comparison.found))
    passed = not discrepancies and all(result.complete for result in rules)

    if retrying and report_path.exists():
        attempt = record.get("attempt")
        fresh = _build_report(
            run_id, attempt, runs._utc_now(), comparison, rules, discrepancies, database,
            recorded_sha, archive,
        )
        report = _verified_provisional_report(evidence, report_path, record, fresh)
    else:
        if state is runs.ReconciliationState.UNFINALIZED:
            _conflict(evidence, report_path, [
                f"The manifest records an unfinalized {_REPORT_RELATIVE} that no longer exists."
            ])
        attempt = secrets.token_hex(8)
        report = _build_report(
            run_id, attempt, runs._utc_now(), comparison, rules, discrepancies, database,
            recorded_sha, archive,
        )
        before = evidence.reconciliation_started(attempt)
        report_path.parent.mkdir(exist_ok=True)
        try:
            runs.write_json_atomic(report_path, report)
        except BaseException:
            if not report_path.exists():
                evidence.reconciliation_abandoned(before)
            raise

    failed = [result.rule for result in rules if result.result == "FAIL"]
    incomplete = [result.rule for result in rules if not result.complete]
    summary = {
        "report_version": REPORT_VERSION,
        "result": "passed" if passed else "failed",
        "attempt": attempt,
        "report": _REPORT_RELATIVE,
        "report_sha256": _sha256(report_path),
        "reconciled_at": report["generated_at"],
        "database_sha256": recorded_sha,
        "rules_failed": failed,
        "rules_incomplete": incomplete,
        "discrepancies": len(discrepancies),
        "eligibility": comparison.eligibility_summary(),
        "release_review": "awaiting_approval" if passed else "blocked",
    }
    if retrying:
        summary["finalized_by_retry_at"] = runs._utc_now()
    failure = None if passed else {
        "stage": "reconciliation",
        "step": "reconcile",
        # A wrong disposition causes the differences it produces in RC-03 to RC-10 (spec 12.1).
        "rule": RC.RC_11 if RC.RC_11 in failed else (failed or incomplete)[0],
        "reason": (
            f"{len(discrepancies)} discrepancies in {', '.join(failed)}. "
            + (f"Not fully evaluated: {', '.join(incomplete)}. " if incomplete else "")
            + f"See {_REPORT_RELATIVE}."
        ),
    }
    try:
        evidence.reconciled(passed, summary, failure)
    except Exception as error:
        raise ReconciliationNotFinalizedError(run_id, report_path, error) from error
    return ReconciliationResult(
        run_id,
        RunStatus.RECONCILED if passed else RunStatus.FAILED,
        rules,
        discrepancies,
        report_path,
        report,
    )


def _conflict(evidence: runs._Evidence, report_path: Path, problems: list[str]) -> None:
    existing_sha = _sha256(report_path) if report_path.is_file() else None
    evidence.reconciliation_conflict(problems, existing_sha)
    raise ReconciliationConflictError(evidence.manifest["run_id"], problems)


def _verified_provisional_report(
    evidence: runs._Evidence,
    report_path: Path,
    record: Mapping[str, Any],
    fresh: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the existing report if it is exactly what this attempt would write, else conflict."""
    problems = []
    recorded_sha = record.get("report_sha256")
    if recorded_sha is not None and _sha256(report_path) != recorded_sha:
        problems.append(
            f"{_REPORT_RELATIVE} does not match the checksum recorded for attempt "
            f"{record.get('attempt')}."
        )
    try:
        existing = runs._read_json(report_path)
    except runs.EvidenceUnreadableError as error:
        _conflict(evidence, report_path, [*problems, str(error)])
    if existing.get("attempt") != record.get("attempt"):
        problems.append(
            f"{_REPORT_RELATIVE} belongs to attempt {existing.get('attempt')}, "
            f"not {record.get('attempt')}."
        )
    if existing.get("report_version") != REPORT_VERSION:
        problems.append(
            f"{_REPORT_RELATIVE} was written under report version "
            f"{existing.get('report_version')!r}; it cannot be finalized under report version "
            f"{REPORT_VERSION} and is never rewritten."
        )
    expected = json.loads(json.dumps(fresh, default=str))
    if _without_timestamp(existing) != _without_timestamp(expected):
        changed = sorted(
            key for key in set(existing) | set(expected)
            if key != "generated_at" and existing.get(key) != expected.get(key)
        )
        problems.append(
            f"{_REPORT_RELATIVE} differs from a fresh reconciliation of the same evidence "
            f"(fields: {', '.join(changed)})."
        )
    if problems:
        _conflict(evidence, report_path, problems)
    return existing


def _without_timestamp(report: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in report.items() if key != "generated_at"}


def _build_report(
    run_id: str,
    attempt: str,
    generated_at: str,
    comparison: "_Comparison",
    rules: tuple[RuleResult, ...],
    discrepancies: tuple[Discrepancy, ...],
    database: Path,
    recorded_sha: str,
    archive: Path,
) -> dict[str, Any]:
    passed = not discrepancies and all(result.complete for result in rules)
    return {
        "report_version": REPORT_VERSION,
        "run_id": run_id,
        "attempt": attempt,
        "generated_at": generated_at,
        "specification": runs.SPECIFICATION,
        "result": "PASS" if passed else "FAIL",
        "status": RunStatus.RECONCILED if passed else RunStatus.FAILED,
        "method": {
            "source": "Archived source files re-read and re-parsed independently.",
            "dispositions": (
                "Determined independently from the archived source (spec 12.3); the converter's "
                "planner is not run. The record reports and manifest counts are compared with "
                "that answer (RC-01, RC-10 to RC-12) and are never used as expected values."
            ),
            "expected_values": (
                "Recomputed from raw source text with independent code tables and "
                "section 7 transformations; the conversion plan's mapped values are not used."
            ),
            "target": "Queried read-only (mode=ro, query_only); amounts as exact Decimal.",
        },
        "database": {"path": str(database), "sha256": recorded_sha},
        "source": {
            "archive": runs.SOURCE_DIRECTORY,
            "sha256": {name: _sha256(archive / name) for name in runs.SOURCE_FILES},
        },
        "rules": [
            {
                "rule": result.rule,
                "title": result.title,
                "result": result.result,
                "checked": result.checked,
                "discrepancies": result.discrepancies,
                "complete": result.complete,
                **(
                    {"not_evaluated": dict(result.not_evaluated)}
                    if result.not_evaluated is not None else {}
                ),
                "note": result.note,
            }
            for result in rules
        ],
        **comparison.summary(),
        "eligibility": comparison.eligibility_report(),
        "discrepancies": [d.to_json() for d in discrepancies],
        "release": "Not released. Release approval is a separate human decision.",
    }


def verify_reconciliation(run_id: str, *, evidence_root: Path | None = None) -> ReconciliationCheck:
    """Re-verify a ``RECONCILED`` run's evidence: manifest, report, source, and database hashes.

    Read-only. A verified run is reconciled and intact; it is still not released.
    """
    directory = runs._run_directory(run_id, evidence_root)
    try:
        manifest = runs._read_json(directory / runs.MANIFEST_NAME)
    except runs.EvidenceUnreadableError as error:
        return ReconciliationCheck(run_id, (str(error),))
    problems = []
    if manifest.get("status") != RunStatus.RECONCILED:
        problems.append(f"Status is {manifest.get('status')}, not RECONCILED.")
    if manifest.get("release") is not None:
        problems.append("The manifest records a release decision; reconciliation never does.")
    record = manifest.get("reconciliation") or {}
    if record.get("state") != runs.ReconciliationState.FINAL or record.get("result") != "passed":
        problems.append(
            f"The manifest records no final passed reconciliation (state {record.get('state')}, "
            f"result {record.get('result')})."
        )
    report_path = directory / runs.REPORTS_DIRECTORY / RECONCILIATION_REPORT_NAME
    try:
        report = runs._read_json(report_path)
    except runs.EvidenceUnreadableError as error:
        problems.append(str(error))
        return ReconciliationCheck(run_id, tuple(problems))
    if _sha256(report_path) != record.get("report_sha256"):
        problems.append(f"{_REPORT_RELATIVE} does not match the checksum in the manifest.")
    if (report.get("run_id"), report.get("attempt")) != (run_id, record.get("attempt")):
        problems.append(f"{_REPORT_RELATIVE} is not the report of the finalized attempt.")
    if report.get("result") != "PASS":
        problems.append(f"{_REPORT_RELATIVE} does not record a PASS.")
    problems.extend(report_version_problems(report, record))

    recorded_sha = (manifest.get("database") or {}).get("sha256")
    if {(report.get("database") or {}).get("sha256"), record.get("database_sha256")} != {
        recorded_sha
    }:
        problems.append("The report, manifest, and load disagree about the database checksum.")
    database_path = manifest.get("database_path")
    if not database_path or not Path(database_path).is_file():
        problems.append("The conversion database is missing.")
    elif _sha256(Path(database_path)) != recorded_sha:
        problems.append("The conversion database no longer matches its recorded checksum.")

    archive = directory / runs.SOURCE_DIRECTORY
    files = (manifest.get("source") or {}).get("files") or {}
    reported = (report.get("source") or {}).get("sha256") or {}
    for name in runs.SOURCE_FILES:
        path = archive / name
        actual = _sha256(path) if path.is_file() else None
        if actual is None or actual != (files.get(name) or {}).get("archived_sha256") or (
            actual != reported.get(name)
        ):
            problems.append(f"The archived {name} does not match the recorded checksums.")
    return ReconciliationCheck(run_id, tuple(problems))


def report_version_problems(report: Mapping[str, Any], record: Mapping[str, Any]) -> list[str]:
    """Why a passed report does not meet the rules of its own ``report_version`` (spec 12.2)."""
    version = report.get("report_version")
    required = report_rules(report)
    if required is None:
        return [f"{_REPORT_RELATIVE} has an unknown report version {version!r}."]
    problems = []
    if version >= 2 and record.get("report_version") != version:
        problems.append("The manifest and the report disagree about the report version.")
    if version == 1 and record.get("report_version", 1) != 1:
        problems.append("The manifest and the report disagree about the report version.")
    results: dict[Any, Any] = {}
    items = report.get("rules")
    for item in items if isinstance(items, list) else []:
        rule = item.get("rule") if isinstance(item, Mapping) else None
        # A duplicated rule cannot be trusted either way.
        results[rule] = None if rule in results else item.get("result")
    if any(results.get(rule) != "PASS" for rule in required):
        problems.append(
            f"Not every rule {required[0]} to {required[-1]} is recorded as PASS "
            f"(report version {version})."
        )
    return problems


def _precondition_problems(
    archive: Path, manifest: Mapping[str, Any], database: Path
) -> list[str]:
    problems = []
    files = manifest["source"]["files"]
    for name in runs.SOURCE_FILES:
        entry = files.get(name) or {}
        path = archive / name
        if not path.is_file():
            problems.append(f"The archived {name} is missing.")
            continue
        actual = _sha256(path)
        if actual != entry.get("archived_sha256") or actual != entry.get("planned_sha256"):
            problems.append(
                f"The archived {name} no longer matches its recorded checksum "
                f"(SHA-256 {actual}, recorded {entry.get('archived_sha256')})."
            )
    for suffix in ("-journal", "-wal"):
        leftover = database.with_name(database.name + suffix)
        if leftover.exists():
            problems.append(f"{leftover.name} exists, so the database may be mid-transaction.")
    return problems


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- Independent source reader ---------------------------------------------------------------


@dataclass(frozen=True)
class SourceLine:
    """One physical data line of an archived file (the header is line 1)."""

    file: str
    line: int
    raw: str
    # None when the line is not well-formed CSV with the header's field count.
    values: Mapping[str, str] | None

    def get(self, name: str) -> str:
        return self.values[name] if self.values is not None else ""


def read_archive(archive: Path) -> dict[str, tuple[SourceLine, ...]]:
    """Read the four archived files as text, exactly as section 3 defines the dialect."""
    files = {}
    for name in (*contract.DATA_FILES, CONTROL):
        text = (archive / name).read_bytes().removeprefix(b"\xef\xbb\xbf").decode("utf-8")
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        lines = [line.removesuffix("\r") for line in lines]
        header = _parse(lines[0]) if lines else None
        if header is None or header != contract.HEADERS[name]:
            raise ValueError(f"{name} does not start with its documented header.")
        rows = []
        for number, line in enumerate(lines[1:], start=2):
            fields = _parse(line)
            values = (
                dict(zip(header, fields, strict=True))
                if fields is not None and len(fields) == len(header)
                else None
            )
            rows.append(SourceLine(name, number, line, values))
        files[name] = tuple(rows)
    return files


def _parse(line: str) -> tuple[str, ...] | None:
    try:
        return tuple(next(csv.reader([line], strict=True), []))
    except csv.Error:
        return None


# --- Independent transformations (spec section 7) --------------------------------------------


class Unconvertible(ValueError):
    """The source text cannot produce a target value under the documented rules."""


def _collapse(text: str) -> str:
    return " ".join(text.split())


def expected_legal_name(values: Mapping[str, str]) -> str:
    kind = values["CUST_TYPE"]
    if kind == "B":
        name = _collapse(values["BUSINESS_NAME"])
    elif kind == "I":
        first, last = _collapse(values["FIRST_NAME"]), _collapse(values["LAST_NAME"])
        initial = values["MIDDLE_INIT"].strip()
        if not first or not last:
            raise Unconvertible("An individual needs a first and a last name.")
        if initial and not _INITIAL.fullmatch(initial):
            raise Unconvertible(f"MIDDLE_INIT {initial!r} is not a single letter.")
        name = f"{first} {initial.upper()}. {last}" if initial else f"{first} {last}"
    else:
        raise Unconvertible(f"CUST_TYPE {kind!r} has no target borrower type.")
    if not name or len(name) > _MAX_NAME:
        raise Unconvertible(f"legal_name must be 1 to {_MAX_NAME} characters.")
    return name


def expected_borrower(values: Mapping[str, str]) -> dict[str, Any]:
    if not _CUST_NO.fullmatch(values["CUST_NO"]):
        raise Unconvertible(f"CUST_NO {values['CUST_NO']!r} is not 8 digits.")
    kind = _BORROWER_TYPES.get(values["CUST_TYPE"])
    if kind is None:
        raise Unconvertible(f"CUST_TYPE {values['CUST_TYPE']!r} has no target borrower type.")
    return {
        "source_system": _SOURCE_SYSTEM,
        "borrower_type": kind,
        "legal_name": expected_legal_name(values),
    }


def expected_amount(text: str) -> Decimal:
    """T-AMOUNT: the text is already an exact two-place decimal."""
    if not _AMOUNT.fullmatch(text):
        raise Unconvertible(f"REQ_AMT {text!r} is not in the documented format.")
    return Decimal(text)


def expected_rate(text: str) -> Decimal:
    """T-RATE: six digits in thousandths of a percent, so the decimal point is implied 3 places in.

    Built from the digits themselves: ``006500`` is ``6.500`` percent, ``Decimal("6.5000")``.
    """
    if not _RATE.fullmatch(text):
        raise Unconvertible(f"INT_RATE {text!r} is not six digits.")
    return Decimal(f"{int(text[:3])}.{text[3:]}0")


def expected_term(text: str) -> int:
    if not _TERM.fullmatch(text) or not 1 <= int(text) <= _MAX_TERM:
        raise Unconvertible(f"TERM_MOS {text!r} is not a term of 1 to {_MAX_TERM} months.")
    return int(text)


def expected_application(values: Mapping[str, str]) -> dict[str, Any]:
    if not _APPL_NO.fullmatch(values["APPL_NO"]):
        raise Unconvertible(f"APPL_NO {values['APPL_NO']!r} is not 10 digits.")
    product = _PRODUCTS.get(values["PROD_CD"])
    status = _STATUSES.get(values["APPL_STAT"])
    if product is None:
        raise Unconvertible(f"PROD_CD {values['PROD_CD']!r} has no target product.")
    if status is None:
        raise Unconvertible(f"APPL_STAT {values['APPL_STAT']!r} has no target status.")
    amount = expected_amount(values["REQ_AMT"])
    if amount <= 0:
        raise Unconvertible("requested_amount must be greater than zero.")
    return {
        "source_system": _SOURCE_SYSTEM,
        "loan_product": product,
        "status": status,
        "requested_amount": amount,
        "interest_rate": expected_rate(values["INT_RATE"]),
        "term_months": expected_term(values["TERM_MOS"]),
    }


def _exact_sum(values: Iterable[Decimal]) -> Decimal:
    with localcontext(_EXACT):
        return sum(values, _ZERO)


# --- Read-only target ------------------------------------------------------------------------


@dataclass(frozen=True)
class TargetBorrower:
    id: int
    source_system: Any
    source_system_id: Any
    legal_name: Any
    borrower_type: Any


@dataclass(frozen=True)
class TargetApplication:
    id: int
    source_system: Any
    source_system_id: Any
    loan_product: Any
    # Raw stored values: ExactDecimal keeps value * 10**scale as an SQLite INTEGER.
    requested_amount: Any
    interest_rate: Any
    term_months: Any
    status: Any


@dataclass(frozen=True)
class TargetParty:
    id: int
    application_id: Any
    borrower_id: Any
    role: Any


@dataclass(frozen=True)
class TargetSnapshot:
    borrowers: tuple[TargetBorrower, ...]
    applications: tuple[TargetApplication, ...]
    parties: tuple[TargetParty, ...]


def read_target(path: Path) -> TargetSnapshot:
    """Read every LOS row through a connection that cannot write."""
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    with closing(connection):
        connection.execute("PRAGMA query_only = ON")
        borrowers = tuple(
            TargetBorrower(*row)
            for row in connection.execute(
                "SELECT id, source_system, source_system_id, legal_name, borrower_type "
                "FROM borrower ORDER BY id"
            )
        )
        applications = tuple(
            TargetApplication(*row)
            for row in connection.execute(
                "SELECT id, source_system, source_system_id, loan_product, requested_amount, "
                "interest_rate, term_months, status FROM loan_application ORDER BY id"
            )
        )
        parties = tuple(
            TargetParty(*row)
            for row in connection.execute(
                "SELECT id, application_id, borrower_id, role FROM application_party ORDER BY id"
            )
        )
    return TargetSnapshot(borrowers, applications, parties)


def stored_decimal(raw: Any, places: int) -> Decimal | None:
    """An ExactDecimal value read back exactly, or None if it is not a scaled integer."""
    if type(raw) is not int:
        return None
    return Decimal(raw).scaleb(-places)


def _shown(value: Any) -> str | None:
    return None if value is None else str(value)


# --- The comparison --------------------------------------------------------------------------


_FILE_ORDER = {BORROWERS: 0, APPLICATIONS: 1, PARTIES: 2, CONTROL: 3}
# Rule codes a ROOT_CAUSE entry may name: the line-level rules of spec sections 9 and 10.
_LINE_RULES = frozenset(
    [f"SV-{n:02d}" for n in range(1, 12)] + [f"MP-{n:02d}" for n in range(1, 5)]
    + [f"RF-{n:02d}" for n in range(1, 9)] + [f"EX-{n:02d}" for n in range(1, 6)] + ["WN-01"]
)
_CAUSE_ENTRY = re.compile(r"([A-Z]{2}-[0-9]{2}) ([^ :;]+):([1-9][0-9]*)")
_DISPOSITION_CHECKS = {
    (Disposition.LOADED, Disposition.REJECTED): "wrongly_rejected",
    (Disposition.LOADED, Disposition.EXCLUDED): "wrongly_excluded",
    (Disposition.EXCLUDED, Disposition.LOADED): "wrongly_loaded",
    (Disposition.REJECTED, Disposition.LOADED): "wrongly_loaded",
    (Disposition.EXCLUDED, Disposition.REJECTED): "rejected_instead_of_excluded",
    (Disposition.REJECTED, Disposition.EXCLUDED): "excluded_instead_of_rejected",
}
# Manifest validation.rows keys and the independent counts they must equal.
_COUNT_KEYS = ("eligible", "excluded", "rejected", "excluded_dependent", "rejected_dependent")
# RC-12's record of the row-detail comparisons it did not make (spec 12.4).
_NOT_EVALUATED_KEYS = ("lines", "expected_rows", "recorded_rows")


def _sorted(found: Iterable[Discrepancy]) -> list[Discrepancy]:
    """Spec 12.5 order: rule, then file order, then line, then check (stable otherwise)."""
    return sorted(found, key=lambda d: (
        d.rule, _FILE_ORDER.get(d.file or "", 4), -1 if d.line is None else d.line, d.check,
    ))


def _cause_order(cause: Cause) -> tuple[int, int, str]:
    return _FILE_ORDER[cause.file], cause.line, cause.rule


def _causes_text(causes: Iterable[Cause]) -> str:
    return "; ".join(str(c) for c in sorted(set(causes), key=_cause_order))


def _row_text(rule: str, field: str, value: str) -> str:
    return f"{rule} {field}={value}"


class _Comparison:
    def __init__(
        self,
        source: Mapping[str, tuple[SourceLine, ...]],
        expected: EligibilityResult,
        recorded: RecordedReports,
        target: TargetSnapshot,
        manifest: Mapping[str, Any],
    ) -> None:
        self.source = source
        self.expected = expected
        self.recorded = recorded
        self.target = target
        self.manifest = manifest
        self.found: list[Discrepancy] = []
        self.checked: Counter[ReconciliationRule] = Counter()
        self.notes: dict[ReconciliationRule, str] = {}
        self.expected_lines: dict[tuple[str, int], ExpectedLine] = {
            (line.file, line.line): line for line in expected.lines
        }
        # Recorded report rows by the source line they name; rows naming none are kept apart.
        self.recorded_rows: dict[tuple[str, int], dict[str, list[RecordedRow]]] = defaultdict(
            lambda: defaultdict(list)
        )
        self.unplaced_rows: list[RecordedRow] = []
        for row in recorded.all_rows():
            if (row.file, row.line) in self.expected_lines:
                self.recorded_rows[(row.file, row.line)][row.kind].append(row)
            else:
                self.unplaced_rows.append(row)
        self.disposition_discrepancies: set[tuple[str, int]] = set()
        # RC-12 row-detail comparisons left unmade because RC-11 disputes the line's disposition.
        self.not_evaluated: Counter[str] = Counter({key: 0 for key in _NOT_EVALUATED_KEYS})

        self.borrowers = [b for b in target.borrowers if b.source_system == _SOURCE_SYSTEM]
        self.applications = [a for a in target.applications if a.source_system == _SOURCE_SYSTEM]
        self.borrower_by_id = {b.id: b for b in target.borrowers}
        self.application_by_id = {a.id: a for a in target.applications}
        self.borrowers_by_key: dict[Any, list[TargetBorrower]] = defaultdict(list)
        for borrower in self.borrowers:
            self.borrowers_by_key[borrower.source_system_id].append(borrower)
        self.applications_by_key: dict[Any, list[TargetApplication]] = defaultdict(list)
        for application in self.applications:
            self.applications_by_key[application.source_system_id].append(application)
        self.parties_by_application: dict[Any, list[TargetParty]] = defaultdict(list)
        self.parties_by_borrower: dict[Any, list[TargetParty]] = defaultdict(list)
        # LEGACY_LOS parties by the (APPL_NO, CUST_NO) source keys of their two ends.
        self.parties_by_pair: dict[tuple[Any, Any], list[TargetParty]] = defaultdict(list)
        for party in target.parties:
            self.parties_by_application[party.application_id].append(party)
            self.parties_by_borrower[party.borrower_id].append(party)
            application = self.application_by_id.get(party.application_id)
            borrower = self.borrower_by_id.get(party.borrower_id)
            if application is not None and borrower is not None:
                pair = (application.source_system_id, borrower.source_system_id)
                self.parties_by_pair[pair].append(party)

        self.expected_relationships: dict[str, list[tuple[str, str | None]]] = {}
        self.actual_relationships: dict[str, list[tuple[str, str]]] = {}

    # Helpers.

    def add(self, rule: ReconciliationRule, check: str, message: str, **detail: Any) -> None:
        self.found.append(Discrepancy(rule, check, message, **detail))

    def expected_line(self, line: SourceLine) -> ExpectedLine:
        return self.expected_lines[(line.file, line.line)]

    def disposition(self, line: SourceLine) -> Disposition:
        """The line's independent disposition (spec 12.3), never the converter's."""
        return self.expected_line(line).disposition

    def rows_of(self, line: SourceLine, kind: str) -> list[RecordedRow]:
        return self.recorded_rows.get((line.file, line.line), {}).get(kind, [])

    def recorded_disposition(self, line: SourceLine) -> Disposition | None:
        """The disposition the record reports give the line; None if they give two (spec 12.4)."""
        rejected, excluded = self.rows_of(line, EXCEPTIONS), self.rows_of(line, EXCLUSIONS)
        if rejected and excluded:
            return None
        if rejected:
            return Disposition.REJECTED
        return Disposition.EXCLUDED if excluded else Disposition.LOADED

    def loaded(self, file: str) -> list[SourceLine]:
        return [
            line for line in self.source[file]
            if self.disposition(line) is Disposition.LOADED and line.values is not None
        ]

    def lines_with(self, file: str, field: str, value: str) -> list[SourceLine]:
        return [line for line in self.source[file] if line.get(field) == value]

    def describe(self, line: SourceLine) -> str:
        expected = self.expected_line(line)
        state = f"{expected.disposition}" + (", dependent" if expected.dependent else "")
        rules = ", ".join(sorted(expected.rules))
        return f"{line.file}:{line.line} is {state}" + (f" ({rules})" if rules else "")

    def run(self) -> None:
        self.rc01_accounting()
        self.rc02_control()
        self.rc03_counts()
        self.rc04_keys()
        self.rc05_amounts()
        self.rc06_distributions()
        self.rc07_fields()
        self.rc08_relationships()
        self.rc09_primary()
        self.rc10_standalone()
        self.rc11_dispositions()
        self.rc12_report_rows()

    def rule_results(self) -> tuple[RuleResult, ...]:
        found = Counter(d.rule for d in self.found)
        return tuple(
            RuleResult(
                rule, self.checked[rule], found[rule], self.notes.get(rule),
                self.not_evaluated_counts() if rule is RC.RC_12 else None,
            )
            for rule in RC
        )

    def not_evaluated_counts(self) -> dict[str, int]:
        return {key: self.not_evaluated[key] for key in _NOT_EVALUATED_KEYS}

    # RC-01.

    def _exclusions(self, line: SourceLine) -> set[str]:
        """The exclusion rules whose criterion the row meets, judged from its raw text."""
        if line.values is None:
            return set()
        if line.file == BORROWERS:
            return {"EX-01"} if line.get("RECORD_STATUS") == "D" else set()
        if line.file == APPLICATIONS:
            found = set()
            if line.get("PROD_CD") in ("330", "900"):
                found.add("EX-02")
            if line.get("APPL_STAT") == "X":
                found.add("EX-03")
            return found
        found = set()
        if line.get("REL_CD") == "SGN":
            found.add("EX-04")
        appl_no = line.get("APPL_NO")
        heads = self.lines_with(APPLICATIONS, "APPL_NO", appl_no) if appl_no.strip() else []
        # Two copies of the application are SV-09 rejections, never exclusions (spec 12.3.2).
        if len(heads) == 1 and self._exclusions(heads[0]):
            found.add("EX-05")
        return found

    def recorded_rules(self, line: SourceLine) -> set[str]:
        return {row.rule for kind in (EXCEPTIONS, EXCLUSIONS) for row in self.rows_of(line, kind)}

    def rc01_accounting(self) -> None:
        validation = (self.manifest.get("validation") or {}).get("rows")
        for file in contract.DATA_FILES:
            lines = self.source[file]
            self.checked[RC.RC_01] += len(lines)
            counts = validation.get(file) if isinstance(validation, Mapping) else None
            if not isinstance(counts, Mapping):
                self.add(
                    RC.RC_01, "no_disposition",
                    f"The manifest records no dispositions for {file}.",
                    file=file, evidence=runs.MANIFEST_NAME, expected=str(len(lines)), actual=None,
                )
            else:
                if counts.get("read") != len(lines):
                    self.add(
                        RC.RC_01, "rows_not_accounted",
                        f"{file}: {len(lines)} rows were read, but the manifest records "
                        f"{counts.get('read')}.",
                        file=file, evidence=runs.MANIFEST_NAME, expected=str(len(lines)),
                        actual=_shown(counts.get("read")),
                    )
                parts = [counts.get(key) for key in ("eligible", "excluded", "rejected")]
                total = sum(parts) if all(type(p) is int for p in parts) else None
                if total != len(lines):
                    self.add(
                        RC.RC_01, "disposition_total",
                        f"{file}: the recorded loaded + excluded + rejected is not the number "
                        "of rows read.",
                        file=file, evidence=runs.MANIFEST_NAME, expected=str(len(lines)),
                        actual=_shown(total),
                    )
            for line in lines:
                key = self.expected_line(line).key
                for kind in (EXCEPTIONS, EXCLUSIONS, WARNINGS):
                    for row in self.rows_of(line, kind):
                        if row.values["SOURCE_KEY"] != key:
                            self.add(
                                RC.RC_01, "key_mismatch",
                                "The record report names this line by a different key.",
                                file=file, line=line.line, source_key=key,
                                evidence=row.evidence, field="SOURCE_KEY", expected=key,
                                actual=row.values["SOURCE_KEY"],
                            )
                recorded = self.recorded_disposition(line)
                if line.values is None or recorded is None:
                    continue  # SV-01 lines meet no criterion; RC-11 reports recorded_twice.
                rules = self.recorded_rules(line)
                criteria = self._exclusions(line)
                if recorded is Disposition.EXCLUDED and not criteria & rules:
                    self.add(
                        RC.RC_01, "exclusion_without_criterion",
                        "The row is excluded, but its source text meets none of the exclusion "
                        "criteria recorded for it.",
                        file=file, line=line.line, source_key=key,
                        expected=", ".join(sorted(criteria)) or "not excluded",
                        actual=", ".join(sorted(rules)),
                    )
                if recorded is Disposition.LOADED and criteria:
                    self.add(
                        RC.RC_01, "loaded_despite_exclusion",
                        "The row is recorded as loaded, but it meets an exclusion criterion.",
                        file=file, line=line.line, source_key=key,
                        expected=", ".join(sorted(criteria)), actual="loaded",
                    )
                if (
                    recorded is Disposition.REJECTED
                    and criteria
                    and not _REJECTED_BEFORE_EXCLUSION & rules
                ):
                    self.add(
                        RC.RC_01, "rejected_despite_exclusion",
                        "The row is rejected, but it meets an exclusion criterion, and only "
                        "SV-01, SV-09, or SV-10 are applied before exclusions.",
                        file=file, line=line.line, source_key=key,
                        expected=", ".join(sorted(criteria)), actual=", ".join(sorted(rules)),
                    )

        expected = self.manifest.get("expected_target") or {}
        for name, file in (
            ("borrowers", BORROWERS), ("applications", APPLICATIONS), ("parties", PARTIES)
        ):
            loaded = self.expected.counts(file)[Disposition.LOADED]
            self.checked[RC.RC_01] += 1
            if expected.get(name) != loaded:
                self.add(
                    RC.RC_01, "load_plan_count",
                    f"The load expected {expected.get(name)} {name}, but the independent "
                    f"determination from the archived source gives {loaded}.",
                    file=file, evidence=runs.MANIFEST_NAME, expected=str(loaded),
                    actual=_shown(expected.get(name)),
                )

    @staticmethod
    def _key(line: SourceLine) -> str:
        if line.file == BORROWERS:
            return line.get("CUST_NO")
        if line.file == APPLICATIONS:
            return line.get("APPL_NO")
        return f"{line.get('APPL_NO')}/{line.get('CUST_NO')}"

    # RC-02.

    def rc02_control(self) -> None:
        control: dict[str, SourceLine] = {}
        for line in self.source[CONTROL]:
            if line.values is None or line.get("FILE_NAME") not in contract.DATA_FILES:
                self.add(
                    RC.RC_02, "control_row_invalid", "The control row cannot be used.",
                    file=CONTROL, line=line.line, actual=line.raw,
                )
                continue
            if line.get("FILE_NAME") in control:
                self.add(
                    RC.RC_02, "control_row_duplicate", "The file is listed twice.",
                    file=CONTROL, line=line.line, source_key=line.get("FILE_NAME"),
                )
                continue
            control[line.get("FILE_NAME")] = line

        for file in contract.DATA_FILES:
            self.checked[RC.RC_02] += 1
            entry = control.get(file)
            read = len(self.source[file])
            if entry is None:
                self.add(RC.RC_02, "control_row_missing", f"{file} is not in the control file.",
                         file=CONTROL, source_key=file)
                continue
            count = entry.get("RECORD_COUNT")
            if not _COUNT.fullmatch(count) or int(count) != read:
                self.add(
                    RC.RC_02, "record_count",
                    f"{file} has {read} data rows; the control file says {count!r}.",
                    file=CONTROL, line=entry.line, source_key=file, field="RECORD_COUNT",
                    expected=count, actual=str(read),
                )

        dates = {line.get("EXTRACT_DATE") for line in control.values()}
        if len(dates) > 1:
            self.add(RC.RC_02, "extract_date", "EXTRACT_DATE differs between rows.",
                     file=CONTROL, actual=", ".join(sorted(dates)))

        self.checked[RC.RC_02] += 1
        amounts, unparseable = self._source_amounts(self.source[APPLICATIONS])
        self.source_total = _exact_sum(amounts.values())
        self.unparseable = unparseable
        head = control.get(APPLICATIONS)
        total_text = head.get("AMOUNT_TOTAL") if head else ""
        if not _AMOUNT.fullmatch(total_text):
            self.add(RC.RC_02, "control_amount_invalid",
                     f"AMOUNT_TOTAL {total_text!r} is not an amount.",
                     file=CONTROL, line=head.line if head else None, field="AMOUNT_TOTAL",
                     actual=total_text)
            self.control_total = None
            return
        self.control_total = Decimal(total_text)
        if unparseable:
            self.notes[RC.RC_02] = (
                f"Amount total unverifiable (RUN-05): {unparseable} REQ_AMT values cannot be parsed."
            )
        elif self.source_total != self.control_total:
            self.add(
                RC.RC_02, "control_amount",
                "The REQ_AMT total does not equal the control AMOUNT_TOTAL.",
                file=CONTROL, line=head.line, source_key=APPLICATIONS, field="AMOUNT_TOTAL",
                expected=str(self.control_total), actual=str(self.source_total),
            )

    @staticmethod
    def _source_amounts(lines: Iterable[SourceLine]) -> tuple[dict[int, Decimal], int]:
        amounts, unparseable = {}, 0
        for line in lines:
            text = line.get("REQ_AMT")
            if _AMOUNT.fullmatch(text):
                amounts[line.line] = Decimal(text)
            else:
                unparseable += 1
        return amounts, unparseable

    # RC-03.

    def rc03_counts(self) -> None:
        application_ids = {a.id for a in self.applications}
        actual_parties = sum(
            1 for p in self.target.parties if p.application_id in application_ids
        )
        for file, actual in (
            (BORROWERS, len(self.borrowers)),
            (APPLICATIONS, len(self.applications)),
            (PARTIES, actual_parties),
        ):
            expected = len(self.loaded(file))
            self.checked[RC.RC_03] += 1
            if expected != actual:
                self.add(
                    RC.RC_03, "row_count",
                    f"{_TABLES[file]} holds {actual} {_SOURCE_SYSTEM} rows; {expected} "
                    f"{file} rows were loaded.",
                    file=file, target_table=_TABLES[file],
                    expected=str(expected), actual=str(actual),
                )

    # RC-04.

    def rc04_keys(self) -> None:
        for file, key_field, rows, every in (
            (BORROWERS, "CUST_NO", self.borrowers, self.target.borrowers),
            (APPLICATIONS, "APPL_NO", self.applications, self.target.applications),
        ):
            table = _TABLES[file]
            expected: dict[str, SourceLine] = {}
            for line in self.loaded(file):
                key = line.get(key_field)
                self.checked[RC.RC_04] += 1
                if key in expected:
                    self.add(RC.RC_04, "duplicate_source_key",
                             "Two loaded source rows share this key.",
                             file=file, line=line.line, source_key=key)
                expected[key] = line
            actual = Counter(row.source_system_id for row in rows)
            for key, line in expected.items():
                if actual[key] == 0:
                    self.add(
                        RC.RC_04, "missing_key",
                        f"The loaded source row has no {table} row with this source_system_id.",
                        file=file, line=line.line, source_key=key, target_table=table,
                        field="source_system_id", expected=key, actual=None,
                    )
            seen: set[Any] = set()
            for row in rows:
                self.checked[RC.RC_04] += 1
                key = row.source_system_id
                if key not in expected:
                    origin = self.lines_with(file, key_field, key) if isinstance(key, str) else []
                    where = "; ".join(self.describe(line) for line in origin) or "not in the source"
                    self.add(
                        RC.RC_04, "unexpected_key",
                        f"The target row's key was not loaded from the source ({where}).",
                        file=file, line=origin[0].line if origin else None, source_key=_shown(key),
                        target_table=table, target_id=row.id, field="source_system_id",
                        expected=None, actual=_shown(key),
                    )
                elif key in seen:
                    self.add(
                        RC.RC_04, "duplicate_key",
                        f"More than one {table} row has this source_system_id.",
                        file=file, line=expected[key].line, source_key=key, target_table=table,
                        target_id=row.id, field="source_system_id", expected="1",
                        actual=str(actual[key]),
                    )
                seen.add(key)
            for row in every:
                if row.source_system != _SOURCE_SYSTEM:
                    self.add(
                        RC.RC_04, "foreign_row",
                        f"The {table} row is not from {_SOURCE_SYSTEM}; an isolated conversion "
                        "database holds nothing else.",
                        target_table=table, target_id=row.id, field="source_system",
                        expected=_SOURCE_SYSTEM, actual=_shown(row.source_system),
                    )

    # RC-05.

    def rc05_amounts(self) -> None:
        amounts, _ = self._source_amounts(self.source[APPLICATIONS])
        by_disposition: dict[Disposition, list[Decimal]] = defaultdict(list)
        for line in self.source[APPLICATIONS]:
            disposition = self.disposition(line)
            if line.line in amounts and disposition is not None:
                by_disposition[disposition].append(amounts[line.line])
        self.amounts = {d: _exact_sum(by_disposition[d]) for d in Disposition}

        target_amounts = []
        for application in self.applications:
            value = stored_decimal(application.requested_amount, 2)
            self.checked[RC.RC_05] += 1
            if value is None:
                self.add(
                    RC.RC_05, "stored_amount_invalid",
                    "requested_amount is not stored as an exact scaled integer.",
                    source_key=_shown(application.source_system_id),
                    target_table="loan_application", target_id=application.id,
                    field="requested_amount", actual=repr(application.requested_amount),
                )
            else:
                target_amounts.append(value)
        self.target_total = _exact_sum(target_amounts)
        loaded = self.amounts[Disposition.LOADED]

        self.checked[RC.RC_05] += 3
        if loaded != self.target_total:
            self.add(
                RC.RC_05, "loaded_amount_total",
                "The loaded REQ_AMT total does not equal the target requested_amount total.",
                file=APPLICATIONS, target_table="loan_application", field="requested_amount",
                expected=str(loaded), actual=str(self.target_total),
            )
        accounted = _exact_sum(self.amounts.values())
        if accounted != self.source_total:
            self.add(
                RC.RC_05, "disposition_amount_total",
                "Loaded + excluded + rejected amounts do not equal the source total.",
                file=APPLICATIONS, field="REQ_AMT",
                expected=str(self.source_total), actual=str(accounted),
            )
        planned = (self.manifest.get("expected_target") or {}).get("requested_amount")
        if planned is None or Decimal(planned) != loaded:
            self.add(
                RC.RC_05, "load_plan_amount",
                "The amount the load expected differs from the loaded REQ_AMT total.",
                file=APPLICATIONS, field="requested_amount", expected=str(loaded),
                actual=_shown(planned),
            )
        if self.unparseable:
            self.notes[RC.RC_05] = f"{self.unparseable} REQ_AMT values cannot be parsed."

    # RC-06.

    def _distributions(self) -> dict[str, tuple[Counter[str], Counter[str]]]:
        application_ids = {a.id for a in self.applications}

        def code(table: Mapping[str, str], value: str) -> str:
            return table.get(value, f"unmapped:{value}")

        applications, borrowers = self.loaded(APPLICATIONS), self.loaded(BORROWERS)
        return {
            "loan_product": (
                Counter(code(_PRODUCTS, row.get("PROD_CD")) for row in applications),
                Counter(_shown(a.loan_product) for a in self.applications),
            ),
            "status": (
                Counter(code(_STATUSES, row.get("APPL_STAT")) for row in applications),
                Counter(_shown(a.status) for a in self.applications),
            ),
            "borrower_type": (
                Counter(code(_BORROWER_TYPES, row.get("CUST_TYPE")) for row in borrowers),
                Counter(_shown(b.borrower_type) for b in self.borrowers),
            ),
            "role": (
                Counter(code(_ROLES, row.get("REL_CD")) for row in self.loaded(PARTIES)),
                Counter(
                    _shown(p.role) for p in self.target.parties
                    if p.application_id in application_ids
                ),
            ),
        }

    def rc06_distributions(self) -> None:
        tables = {
            "loan_product": "loan_application", "status": "loan_application",
            "borrower_type": "borrower", "role": "application_party",
        }
        for dimension, (expected, actual) in self._distributions().items():
            for category in sorted(set(expected) | set(actual), key=str):
                self.checked[RC.RC_06] += 1
                if expected[category] != actual[category]:
                    self.add(
                        RC.RC_06, "distribution",
                        f"Loaded rows with {dimension} {category}: {expected[category]} in the "
                        f"source, {actual[category]} in the target.",
                        source_key=_shown(category), target_table=tables[dimension],
                        field=dimension, expected=str(expected[category]),
                        actual=str(actual[category]),
                    )

    # RC-07.

    def rc07_fields(self) -> None:
        for line in self.loaded(BORROWERS):
            matches = self.borrowers_by_key.get(line.get("CUST_NO"), [])
            if len(matches) != 1:
                continue  # RC-04 reports missing and duplicate keys.
            target = matches[0]
            try:
                expected = expected_borrower(line.values or {})
            except Unconvertible as error:
                self._unconvertible(line, "borrower", target.id, error)
                continue
            for field, value in expected.items():
                self._compare(line, "borrower", target.id, field, value, getattr(target, field))

        for line in self.loaded(APPLICATIONS):
            matches = self.applications_by_key.get(line.get("APPL_NO"), [])
            if len(matches) != 1:
                continue
            target = matches[0]
            try:
                expected = expected_application(line.values or {})
            except Unconvertible as error:
                self._unconvertible(line, "loan_application", target.id, error)
                continue
            actual = {
                "source_system": target.source_system,
                "loan_product": target.loan_product,
                "status": target.status,
                "requested_amount": stored_decimal(target.requested_amount, 2),
                "interest_rate": stored_decimal(target.interest_rate, 4),
                "term_months": target.term_months if type(target.term_months) is int else None,
            }
            raw = {
                "requested_amount": target.requested_amount,
                "interest_rate": target.interest_rate,
                "term_months": target.term_months,
            }
            for field, value in expected.items():
                self._compare(
                    line, "loan_application", target.id, field, value, actual[field],
                    raw.get(field),
                )

    def _unconvertible(
        self, line: SourceLine, table: str, target_id: int, error: Unconvertible
    ) -> None:
        self.checked[RC.RC_07] += 1
        self.add(
            RC.RC_07, "source_not_convertible",
            f"The row was loaded, but its source text fails independent validation: {error}",
            file=line.file, line=line.line, source_key=self._key(line), target_table=table,
            target_id=target_id, actual=line.raw,
        )

    def _compare(
        self, line: SourceLine, table: str, target_id: int, field: str, expected: Any,
        actual: Any, raw: Any = None,
    ) -> None:
        self.checked[RC.RC_07] += 1
        if actual is not None and type(actual) is type(expected) and actual == expected:
            return
        shown = _shown(actual) if actual is not None else (repr(raw) if raw is not None else None)
        self.add(
            RC.RC_07, "field_mismatch",
            f"{table}.{field} does not equal the value recomputed from the source.",
            file=line.file, line=line.line, source_key=self._key(line), target_table=table,
            target_id=target_id, field=field, expected=str(expected), actual=shown,
        )

    # RC-08.

    def rc08_relationships(self) -> None:
        loaded_applications = {line.get("APPL_NO"): line for line in self.loaded(APPLICATIONS)}
        loaded_ids = set()
        for appl_no, head in loaded_applications.items():
            matches = self.applications_by_key.get(appl_no, [])
            expected: dict[str, tuple[str | None, SourceLine]] = {}
            for line in self.lines_with(PARTIES, "APPL_NO", appl_no):
                disposition = self.disposition(line)
                if disposition is Disposition.EXCLUDED or line.values is None:
                    continue
                self.checked[RC.RC_08] += 1
                if disposition is not Disposition.LOADED:
                    self.add(
                        RC.RC_08, "rejected_relationship_on_loaded_application",
                        "A required relationship of a loaded application was not loaded "
                        f"({self.describe(line)}).",
                        file=PARTIES, line=line.line, source_key=self._key(line),
                    )
                role = _ROLES.get(line.get("REL_CD"))
                if role is None:
                    self.add(
                        RC.RC_08, "source_role_unmapped",
                        f"REL_CD {line.get('REL_CD')!r} has no target role.",
                        file=PARTIES, line=line.line, source_key=self._key(line),
                        field="REL_CD", actual=line.get("REL_CD"),
                    )
                expected[line.get("CUST_NO")] = (role, line)
            self.expected_relationships[appl_no] = sorted(
                (cust, role) for cust, (role, _) in expected.items()
            )
            if len(matches) != 1:
                continue  # RC-04 reports the application itself.
            application = matches[0]
            loaded_ids.add(application.id)

            actual: dict[str, list[TargetParty]] = defaultdict(list)
            for party in self.parties_by_application.get(application.id, []):
                self.checked[RC.RC_08] += 1
                borrower = self.borrower_by_id.get(party.borrower_id)
                if borrower is None or borrower.source_system != _SOURCE_SYSTEM:
                    self.add(
                        RC.RC_08, "relationship_borrower_unknown",
                        "The relationship's borrower is not a converted customer.",
                        source_key=appl_no, target_table="application_party",
                        target_id=party.id, field="borrower_id", actual=_shown(party.borrower_id),
                    )
                    continue
                actual[borrower.source_system_id].append(party)
            self.actual_relationships[appl_no] = sorted(
                (cust, _shown(p.role) or "") for cust, ps in actual.items() for p in ps
            )

            for cust, (role, line) in expected.items():
                parties = actual.get(cust, [])
                key = f"{appl_no}/{cust}"
                if not parties:
                    self.add(
                        RC.RC_08, "missing_relationship",
                        f"Customer {cust} is {role} on application {appl_no} in the source, "
                        "but has no relationship to it in the target.",
                        file=PARTIES, line=line.line, source_key=key,
                        target_table="application_party", field="role",
                        expected=role, actual=None,
                    )
                    continue
                if len(parties) > 1:
                    for party in parties[1:]:
                        self.add(
                            RC.RC_08, "duplicate_relationship",
                            "The target relates this customer to the application more than once.",
                            file=PARTIES, line=line.line, source_key=key,
                            target_table="application_party", target_id=party.id,
                            expected="1", actual=str(len(parties)),
                        )
                if parties[0].role != role:
                    self.add(
                        RC.RC_08, "role_mismatch",
                        f"Customer {cust} has the wrong role on application {appl_no}.",
                        file=PARTIES, line=line.line, source_key=key,
                        target_table="application_party", target_id=parties[0].id, field="role",
                        expected=role, actual=_shown(parties[0].role),
                    )
            for cust, parties in actual.items():
                if cust in expected:
                    continue
                origin = [
                    line for line in self.lines_with(PARTIES, "APPL_NO", appl_no)
                    if line.get("CUST_NO") == cust
                ]
                where = "; ".join(self.describe(line) for line in origin) or "not in the source"
                for party in parties:
                    self.add(
                        RC.RC_08, "unexpected_relationship",
                        f"The target relates customer {cust} to application {appl_no} as "
                        f"{party.role}, but no loaded source row does ({where}).",
                        file=PARTIES, line=origin[0].line if origin else None,
                        source_key=f"{appl_no}/{cust}", target_table="application_party",
                        target_id=party.id, field="role", expected=None, actual=_shown(party.role),
                    )

        for party in self.target.parties:
            if party.application_id in loaded_ids:
                continue
            self.checked[RC.RC_08] += 1
            application = self.application_by_id.get(party.application_id)
            self.add(
                RC.RC_08, "relationship_on_unloaded_application",
                "The relationship belongs to an application that was not loaded from the source.",
                source_key=_shown(application.source_system_id) if application else None,
                target_table="application_party", target_id=party.id, field="application_id",
                actual=_shown(party.application_id),
            )
        for line in self.loaded(PARTIES):
            if line.get("APPL_NO") not in loaded_applications:
                self.add(
                    RC.RC_08, "relationship_without_application",
                    "A loaded relationship's application was not loaded.",
                    file=PARTIES, line=line.line, source_key=self._key(line),
                )

    # RC-09.

    def rc09_primary(self) -> None:
        for application in self.applications:
            self.checked[RC.RC_09] += 1
            primaries = [
                p for p in self.parties_by_application.get(application.id, [])
                if p.role == _PRIMARY
            ]
            if len(primaries) != 1:
                heads = self.lines_with(APPLICATIONS, "APPL_NO", application.source_system_id)
                self.add(
                    RC.RC_09, "primary_borrower_count",
                    f"Application {application.source_system_id} has {len(primaries)} primary "
                    "borrowers in the target; exactly one is required.",
                    file=APPLICATIONS if heads else None, line=heads[0].line if heads else None,
                    source_key=_shown(application.source_system_id),
                    target_table="loan_application", target_id=application.id,
                    field="primary_borrower", expected="1", actual=str(len(primaries)),
                )

    # RC-10.

    def rc10_standalone(self) -> None:
        # Expected: the independent WN-01 set (spec 12.3.5). Recorded: warnings.csv.
        warned = {line.key for line in self.expected.warnings}
        listed: dict[str, RecordedRow] = {}
        for row in self.recorded.rows[WARNINGS]:
            named = self.expected_lines.get((row.file, row.line)) if row.file == BORROWERS else None
            if row.rule == "WN-01" and named is not None:
                listed.setdefault(named.key, row)
        target = {
            b.source_system_id: b for b in self.borrowers if not self.parties_by_borrower.get(b.id)
        }
        self.standalone = sorted(warned)

        for cust in sorted(warned ^ set(listed)):
            self.checked[RC.RC_10] += 1
            self.add(
                RC.RC_10, "warning_list_mismatch",
                "The WN-01 list in warnings.csv does not match the independently determined "
                "customers with no loaded relationship.",
                file=BORROWERS, source_key=cust,
                evidence=listed[cust].evidence if cust in listed else (
                    recorded_evidence.report_path(WARNINGS)
                ),
                expected="listed" if cust in warned else "not listed",
                actual="listed" if cust in listed else "not listed",
            )
        borrowers_by_key = {line.get("CUST_NO"): line for line in self.loaded(BORROWERS)}
        for cust in sorted(warned | set(target), key=str):
            self.checked[RC.RC_10] += 1
            if cust in warned and cust not in target:
                matches = self.borrowers_by_key.get(cust, [])
                if not matches:
                    continue  # RC-04 reports the missing customer.
                self.add(
                    RC.RC_10, "standalone_customer_related",
                    f"Customer {cust} has no loaded relationship in the source, but has "
                    "relationships in the target.",
                    file=BORROWERS, line=borrowers_by_key[cust].line if cust in borrowers_by_key
                    else None, source_key=cust, target_table="borrower",
                    target_id=matches[0].id, expected="0 relationships",
                    actual=f"{len(self.parties_by_borrower.get(matches[0].id, []))} relationships",
                )
            elif cust not in warned and cust in target:
                line = borrowers_by_key.get(cust)
                self.add(
                    RC.RC_10, "unexpected_standalone_customer",
                    f"Customer {cust} has no relationship in the target, but is not on the "
                    "WN-01 list.",
                    file=BORROWERS if line else None, line=line.line if line else None,
                    source_key=_shown(cust), target_table="borrower", target_id=target[cust].id,
                    expected="listed relationships", actual="0 relationships",
                )

    # RC-11.

    def _target_records(self, line: SourceLine) -> list[Any]:
        """Target records carrying the line's key (spec 12.3.1); none for an SV-01 line."""
        if line.values is None:
            return []
        if line.file == BORROWERS:
            return list(self.borrowers_by_key.get(line.get("CUST_NO"), []))
        if line.file == APPLICATIONS:
            return list(self.applications_by_key.get(line.get("APPL_NO"), []))
        return list(self.parties_by_pair.get((line.get("APPL_NO"), line.get("CUST_NO")), []))

    def independent_counts(self, file: str) -> dict[str, int]:
        lines = self.expected.file_lines(file)
        counts = self.expected.counts(file)
        return {
            "eligible": counts[Disposition.LOADED],
            "excluded": counts[Disposition.EXCLUDED],
            "rejected": counts[Disposition.REJECTED],
            "excluded_dependent": sum(
                1 for line in lines if line.dependent and line.disposition is Disposition.EXCLUDED
            ),
            "rejected_dependent": sum(
                1 for line in lines if line.dependent and line.disposition is Disposition.REJECTED
            ),
        }

    def rc11_dispositions(self) -> None:
        for row in self.unplaced_rows:
            self.checked[RC.RC_11] += 1
            self.add(
                RC.RC_11, "report_row_without_source_line",
                f"A {row.kind}.csv row names {row.file}:{row.values['LINE_NO']}, which is not a "
                "data line of the archived source.",
                file=row.file, line=row.line, evidence=row.evidence, field="disposition",
                expected=None, actual=f"{row.file}:{row.values['LINE_NO']}",
            )

        for file in contract.DATA_FILES:
            for line in self.source[file]:
                expected = self.expected_line(line)
                where = f"{file}:{line.line}"
                detail = {
                    "file": file, "line": line.line, "source_key": expected.key,
                    "unit_key": expected.unit_key or None,
                }
                self.checked[RC.RC_11] += 2
                recorded = self.recorded_disposition(line)
                if recorded is None:
                    self.disposition_discrepancies.add((file, line.line))
                    self.add(
                        RC.RC_11, "recorded_twice",
                        f"{where} has rows in both exceptions.csv and exclusions.csv; it is "
                        f"{expected.disposition} by the independent determination.",
                        evidence=self.rows_of(line, EXCEPTIONS)[0].evidence, field="disposition",
                        expected=str(expected.disposition), actual="rejected and excluded",
                        **detail,
                    )
                elif recorded is not expected.disposition:
                    self.disposition_discrepancies.add((file, line.line))
                    self._disposition_mismatch(line, expected, recorded, detail)

                records = self._target_records(line)
                table = _TABLES[file]
                if expected.disposition is Disposition.LOADED and not records:
                    self.add(
                        RC.RC_11, "missing_from_target",
                        f"{where} loads by the independent determination, but {table} has no "
                        "record for it.",
                        target_table=table, field="target_record", expected="present",
                        actual="absent", **detail,
                    )
                elif expected.disposition is not Disposition.LOADED and records:
                    self.add(
                        RC.RC_11, "unexpected_in_target",
                        f"{where} is {expected.disposition} by the independent determination, "
                        f"but {table} holds a record with its key.",
                        target_table=table, target_id=records[0].id, field="target_record",
                        expected="absent", actual="present", **detail,
                    )

        validation = (self.manifest.get("validation") or {}).get("rows")
        for file in contract.DATA_FILES:
            independent = self.independent_counts(file)
            counts = validation.get(file) if isinstance(validation, Mapping) else None
            for key in _COUNT_KEYS:
                self.checked[RC.RC_11] += 1
                actual = counts.get(key) if isinstance(counts, Mapping) else None
                if actual != independent[key]:
                    label = key.replace("eligible", "loaded")
                    self.add(
                        RC.RC_11, "disposition_count",
                        f"{file}: the manifest records {actual} {label} rows; the independent "
                        f"determination gives {independent[key]}.",
                        file=file, evidence=runs.MANIFEST_NAME, field="disposition",
                        expected=f"{label}={independent[key]}", actual=f"{label}={actual}",
                    )

    def _disposition_mismatch(
        self,
        line: SourceLine,
        expected: ExpectedLine,
        recorded: Disposition,
        detail: Mapping[str, Any],
    ) -> None:
        def with_rules(state: str, rules: Iterable[str]) -> str:
            shown = ", ".join(sorted(rules))
            return f"{state} ({shown})" if shown else state

        if recorded is Disposition.LOADED:
            kind = EXCEPTIONS if expected.disposition is Disposition.REJECTED else EXCLUSIONS
            evidence = recorded_evidence.report_path(kind)
        else:
            kind = EXCEPTIONS if recorded is Disposition.REJECTED else EXCLUSIONS
            evidence = self.rows_of(line, kind)[0].evidence
        self.add(
            RC.RC_11, _DISPOSITION_CHECKS[(expected.disposition, recorded)],
            f"{line.file}:{line.line} is "
            f"{with_rules(str(expected.disposition), expected.rules)} by the independent "
            f"determination, but the record reports show it "
            f"{with_rules(str(recorded), self.recorded_rules(line))}.",
            evidence=evidence, field="disposition", expected=str(expected.disposition),
            actual=str(recorded), **detail,
        )

    # RC-12.

    def rc12_report_rows(self) -> None:
        for file in contract.DATA_FILES:
            for line in self.source[file]:
                expected = self.expected_line(line)
                if (file, line.line) in self.disposition_discrepancies:
                    # RC-11 already reports the wrong disposition; comparing rows written for
                    # it would only restate that difference.
                    self.not_evaluated["lines"] += 1
                    self.not_evaluated["expected_rows"] += len(expected.report_rows)
                    self.not_evaluated["recorded_rows"] += len(
                        self.rows_of(line, EXCEPTIONS) + self.rows_of(line, EXCLUSIONS)
                    )
                elif expected.disposition is not Disposition.LOADED:
                    kind = (
                        EXCEPTIONS if expected.disposition is Disposition.REJECTED else EXCLUSIONS
                    )
                    self._compare_rows(
                        line, expected, kind, expected.report_rows, expected.causes,
                        self.rows_of(line, kind),
                    )
                warnings = self.rows_of(line, WARNINGS)
                if expected.warning is not None or warnings:
                    wanted = (ReportRow("WN-01", "", ""),) if expected.warning is not None else ()
                    self._compare_rows(
                        line, expected, WARNINGS, wanted,
                        {"WN-01": expected.warning or frozenset()}, warnings,
                    )
        skipped = self.not_evaluated
        if skipped["lines"]:
            self.notes[RC.RC_12] = (
                f"Incomplete: exception and exclusion row details of {skipped['lines']} lines "
                f"were not compared ({skipped['expected_rows']} expected rows, "
                f"{skipped['recorded_rows']} recorded rows), because RC-11 found their "
                "dispositions wrong. Those details are unexamined, not verified; their "
                "warnings were still compared."
            )

    def _parse_causes(self, text: str) -> frozenset[Cause] | None:
        """A ROOT_CAUSE value parsed strictly (spec 12.4); None if it cannot be."""
        if text == "":
            return frozenset()
        causes = set()
        for entry in text.split("; "):
            match = _CAUSE_ENTRY.fullmatch(entry)
            if (
                match is None
                or match[1] not in _LINE_RULES
                or (match[2], int(match[3])) not in self.expected_lines
            ):
                return None
            causes.add(Cause(match[1], match[2], int(match[3])))
        return frozenset(causes)

    def _compare_rows(
        self,
        line: SourceLine,
        expected: ExpectedLine,
        kind: str,
        wanted: Iterable[ReportRow],
        causes: Mapping[str, frozenset[Cause]],
        rows: list[RecordedRow],
    ) -> None:
        """Match one line's recorded rows of one report to its expected rows (spec 12.4)."""
        warning = kind == WARNINGS

        def check(name: str) -> str:
            if not warning:
                return name
            return {
                "missing_rule": "missing_warning", "missing_row": "missing_warning",
                "unexpected_rule": "unexpected_warning", "unexpected_row": "unexpected_warning",
                "root_cause_mismatch": "warning_cause_mismatch",
            }.get(name, name)

        where = f"{line.file}:{line.line}"
        detail = {
            "file": line.file, "line": line.line, "source_key": expected.key,
            "unit_key": expected.unit_key or None,
        }
        wanted = list(wanted)
        open_rows = list(rows)
        matched: list[tuple[ReportRow, RecordedRow]] = []
        remaining: list[ReportRow] = []
        for want in wanted:
            hit = next((
                row for row in open_rows
                if (row.rule, row.values["FIELD"], row.values["SOURCE_VALUE"])
                == (want.rule, want.field, want.source_value)
            ), None)
            if hit is None:
                remaining.append(want)
            else:
                open_rows.remove(hit)
                matched.append((want, hit))
        for want in list(remaining):
            same_wanted = [w for w in remaining if (w.rule, w.field) == (want.rule, want.field)]
            same_recorded = [
                row for row in open_rows if (row.rule, row.values["FIELD"]) == (want.rule, want.field)
            ]
            if len(same_wanted) == 1 and len(same_recorded) == 1:
                [got] = same_recorded
                remaining.remove(want)
                open_rows.remove(got)
                matched.append((want, got))
                self.add(
                    RC.RC_12, "source_value_mismatch",
                    f"{where}: the {want.rule} row for {want.field or 'the line'} records a "
                    "different SOURCE_VALUE.",
                    evidence=got.evidence, field="SOURCE_VALUE", expected=want.source_value,
                    actual=got.values["SOURCE_VALUE"], **detail,
                )
        for want in remaining:
            text = _row_text(want.rule, want.field, want.source_value)
            name = "missing_row" if any(row.rule == want.rule for row in rows) else "missing_rule"
            self.add(
                RC.RC_12, check(name), f"{where}: {kind}.csv has no row {text}.",
                evidence=recorded_evidence.report_path(kind), field="RULE_CODE", expected=text,
                actual="none", **detail,
            )
        expected_rules = {w.rule for w in wanted}
        for got in open_rows:
            text = _row_text(got.rule, got.values["FIELD"], got.values["SOURCE_VALUE"])
            name = "unexpected_row" if got.rule in expected_rules else "unexpected_rule"
            self.add(
                RC.RC_12, check(name), f"{where}: {kind}.csv records {text}, which is not expected.",
                evidence=got.evidence, field="RULE_CODE", expected="none", actual=text, **detail,
            )

        dependent = "Y" if expected.dependent and not warning else "N"
        unit_key = "" if warning else expected.unit_key
        for got in rows:
            self.checked[RC.RC_12] += 1
            values = got.values
            for column, check_name, want_value in (
                ("DEPENDENT", "dependent_mismatch", dependent),
                ("UNIT_KEY", "unit_key_mismatch", unit_key),
                ("STAGE", "stage_mismatch",
                 STAGES[got.rule[:2]] if got.rule in _LINE_RULES else values["STAGE"]),
                ("SOURCE_LINE", "source_line_mismatch", line.raw),
            ):
                if values[column] != want_value:
                    self.add(
                        RC.RC_12, check_name,
                        f"{where}: the {got.rule} row records a different {column}.",
                        evidence=got.evidence, field=column, expected=want_value,
                        actual=values[column], **detail,
                    )
            if self._parse_causes(values["ROOT_CAUSE"]) is None:
                self.add(
                    RC.RC_12, "root_cause_unreadable",
                    f"{where}: the {got.rule} row's ROOT_CAUSE cannot be parsed.",
                    evidence=got.evidence, field="ROOT_CAUSE", expected=None,
                    actual=values["ROOT_CAUSE"], **detail,
                )
        for want, got in matched:
            parsed = self._parse_causes(got.values["ROOT_CAUSE"])
            wanted_causes = causes.get(want.rule, frozenset())
            if parsed is not None and parsed != wanted_causes:
                self.add(
                    RC.RC_12, check("root_cause_mismatch"),
                    f"{where}: the {want.rule} row names different immediate causes.",
                    evidence=got.evidence, field="ROOT_CAUSE",
                    expected=_causes_text(wanted_causes), actual=got.values["ROOT_CAUSE"],
                    **detail,
                )

    # Report.

    def eligibility_report(self) -> dict[str, Any]:
        """The independently verified eligibility section of the report (version 2)."""
        files = {}
        for file in contract.DATA_FILES:
            recorded: Counter[str] = Counter()
            for line in self.source[file]:
                disposition = self.recorded_disposition(line)
                recorded[str(disposition or "recorded_twice")] += 1
                rows = [*self.rows_of(line, EXCEPTIONS), *self.rows_of(line, EXCLUSIONS)]
                if disposition is not None and any(r.values["DEPENDENT"] == "Y" for r in rows):
                    recorded[f"{disposition}_dependent"] += 1
            files[file] = {
                "read": len(self.source[file]),
                "independent": {
                    key.replace("eligible", "loaded"): value
                    for key, value in self.independent_counts(file).items()
                },
                "recorded": {
                    name: recorded[name]
                    for name in (
                        "loaded", "excluded", "rejected", "excluded_dependent",
                        "rejected_dependent", "recorded_twice",
                    )
                },
            }
        return {
            "determination": (
                "Independent, from the archived source alone (spec 12.3); the converter's "
                "decisions and record reports are compared with it, never used as expected values."
            ),
            "files": files,
            "lines_compared": sum(len(self.source[file]) for file in contract.DATA_FILES),
            "disposition_discrepancies": len(self.disposition_discrepancies),
            "row_details": "complete" if not self.not_evaluated["lines"] else "incomplete",
            "not_evaluated_by_rc12": self.not_evaluated_counts(),
            "warnings": sorted(line.key for line in self.expected.warnings),
        }

    def eligibility_summary(self) -> dict[str, Any]:
        """The manifest's compact copy of :meth:`eligibility_report`.

        ``independently_verified`` is true only when RC-11 and RC-12 both passed with every
        comparison made: the converter's eligibility then agrees with the independent one.
        """
        found = Counter(d.rule for d in self.found)
        complete = not self.not_evaluated["lines"]
        return {
            "expected_from": "independent determination (spec 12.3)",
            "independently_verified": not found[RC.RC_11] and not found[RC.RC_12] and complete,
            "row_details": "complete" if complete else "incomplete",
            "not_evaluated_by_rc12": self.not_evaluated_counts(),
            "lines_compared": sum(len(self.source[file]) for file in contract.DATA_FILES),
            "loaded": {
                file: self.expected.counts(file)[Disposition.LOADED]
                for file in contract.DATA_FILES
            },
            "disposition_discrepancies": len(self.disposition_discrepancies),
            "rc11_discrepancies": found[RC.RC_11],
            "rc12_discrepancies": found[RC.RC_12],
        }

    def summary(self) -> dict[str, Any]:
        rows = {}
        for file in contract.DATA_FILES:
            counts = self.expected.counts(file)
            rows[file] = {
                "read": len(self.source[file]),
                "loaded": counts[Disposition.LOADED],
                "excluded": counts[Disposition.EXCLUDED],
                "rejected": counts[Disposition.REJECTED],
            }
        application_ids = {a.id for a in self.applications}
        customers = []
        for cust in self.standalone:
            relationships = []
            for line in self.lines_with(PARTIES, "CUST_NO", cust):
                expected = self.expected_line(line)
                relationships.append({
                    "line": line.line,
                    "appl_no": line.get("APPL_NO"),
                    "rel_cd": line.get("REL_CD"),
                    "outcome": f"{expected.disposition}"
                    + ("_dependent" if expected.dependent else ""),
                    "rules": sorted(expected.rules),
                })
            customers.append({"cust_no": cust, "relationships": relationships})
        return {
            "totals": {
                "rows": rows,
                "requested_amount": {
                    "source_total": str(self.source_total),
                    "control_total": _shown(self.control_total),
                    "loaded": str(self.amounts[Disposition.LOADED]),
                    "excluded": str(self.amounts[Disposition.EXCLUDED]),
                    "rejected": str(self.amounts[Disposition.REJECTED]),
                    "unparseable": self.unparseable,
                    "target_total": str(self.target_total),
                },
                "target": {
                    "borrowers": len(self.borrowers),
                    "applications": len(self.applications),
                    "parties": sum(
                        1 for p in self.target.parties if p.application_id in application_ids
                    ),
                },
            },
            "distributions": {
                dimension: {"expected": dict(sorted(expected.items())),
                            "actual": dict(sorted(actual.items(), key=lambda kv: str(kv[0])))}
                for dimension, (expected, actual) in self._distributions().items()
            },
            "relationships": {
                appl_no: {
                    "expected": [list(pair) for pair in self.expected_relationships[appl_no]],
                    "actual": [list(pair) for pair in self.actual_relationships.get(appl_no, [])],
                }
                for appl_no in sorted(self.expected_relationships)
            },
            "customers_without_applications": customers,
        }
