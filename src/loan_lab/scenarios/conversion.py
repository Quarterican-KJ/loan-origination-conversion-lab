"""Repeatable synthetic conversion failure demonstrations (SYNTHETIC, DEMO ONLY).

Each scenario drives the real conversion pipeline (planner, transactional loader, run evidence,
and independent reconciliation) under a new run ID carrying a scenario prefix:

* ``control-totals`` (``SYN-CTRL-``): a copy of the sample extract whose control file misstates
  the applications record count and REQ_AMT total. Expected: FAILED at validation (RUN-04,
  RUN-05), with the source archived and no target database.
* ``business-rejections`` (``SYN-REJ-``): the sample extract as is. Its invalid amounts and
  missing party relationships are rejected and its out-of-scope rows excluded by the documented
  rules; the eligible records load and reconcile. Expected: RECONCILED.
* ``loader-defect`` (``SYN-DEFECT-``): the sample extract, with one eligible application's
  interest rate altered in the load input after planning. Counts and amounts are unchanged.
  Expected: FAILED at reconciliation, with an RC-07 field discrepancy.

The conversion rules, loader, and evidence code are used unchanged. The only defect is injected
here, into the plan handed to the loader, so the database checksum and load report record what
was actually written. Each scenario also writes ``<scenario root>/<run_id>/scenario.json``
(outside the run's evidence) labeling the run as synthetic and describing what was altered.
"""

import csv
import hashlib
import io
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from loan_lab.conversion.legacy import contract
from loan_lab.conversion.legacy.contract import Rule
from loan_lab.conversion.legacy.loader import RUN_ID_PATTERN, new_run_id
from loan_lab.conversion.legacy.plan import ConversionPlan, Disposition, MappedApplication
from loan_lab.conversion.legacy.planner import plan_conversion
from loan_lab.conversion.legacy.reconcile import RECONCILIATION_REPORT_NAME, reconcile_run
from loan_lab.conversion.legacy.run import (
    LOAD_REPORT_NAME,
    MANIFEST_NAME,
    REPORTS_DIRECTORY,
    SOURCE_DIRECTORY,
    SOURCE_FILES,
    RunFailedError,
    SourceRunFailedError,
    run_conversion,
    run_load,
)
from loan_lab.paths import default_conversion_root, default_evidence_root, find_project_root

SCENARIO_ROOT_RELATIVE = Path("data") / "scenarios"
SAMPLE_RELATIVE = Path("sample_data") / "legacy"
SCENARIO_FILE = "scenario.json"
EXTRACT_DIRECTORY = "extract"
LABEL = "SYNTHETIC DEMO SCENARIO: not a production conversion"


class Scenario(StrEnum):
    CONTROL_TOTALS = "control-totals"
    BUSINESS_REJECTIONS = "business-rejections"
    LOADER_DEFECT = "loader-defect"


RUN_ID_PREFIXES = {
    Scenario.CONTROL_TOTALS: "SYN-CTRL-",
    Scenario.BUSINESS_REJECTIONS: "SYN-REJ-",
    Scenario.LOADER_DEFECT: "SYN-DEFECT-",
}
TITLES = {
    Scenario.CONTROL_TOTALS: "Source validation failure: incorrect extract control totals",
    Scenario.BUSINESS_REJECTIONS: "Business record rejections and exclusions",
    Scenario.LOADER_DEFECT: "Loader transformation defect: wrong interest rate",
}
EXPECTED = {
    Scenario.CONTROL_TOTALS:
        "FAILED at validation (RUN-04, RUN-05); source archived; no target database",
    Scenario.BUSINESS_REJECTIONS:
        "Documented rejections and exclusions; eligible records load and RECONCILE",
    Scenario.LOADER_DEFECT:
        "Counts and amounts unchanged; FAILED at reconciliation with an RC-07 discrepancy",
}

# Control file misstatements for control-totals, applied to the applications row.
CONTROL_COUNT_ERROR = 1
CONTROL_AMOUNT_ERROR = Decimal("9000.00")
# Added to the planned rate of the first eligible application for loader-defect.
RATE_DEFECT = Decimal("0.1000")


