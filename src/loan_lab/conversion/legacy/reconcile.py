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
* Which rows *should* have loaded (each row's disposition) comes from re-validating the archived
  files with the Phase 1 rules. That accounting is cross-checked: excluded rows must meet an
  exclusion criterion, loaded and (most) rejected rows must not, and every loaded row must pass
  this module's own validation and transformations. A valid row that the planner wrongly rejects
  is *not* detected here; tests/test_conversion_spec_acceptance.py guards the sample's
  dispositions against spec section 15 instead.

A run passes only if every rule RC-01 to RC-10 matches. The outcome is written atomically to
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
from loan_lab.conversion.legacy import run as runs
from loan_lab.conversion.legacy.plan import ConversionPlan, Disposition, RowResult
from loan_lab.conversion.legacy.planner import plan_conversion
from loan_lab.conversion.legacy.run import RunStatus, check_ready
from loan_lab.conversion.legacy.source import SourceValidationError

REPORT_VERSION = 1
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
}


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

    @property
    def passed(self) -> bool:
        return self.discrepancies == 0

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
        return tuple(result.rule for result in self.rules if not result.passed)


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
    archived source file no longer matches its recorded checksum, or the database differs from
    the checksum recorded when it was loaded. Otherwise writes ``reports/reconciliation.json``
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

    if not retrying and report_path.exists():
        _conflict(evidence, report_path, [
            f"{_REPORT_RELATIVE} exists but the manifest records no reconciliation attempt."
        ])

    try:
        source = read_archive(archive)
        basis = plan_conversion(archive)
    except (OSError, ValueError, SourceValidationError) as error:
        raise ReconciliationRefusedError(
            run_id, [f"The archived source cannot be read: {type(error).__name__}: {error}"]
        ) from error
    target = read_target(database)
    if runs._sha256(database) != recorded_sha:
        raise ReconciliationRefusedError(
            run_id, ["The conversion database changed while it was being reconciled."]
        )

    comparison = _Comparison(source, basis, target, manifest)
    comparison.run()
    rules = comparison.rule_results()
    discrepancies = tuple(comparison.found)
    passed = not discrepancies

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

    failed = [result.rule for result in rules if not result.passed]
    summary = {
        "result": "passed" if passed else "failed",
        "attempt": attempt,
        "report": _REPORT_RELATIVE,
        "report_sha256": _sha256(report_path),
        "reconciled_at": report["generated_at"],
        "database_sha256": recorded_sha,
        "rules_failed": failed,
        "discrepancies": len(discrepancies),
        "release_review": "awaiting_approval" if passed else "blocked",
    }
    if retrying:
        summary["finalized_by_retry_at"] = runs._utc_now()
    failure = None if passed else {
        "stage": "reconciliation",
        "step": "reconcile",
        "rule": failed[0],
        "reason": (
            f"{len(discrepancies)} discrepancies in {', '.join(failed)}. "
            f"See {_REPORT_RELATIVE}."
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
    passed = not discrepancies
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
                "Re-validated from the archived source with the Phase 1 rules, then "
                "cross-checked against the exclusion criteria and independent validation."
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
                "result": "PASS" if result.passed else "FAIL",
                "checked": result.checked,
                "discrepancies": result.discrepancies,
                "note": result.note,
            }
            for result in rules
        ],
        **comparison.summary(),
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