class ScenarioRefusedError(Exception):
    """Nothing was run: the run ID is invalid or already used."""


@dataclass(frozen=True)
class InjectedRateDefect:
    application: str
    line: int
    planned_rate: Decimal
    injected_rate: Decimal

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": "interest_rate",
            "application": self.application,
            "source_file": contract.APPLICATIONS_FILE,
            "source_line": self.line,
            "planned_rate": str(self.planned_rate),
            "injected_rate": str(self.injected_rate),
            "where": "load input (mapped application handed to the transactional loader)",
        }


@dataclass(frozen=True)
class ScenarioResult:
    scenario: Scenario
    run_id: str
    evidence_directory: Path
    scenario_directory: Path
    # As recorded in the run's manifest; None if it could not be read.
    status: str | None
    failure_stage: str | None
    # What the run's evidence shows.
    findings: tuple[str, ...]
    # Every difference from the scenario's expected outcome.
    deviations: tuple[str, ...]
    defect: InjectedRateDefect | None = None

    @property
    def as_expected(self) -> bool:
        return not self.deviations


def default_scenario_root() -> Path:
    """Parent of the per-run scenario descriptors, data/scenarios/<run_id>/ (git-ignored)."""
    return find_project_root(Path(__file__)) / SCENARIO_ROOT_RELATIVE


def default_sample_directory() -> Path:
    return find_project_root(Path(__file__)) / SAMPLE_RELATIVE


def new_scenario_run_id(scenario: Scenario) -> str:
    return f"{RUN_ID_PREFIXES[scenario]}{new_run_id()}"


def run_scenario(
    scenario: Scenario,
    run_id: str | None = None,
    *,
    source_directory: Path | None = None,
    conversion_root: Path | None = None,
    evidence_root: Path | None = None,
    scenario_root: Path | None = None,
) -> ScenarioResult:
    """Run one scenario under a new run ID and check its evidence against the expected outcome.

    Raises :class:`ScenarioRefusedError`, before anything is written, if the run ID lacks the
    scenario's prefix or is already used by evidence, a database, or a scenario descriptor.
    """
    run_id = run_id or new_scenario_run_id(scenario)
    prefix = RUN_ID_PREFIXES[scenario]
    if not (RUN_ID_PATTERN.fullmatch(run_id) and run_id.startswith(prefix) and run_id != prefix):
        raise ScenarioRefusedError(
            f"Invalid run ID {run_id!r}: {scenario} runs must start with {prefix!r} and use at "
            "most 64 letters, digits, '-' or '_'."
        )
    roots = _Roots(
        source_directory or default_sample_directory(),
        conversion_root or default_conversion_root(),
        evidence_root or default_evidence_root(),
        scenario_root or default_scenario_root(),
    )
    for root in (roots.evidence, roots.conversion, roots.scenarios):
        if (root / run_id).exists():
            raise ScenarioRefusedError(f"{root / run_id} already exists; a run ID is never reused.")
    directory = roots.scenarios / run_id
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise ScenarioRefusedError(f"{directory} already exists; a run ID is never reused.") from None
    return _RUNNERS[scenario](run_id, roots, directory)


def inject_rate_defect(plan: ConversionPlan) -> tuple[ConversionPlan, InjectedRateDefect]:
    """A copy of ``plan`` whose first eligible application carries a wrong interest rate.

    Only that one mapped value changes: the rows, dispositions, counts, amounts, and source
    checksums are the planner's, so the loader writes the same number of records and dollars.
    """
    for index, row in enumerate(plan.applications):
        if row.disposition is Disposition.ELIGIBLE and isinstance(row.target, MappedApplication):
            planned = row.target.interest_rate
            injected = planned + RATE_DEFECT
            rows = list(plan.applications)
            rows[index] = replace(row, target=replace(row.target, interest_rate=injected))
            defective = replace(plan, applications=tuple(rows))
            defect = InjectedRateDefect(row.key, row.ref.line, planned, injected)
            break
    else:
        raise ValueError("The extract has no eligible application to alter.")
    if _load_shape(defective) != _load_shape(plan):
        raise AssertionError("Injecting the rate defect changed the load's counts or amounts.")
    return defective, defect


# --- Scenarios -----------------------------------------------------------------------------


@dataclass(frozen=True)
class _Roots:
    source: Path
    conversion: Path
    evidence: Path
    scenarios: Path


def _control_totals(run_id: str, roots: _Roots, directory: Path) -> ScenarioResult:
    extract = directory / EXTRACT_DIRECTORY
    extract.mkdir()
    for name in contract.DATA_FILES:
        shutil.copyfile(roots.source / name, extract / name)
    control, change = _misstated_control((roots.source / contract.CONTROL_FILE).read_bytes())
    (extract / contract.CONTROL_FILE).write_bytes(control)
    _write_descriptor(directory, Scenario.CONTROL_TOTALS, run_id, roots, {
        "source": str(extract), "altered": change,
    })

    deviations: list[str] = []
    try:
        run_conversion(
            extract, run_id, conversion_root=roots.conversion, evidence_root=roots.evidence
        )
    except SourceRunFailedError:
        pass
    except RunFailedError as error:
        deviations.append(f"The run failed outside validation: {error}")
    else:
        deviations.append("The extract passed validation and loaded.")

    evidence = roots.evidence / run_id
    manifest = _read_json(evidence / MANIFEST_NAME)
    findings: list[str] = []
    status, stage = _status(manifest, findings)
    _expect(deviations, status == "FAILED" and stage == "validation", "Expected FAILED at validation.")
    issues = (manifest.get("validation") or {}).get("issues") or []
    for issue in issues:
        findings.append(f"{issue.get('rule')} {issue.get('file')}: {issue.get('message')}")
    rules = sorted({issue.get("rule") for issue in issues})
    _expect(deviations, rules == [Rule.RUN_04, Rule.RUN_05],
            f"Expected run-level issues RUN-04 and RUN-05, found {rules}.")

    files = (manifest.get("source") or {}).get("files") or {}
    archived = 0
    for name in SOURCE_FILES:
        copy = evidence / SOURCE_DIRECTORY / name
        recorded = (files.get(name) or {}).get("archived_sha256")
        if copy.is_file() and recorded == _sha256(copy) == _sha256(extract / name):
            archived += 1
        else:
            deviations.append(f"{name} is not archived with a matching checksum.")
    findings.append(f"Archived {archived} of {len(SOURCE_FILES)} source files; checksums match.")
    database = roots.conversion / run_id
    _expect(deviations, not database.exists(), f"A target database directory exists: {database}")
    _expect(deviations, manifest.get("database_path") is None,
            "The manifest records a database path.")
    _expect(deviations, not (evidence / REPORTS_DIRECTORY / LOAD_REPORT_NAME).exists(),
            "A load report exists although nothing should have loaded.")
    if not database.exists():
        findings.append("No target database was created.")
    return _result(Scenario.CONTROL_TOTALS, run_id, roots, directory, status, stage,
                   findings, deviations)