class _Comparison:
    def __init__(
        self,
        source: Mapping[str, tuple[SourceLine, ...]],
        basis: ConversionPlan,
        target: TargetSnapshot,
        manifest: Mapping[str, Any],
    ) -> None:
        self.source = source
        self.basis = basis
        self.target = target
        self.manifest = manifest
        self.found: list[Discrepancy] = []
        self.checked: Counter[ReconciliationRule] = Counter()
        self.notes: dict[ReconciliationRule, str] = {}
        self.dispositions: dict[str, dict[int, RowResult]] = {
            file: {row.ref.line: row for row in basis.rows(file)} for file in contract.DATA_FILES
        }

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
        for party in target.parties:
            self.parties_by_application[party.application_id].append(party)
            self.parties_by_borrower[party.borrower_id].append(party)

        self.expected_relationships: dict[str, list[tuple[str, str | None]]] = {}
        self.actual_relationships: dict[str, list[tuple[str, str]]] = {}

    # Helpers.

    def add(self, rule: ReconciliationRule, check: str, message: str, **detail: Any) -> None:
        self.found.append(Discrepancy(rule, check, message, **detail))

    def disposition(self, line: SourceLine) -> Disposition | None:
        row = self.dispositions[line.file].get(line.line)
        return None if row is None else row.disposition

    def loaded(self, file: str) -> list[SourceLine]:
        return [
            line for line in self.source[file]
            if self.disposition(line) is Disposition.ELIGIBLE and line.values is not None
        ]

    def lines_with(self, file: str, field: str, value: str) -> list[SourceLine]:
        return [line for line in self.source[file] if line.get(field) == value]

    def describe(self, line: SourceLine) -> str:
        row = self.dispositions[line.file].get(line.line)
        if row is None:
            return f"{line.file}:{line.line} has no disposition"
        rules = ", ".join(row.rules)
        return f"{line.file}:{line.line} is {row.outcome}" + (f" ({rules})" if rules else "")

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

    def rule_results(self) -> tuple[RuleResult, ...]:
        found = Counter(d.rule for d in self.found)
        return tuple(
            RuleResult(rule, self.checked[rule], found[rule], self.notes.get(rule)) for rule in RC
        )

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
        heads = self.lines_with(APPLICATIONS, "APPL_NO", line.get("APPL_NO"))
        if heads and all(self._exclusions(head) for head in heads):
            found.add("EX-05")
        return found

    def rc01_accounting(self) -> None:
        for file in contract.DATA_FILES:
            lines = self.source[file]
            rows = self.basis.rows(file)
            self.checked[RC.RC_01] += len(lines)
            if [r.ref.line for r in rows] != [line.line for line in lines]:
                self.add(
                    RC.RC_01, "rows_not_accounted",
                    f"{file}: {len(lines)} rows were read but {len(rows)} were dispositioned.",
                    file=file, expected=str(len(lines)), actual=str(len(rows)),
                )
            counts = Counter(row.disposition for row in rows)
            if sum(counts.values()) != len(lines):
                self.add(
                    RC.RC_01, "disposition_total",
                    f"{file}: loaded + excluded + rejected is not the number of rows read.",
                    file=file, expected=str(len(lines)), actual=str(sum(counts.values())),
                )
            for line in lines:
                row = self.dispositions[file].get(line.line)
                if row is None:
                    self.add(
                        RC.RC_01, "no_disposition", "The source row has no disposition.",
                        file=file, line=line.line,
                    )
                    continue
                if line.values is None:
                    continue
                key = self._key(line)
                if row.key != key:
                    self.add(
                        RC.RC_01, "key_mismatch",
                        "The accounting basis names this row by a different key.",
                        file=file, line=line.line, source_key=key, expected=key, actual=row.key,
                    )
                criteria = self._exclusions(line)
                if row.disposition is Disposition.EXCLUDED and not criteria & set(row.rules):
                    self.add(
                        RC.RC_01, "exclusion_without_criterion",
                        "The row is excluded, but its source text meets none of the exclusion "
                        "criteria recorded for it.",
                        file=file, line=line.line, source_key=key,
                        expected=", ".join(sorted(criteria)) or "not excluded",
                        actual=", ".join(row.rules),
                    )
                if row.disposition is Disposition.ELIGIBLE and criteria:
                    self.add(
                        RC.RC_01, "loaded_despite_exclusion",
                        "The row is marked to load, but it meets an exclusion criterion.",
                        file=file, line=line.line, source_key=key,
                        expected=", ".join(sorted(criteria)), actual="loaded",
                    )
                if (
                    row.disposition is Disposition.REJECTED
                    and criteria
                    and not _REJECTED_BEFORE_EXCLUSION & set(row.rules)
                ):
                    self.add(
                        RC.RC_01, "rejected_despite_exclusion",
                        "The row is rejected, but it meets an exclusion criterion, and only "
                        "SV-01, SV-09, or SV-10 are applied before exclusions.",
                        file=file, line=line.line, source_key=key,
                        expected=", ".join(sorted(criteria)), actual=", ".join(row.rules),
                    )

        expected = self.manifest.get("expected_target") or {}
        for name, file in (
            ("borrowers", BORROWERS), ("applications", APPLICATIONS), ("parties", PARTIES)
        ):
            loaded = sum(1 for row in self.basis.rows(file) if row.disposition is Disposition.ELIGIBLE)
            self.checked[RC.RC_01] += 1
            if expected.get(name) != loaded:
                self.add(
                    RC.RC_01, "load_plan_count",
                    f"The load expected {expected.get(name)} {name}, but re-validating the "
                    f"archived source gives {loaded}.",
                    file=file, expected=str(loaded), actual=_shown(expected.get(name)),
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
        loaded = self.amounts[Disposition.ELIGIBLE]

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
                if disposition is not Disposition.ELIGIBLE:
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
        warned = {row.key for row in self.basis.customers_without_applications}
        related = {line.get("CUST_NO") for line in self.loaded(PARTIES)}
        independent = {line.get("CUST_NO") for line in self.loaded(BORROWERS)} - related
        target = {
            b.source_system_id: b for b in self.borrowers if not self.parties_by_borrower.get(b.id)
        }
        self.standalone = sorted(warned)

        for cust in sorted(warned ^ independent):
            self.checked[RC.RC_10] += 1
            self.add(
                RC.RC_10, "warning_list_mismatch",
                "The WN-01 list does not match the loaded customers with no loaded relationship.",
                file=BORROWERS, source_key=cust,
                expected="listed" if cust in independent else "not listed",
                actual="listed" if cust in warned else "not listed",
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

    # Report.

    def summary(self) -> dict[str, Any]:
        rows = {}
        for file in contract.DATA_FILES:
            counts = Counter(row.disposition for row in self.basis.rows(file))
            rows[file] = {
                "read": len(self.source[file]),
                "loaded": counts[Disposition.ELIGIBLE],
                "excluded": counts[Disposition.EXCLUDED],
                "rejected": counts[Disposition.REJECTED],
            }
        application_ids = {a.id for a in self.applications}
        customers = []
        for cust in self.standalone:
            relationships = []
            for line in self.lines_with(PARTIES, "CUST_NO", cust):
                row = self.dispositions[PARTIES][line.line]
                relationships.append({
                    "line": line.line,
                    "appl_no": line.get("APPL_NO"),
                    "rel_cd": line.get("REL_CD"),
                    "outcome": row.outcome,
                    "rules": list(row.rules),
                })
            customers.append({"cust_no": cust, "relationships": relationships})
        return {
            "totals": {
                "rows": rows,
                "requested_amount": {
                    "source_total": str(self.source_total),
                    "control_total": _shown(self.control_total),
                    "loaded": str(self.amounts[Disposition.ELIGIBLE]),
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