def _business_rejections(run_id: str, roots: _Roots, directory: Path) -> ScenarioResult:
    _write_descriptor(directory, Scenario.BUSINESS_REJECTIONS, run_id, roots, {
        "source": str(roots.source), "altered": None,
    })
    deviations: list[str] = []
    findings: list[str] = []
    plan: ConversionPlan | None = None
    try:
        plan = run_conversion(
            roots.source, run_id, conversion_root=roots.conversion, evidence_root=roots.evidence
        ).plan
        reconcile_run(run_id, evidence_root=roots.evidence)
    except Exception as error:
        deviations.append(f"The run did not load and reconcile: {type(error).__name__}: {error}")

    evidence = roots.evidence / run_id
    manifest = _read_json(evidence / MANIFEST_NAME)
    status, stage = _status(manifest, findings)
    _expect(deviations, status == "RECONCILED", "Expected RECONCILED.")
    rows = (manifest.get("validation") or {}).get("rows") or {}
    for name in contract.DATA_FILES:
        counts = rows.get(name) or {}
        findings.append(
            f"{name}: read {counts.get('read')}, loaded {counts.get('eligible')}, "
            f"excluded {counts.get('excluded')}, rejected {counts.get('rejected')}"
        )
    for name in (contract.APPLICATIONS_FILE, contract.PARTIES_FILE):
        counts = rows.get(name) or {}
        _expect(deviations, (counts.get("rejected") or 0) > 0, f"Expected rejections in {name}.")
        _expect(deviations, (counts.get("excluded") or 0) > 0, f"Expected exclusions in {name}.")

    rules: set[str] = set()
    if plan is not None:
        for file in (contract.APPLICATIONS_FILE, contract.PARTIES_FILE):
            for row in plan.rows(file):
                if row.disposition is Disposition.REJECTED and not row.dependent:
                    rules.update(row.rules)
                    reasons = "; ".join(f"{i.rule} {i.message}" for i in row.issues)
                    findings.append(f"Rejected {file} line {row.ref.line} {row.key}: {reasons}")
    for rule, meaning in ((Rule.MP_02, "an invalid amount"),
                          (Rule.RF_06, "a missing primary borrower relationship"),
                          (Rule.RF_01, "a relationship to a missing application")):
        _expect(deviations, rule in rules, f"Expected a rejection for {meaning} ({rule}).")
    amounts = (manifest.get("validation") or {}).get("requested_amount") or {}
    findings.append(
        f"REQ_AMT eligible {amounts.get('eligible')}, excluded {amounts.get('excluded')}, "
        f"rejected {amounts.get('rejected')}"
    )
    reconciliation = manifest.get("reconciliation") or {}
    _expect(deviations, reconciliation.get("result") == "passed"
            and not reconciliation.get("rules_failed"), "Expected every RC rule to pass.")
    return _result(Scenario.BUSINESS_REJECTIONS, run_id, roots, directory, status, stage,
                   findings, deviations)


def _loader_defect(run_id: str, roots: _Roots, directory: Path) -> ScenarioResult:
    plan = plan_conversion(roots.source)
    defective, defect = inject_rate_defect(plan)
    _write_descriptor(directory, Scenario.LOADER_DEFECT, run_id, roots, {
        "source": str(roots.source), "altered": defect.to_json(),
    })
    deviations: list[str] = []
    findings = [
        f"Injected: {defect.application} ({contract.APPLICATIONS_FILE} line {defect.line}) "
        f"interest rate {defect.planned_rate} -> {defect.injected_rate} in the load input"
    ]
    try:
        run_load(
            defective, roots.source, run_id,
            conversion_root=roots.conversion, evidence_root=roots.evidence,
        )
        reconcile_run(run_id, evidence_root=roots.evidence)
    except Exception as error:
        deviations.append(f"The run did not load and reconcile: {type(error).__name__}: {error}")

    evidence = roots.evidence / run_id
    manifest = _read_json(evidence / MANIFEST_NAME)
    status, stage = _status(manifest, findings)
    _expect(deviations, status == "FAILED" and stage == "reconciliation",
            "Expected FAILED at reconciliation.")

    expected = manifest.get("expected_target") or {}
    loaded = (_read_json(evidence / REPORTS_DIRECTORY / LOAD_REPORT_NAME).get("loaded") or {})
    shape = _load_shape(plan)
    findings.append(
        f"Loaded {loaded.get('borrowers')} borrowers, {loaded.get('applications')} applications, "
        f"{loaded.get('parties')} parties, requested amount {loaded.get('requested_amount')}"
    )
    for key in ("borrowers", "applications", "parties", "requested_amount"):
        planned = str(shape[key])
        _expect(deviations, str(loaded.get(key)) == str(expected.get(key)) == planned,
                f"Loaded {key} {loaded.get(key)} differs from the plan ({planned}).")

    reconciliation = manifest.get("reconciliation") or {}
    failed = reconciliation.get("rules_failed")
    findings.append(f"Failed rules: {', '.join(failed or []) or 'none'}")
    _expect(deviations, failed == ["RC-07"], f"Expected only RC-07 to fail, found {failed}.")
    report = _read_json(evidence / REPORTS_DIRECTORY / RECONCILIATION_REPORT_NAME)
    discrepancies = report.get("discrepancies") or []
    for item in discrepancies:
        findings.append(
            f"{item.get('rule')} {item.get('file')}:{item.get('line')} {item.get('source_key')} "
            f"{item.get('field')}: expected {item.get('expected')}, actual {item.get('actual')}"
        )
    caught = [
        item for item in discrepancies
        if item.get("source_key") == defect.application and item.get("field") == "interest_rate"
        and item.get("expected") == str(defect.planned_rate)
        and item.get("actual") == str(defect.injected_rate)
    ]
    _expect(deviations, len(caught) == 1 == len(discrepancies),
            "Expected exactly one discrepancy: the injected interest rate.")
    return _result(Scenario.LOADER_DEFECT, run_id, roots, directory, status, stage,
                   findings, deviations, defect)


_RUNNERS: dict[Scenario, Callable[[str, _Roots, Path], ScenarioResult]] = {
    Scenario.CONTROL_TOTALS: _control_totals,
    Scenario.BUSINESS_REJECTIONS: _business_rejections,
    Scenario.LOADER_DEFECT: _loader_defect,
}


# --- Helpers -------------------------------------------------------------------------------


def _misstated_control(data: bytes) -> tuple[bytes, dict[str, Any]]:
    """The control file with the applications RECORD_COUNT and AMOUNT_TOTAL misstated."""
    text = data.decode("utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    rows = list(csv.reader(io.StringIO(text, newline="")))
    header = rows[0]
    name, count, amount = (header.index(f) for f in ("FILE_NAME", "RECORD_COUNT", "AMOUNT_TOTAL"))
    for row in rows[1:]:
        if row[name] == contract.APPLICATIONS_FILE:
            change = {
                "file": contract.CONTROL_FILE,
                "row": contract.APPLICATIONS_FILE,
                "RECORD_COUNT": {"extract": row[count]},
                "AMOUNT_TOTAL": {"extract": row[amount]},
            }
            row[count] = str(int(row[count]) + CONTROL_COUNT_ERROR)
            row[amount] = str(Decimal(row[amount]) + CONTROL_AMOUNT_ERROR)
            change["RECORD_COUNT"]["scenario"] = row[count]
            change["AMOUNT_TOTAL"]["scenario"] = row[amount]
            break
    else:
        raise ValueError(f"{contract.CONTROL_FILE} has no {contract.APPLICATIONS_FILE} row.")
    output = io.StringIO()
    csv.writer(output, lineterminator=newline).writerows(rows)
    return output.getvalue().encode("utf-8"), change


def _load_shape(plan: ConversionPlan) -> dict[str, Any]:
    return {
        "borrowers": len(plan.borrowers_to_load),
        "applications": len(plan.applications_to_load),
        "parties": len(plan.parties_to_load),
        "requested_amount": plan.amounts.eligible,
    }


def _write_descriptor(
    directory: Path, scenario: Scenario, run_id: str, roots: _Roots, details: dict[str, Any]
) -> None:
    descriptor = {
        "label": LABEL,
        "synthetic": True,
        "demo_only": True,
        "scenario": str(scenario),
        "title": TITLES[scenario],
        "expected": EXPECTED[scenario],
        "run_id": run_id,
        "evidence_directory": str(roots.evidence / run_id),
        **details,
    }
    with (directory / SCENARIO_FILE).open("x", encoding="utf-8") as file:
        json.dump(descriptor, file, indent=2)
        file.write("\n")


def _status(manifest: dict[str, Any], findings: list[str]) -> tuple[str | None, str | None]:
    status = manifest.get("status")
    stage = (manifest.get("failure") or {}).get("stage")
    findings.insert(0, f"Status {status}" + (f" at {stage}" if stage else ""))
    return status, stage


def _expect(deviations: list[str], condition: bool, message: str) -> None:
    if not condition:
        deviations.append(message)


def _result(
    scenario: Scenario, run_id: str, roots: _Roots, directory: Path, status: str | None,
    stage: str | None, findings: list[str], deviations: list[str],
    defect: InjectedRateDefect | None = None,
) -> ScenarioResult:
    return ScenarioResult(
        scenario, run_id, roots.evidence / run_id, directory, status, stage,
        tuple(findings), tuple(dict.fromkeys(deviations)), defect,
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
