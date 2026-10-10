"""Milestone 10 Phase 1C: independent eligibility in reconciliation (RC-11, RC-12; spec 12.1-12.6).

Converter defects are injected the way a real defect would arise: either through a converter
table, or by replacing rows of the conversion plan before its reports, manifest counts, and load
are produced from it. The recorded evidence is then internally consistent and checksummed, so
only reconciliation's independent determination can expose the defect. Expected outcomes are
written by hand from docs/conversion-specification.md, never computed by the converter.
"""

import csv
import dataclasses
import hashlib
import io
import json
import shutil
from collections.abc import Callable, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from loan_lab.conversion.legacy import (
    ReconciliationRefusedError,
    ReconciliationResult,
    RunStatus,
    check_ready,
    contract,
    reconcile_run,
    recover_run,
    run_conversion,
)
from loan_lab.conversion.legacy import reconcile, run
from loan_lab.conversion.legacy.loader import DATABASE_NAME
from loan_lab.conversion.legacy.contract import Rule
from loan_lab.conversion.legacy.plan import Cause, Disposition, Issue, RowResult, SourceRef
from loan_lab.paths import find_project_root
from loan_lab.web import conversions

SAMPLE_DIR = find_project_root(Path(__file__)) / "sample_data" / "legacy"
B = "borrowers.csv"
A = "applications.csv"
P = "application_parties.csv"
ALL_RULES = [f"RC-{n:02d}" for n in range(1, 13)]


class Workspace:
    def __init__(self, root: Path, source: Path = SAMPLE_DIR) -> None:
        self.source = root / "source"
        shutil.copytree(source, self.source)
        self.databases = root / "data" / "conversion"
        self.evidence_root = root / "output" / "conversion"

    def convert(self, run_id: str = "run-1") -> None:
        run_conversion(
            self.source, run_id, conversion_root=self.databases,
            evidence_root=self.evidence_root, batch_size=4,
        )

    def reconcile(self, run_id: str = "run-1") -> ReconciliationResult:
        return reconcile_run(run_id, evidence_root=self.evidence_root)

    def evidence(self, run_id: str = "run-1") -> Path:
        return self.evidence_root / run_id

    def manifest(self) -> dict[str, Any]:
        return json.loads((self.evidence() / "manifest.json").read_text("utf-8"))

    def write_manifest(self, manifest: Mapping[str, Any]) -> None:
        (self.evidence() / "manifest.json").write_text(json.dumps(manifest, indent=2), "utf-8")

    def report_path(self) -> Path:
        return self.evidence() / "reports" / "reconciliation.json"

    def report(self) -> dict[str, Any]:
        return json.loads(self.report_path().read_text("utf-8"))

    def snapshot(self) -> dict[str, bytes]:
        files = {
            p.relative_to(self.evidence()).as_posix(): p.read_bytes()
            for p in self.evidence().rglob("*") if p.is_file()
        }
        files["<database>"] = (self.databases / "run-1" / DATABASE_NAME).read_bytes()
        return files


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path)


def found(result: ReconciliationResult, rule: str, check: str | None = None) -> list[Any]:
    return [
        d for d in result.discrepancies if d.rule == rule and (check is None or d.check == check)
    ]


def failed(result: ReconciliationResult) -> set[str]:
    return {str(rule) for rule in result.failed_rules}


def where(result: ReconciliationResult, rule: str) -> set[tuple[str, str, int | None]]:
    return {(d.check, d.file, d.line) for d in found(result, rule)}


# --- Defect injection --------------------------------------------------------------------------


Edit = Callable[[RowResult], RowResult]


def plan_defect(monkeypatch: pytest.MonkeyPatch, edits: Mapping[tuple[str, int], Edit]) -> None:
    """Make the converter's plan wrong at the given source lines, before anything is written."""
    real = run.plan_conversion

    def plan(directory: Path) -> Any:
        result = real(directory)

        def fix(rows: tuple[RowResult, ...]) -> tuple[RowResult, ...]:
            return tuple(edits.get((r.ref.file, r.ref.line), lambda r: r)(r) for r in rows)

        return dataclasses.replace(
            result, borrowers=fix(result.borrowers), applications=fix(result.applications),
            parties=fix(result.parties),
        )

    monkeypatch.setattr(run, "plan_conversion", plan)


def convert_with(ws: Workspace, monkeypatch: pytest.MonkeyPatch, **edits: Edit) -> None:
    """``edits`` maps ``"B12"``-style names (file initial and line) to a row change."""
    files = {"B": B, "A": A, "P": P}
    with monkeypatch.context() as patch:
        plan_defect(patch, {(files[name[0]], int(name[1:])): edit for name, edit in edits.items()})
        ws.convert()
    assert check_ready("run-1", evidence_root=ws.evidence_root).ready


def issue_edit(change: Callable[[Issue], Issue | None], rule: str | None = None) -> Edit:
    """Change (or drop, when ``change`` returns None) the row's issues, or only ``rule``'s."""
    def edit(row: RowResult) -> RowResult:
        issues = []
        for issue in row.issues:
            changed = change(issue) if rule is None or issue.rule == rule else issue
            if changed is not None:
                issues.append(changed)
        return dataclasses.replace(row, issues=tuple(issues))
    return edit


def warning_edit(change: Callable[[tuple[Issue, ...]], tuple[Issue, ...]]) -> Edit:
    return lambda row: dataclasses.replace(row, warnings=change(row.warnings))


def exclude_instead(row: RowResult) -> RowResult:
    """Record a rejected row (borrowers.csv:12 fails SV-02) as an EX-01 exclusion instead."""
    return dataclasses.replace(
        row, disposition=Disposition.EXCLUDED,
        issues=(Issue(Rule.EX_01, "Excluded (injected).", "RECORD_STATUS", "A",
                      (Cause(Rule.EX_01, row.ref),)),),
    )


def reseal(ws: Workspace, kind: str, change: Callable[[list[list[str]]], list[list[str]]]) -> None:
    """Rewrite a record report and its manifest checksum, as a defective converter would have."""
    path = ws.evidence() / "reports" / f"{kind}.csv"
    rows = list(csv.reader(io.StringIO(path.read_text("utf-8"), newline="")))
    buffer = io.StringIO(newline="")
    csv.writer(buffer, lineterminator="\r\n").writerows([rows[0], *change(rows[1:])])
    data = buffer.getvalue().encode("utf-8")
    path.write_bytes(data)
    manifest = ws.manifest()
    manifest["reports"]["files"][kind].update(
        sha256=hashlib.sha256(data).hexdigest(), bytes=len(data)
    )
    ws.write_manifest(manifest)


# --- The sample: corrected converter -----------------------------------------------------------


def test_new_sample_run_passes_rc01_to_rc12(ws: Workspace) -> None:
    ws.convert()

    result = ws.reconcile()

    assert result.passed, result.discrepancies
    assert [str(r.rule) for r in result.rules] == ALL_RULES
    assert all(r.passed and r.complete and r.checked > 0 for r in result.rules)
    report = ws.report()
    assert report["report_version"] == 2
    assert all(item["result"] == "PASS" and item["complete"] for item in report["rules"])
    assert report["rules"][11]["not_evaluated"] == {
        "lines": 0, "expected_rows": 0, "recorded_rows": 0,
    }
    assert "not_evaluated" not in report["rules"][10]
    eligibility = report["eligibility"]
    assert eligibility["row_details"] == "complete"
    # Spec 15.4, by hand.
    assert {f: e["independent"] for f, e in eligibility["files"].items()} == {
        B: {"loaded": 11, "excluded": 1, "rejected": 4, "excluded_dependent": 0,
            "rejected_dependent": 0},
        A: {"loaded": 6, "excluded": 2, "rejected": 5, "excluded_dependent": 0,
            "rejected_dependent": 0},
        P: {"loaded": 10, "excluded": 3, "rejected": 7, "excluded_dependent": 2,
            "rejected_dependent": 3},
    }
    assert eligibility["files"][P]["recorded"] == {
        "loaded": 10, "excluded": 3, "rejected": 7, "excluded_dependent": 2,
        "rejected_dependent": 3, "recorded_twice": 0,
    }
    assert all(e["recorded"]["recorded_twice"] == 0 for e in eligibility["files"].values())
    assert eligibility["lines_compared"] == 49
    assert eligibility["disposition_discrepancies"] == 0
    assert eligibility["warnings"] == ["00010008", "00010009", "00010015"]
    record = ws.manifest()["reconciliation"]
    assert record["report_version"] == 2
    assert record["eligibility"]["independently_verified"] is True
    assert record["eligibility"]["row_details"] == "complete"
    assert record["rules_failed"] == [] and record["rules_incomplete"] == []
    assert record["eligibility"]["loaded"] == {B: 11, A: 6, P: 10}
    assert reconcile.verify_reconciliation("run-1", evidence_root=ws.evidence_root).verified


def test_reconciliation_never_runs_the_planner(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws.convert()

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("reconciliation ran the planner")

    from loan_lab.conversion.legacy import planner
    monkeypatch.setattr(planner, "plan_conversion", forbidden)
    monkeypatch.setattr(run, "plan_conversion", forbidden)

    assert ws.reconcile().passed
    assert not hasattr(reconcile, "plan_conversion")


# --- The 60-month planner defect ---------------------------------------------------------------


def test_sixty_month_planner_defect_fails_with_rc11_primary(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The planner wrongly treats 60 months as out of range, rejecting the valid 0000500105
    # (applications.csv:6) and, through its unit, its valid primary party (parties line 11).
    with monkeypatch.context() as patch:
        patch.setattr(contract, "MIN_TERM_MONTHS", 61)
        ws.convert()
    assert check_ready("run-1", evidence_root=ws.evidence_root).ready
    before = ws.snapshot()

    result = ws.reconcile()

    assert result.status is RunStatus.FAILED
    rc11 = {(d.check, d.file, d.line): d for d in found(result, "RC-11") if d.line}
    assert set(rc11) == {
        ("wrongly_rejected", A, 6), ("missing_from_target", A, 6),
        ("wrongly_rejected", P, 11), ("missing_from_target", P, 11),
    }
    rejected = rc11[("wrongly_rejected", A, 6)]
    assert (rejected.source_key, rejected.unit_key, rejected.field) == (
        "0000500105", "0000500105", "disposition",
    )
    assert (rejected.expected, rejected.actual) == ("loaded", "rejected")
    assert rejected.evidence == "reports/exceptions.csv:6"
    assert "MP-03" in rejected.message
    missing = rc11[("missing_from_target", A, 6)]
    assert (missing.target_table, missing.expected, missing.actual, missing.evidence) == (
        "loan_application", "present", "absent", None,
    )
    party = rc11[("wrongly_rejected", P, 11)]
    assert (party.source_key, party.unit_key) == ("0000500105/00010007", "0000500105")
    counts = {(d.file, d.expected, d.actual) for d in found(result, "RC-11", "disposition_count")}
    assert counts == {
        (A, "loaded=6", "loaded=5"), (A, "rejected=5", "rejected=6"),
        (P, "loaded=10", "loaded=9"), (P, "rejected=7", "rejected=8"),
        (P, "rejected_dependent=3", "rejected_dependent=4"),
    }
    assert all(d.evidence == "manifest.json" for d in found(result, "RC-11", "disposition_count"))
    # The defect's other effects are reported too; nothing is skipped.
    assert {"RC-01", "RC-03", "RC-04", "RC-05", "RC-06", "RC-10", "RC-12"} <= failed(result)
    assert ("unexpected_warning", B, 8) in where(result, "RC-12")
    assert ("unexpected_rule", A, 10) in where(result, "RC-12")
    # The row details of 0000500105 and its party (their MP-03 exception rows) are left to RC-11,
    # disclosed, and never counted as verified.
    rc12 = next(r for r in result.rules if r.rule == "RC-12")
    assert rc12.result == "FAIL" and not rc12.complete
    assert rc12.not_evaluated == {"lines": 2, "expected_rows": 0, "recorded_rows": 2}
    assert rc12.note.startswith("Incomplete: exception and exclusion row details of 2 lines")
    assert not found(result, "RC-12") or all(
        (d.file, d.line) not in {(A, 6), (P, 11)} or d.check.endswith("warning")
        for d in found(result, "RC-12")
    )
    assert result.incomplete_rules == ("RC-12",)

    manifest = ws.manifest()
    assert manifest["status"] == "FAILED"
    assert manifest["failure"]["rule"] == "RC-11"
    assert manifest["failure"]["stage"] == "reconciliation"
    assert "Not fully evaluated: RC-12." in manifest["failure"]["reason"]
    assert manifest["reconciliation"]["rules_failed"] == [
        "RC-01", "RC-03", "RC-04", "RC-05", "RC-06", "RC-10", "RC-11", "RC-12",
    ]
    assert manifest["reconciliation"]["rules_incomplete"] == ["RC-12"]
    assert manifest["reconciliation"]["release_review"] == "blocked"
    assert manifest["reconciliation"]["report_version"] == 2
    eligibility = manifest["reconciliation"]["eligibility"]
    assert eligibility["disposition_discrepancies"] == 2
    assert eligibility["independently_verified"] is False
    assert eligibility["row_details"] == "incomplete"
    assert eligibility["not_evaluated_by_rc12"] == rc12.not_evaluated
    assert manifest["release"] is None
    report = ws.report()
    assert report["result"] == "FAIL"
    assert len(report["discrepancies"]) == len(result.discrepancies)
    assert report["discrepancies"][0]["rule"] == "RC-01"
    # Only the manifest changed and the report was added; source, reports, database untouched.
    after = ws.snapshot()
    assert set(after) == set(before) | {"reports/reconciliation.json"}
    assert all(after[name] == data for name, data in before.items() if name != "manifest.json")
    # A failed run is final.
    with pytest.raises(ReconciliationRefusedError):
        ws.reconcile()
    assert recover_run("run-1", evidence_root=ws.evidence_root).changed is False


def test_discrepancies_are_sorted_and_serialized_with_evidence(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(contract, "MIN_TERM_MONTHS", 61)
        ws.convert()

    result = ws.reconcile()

    order = {B: 0, A: 1, P: 2, "extract_control.csv": 3}
    keys = [
        (d.rule, order.get(d.file or "", 4), -1 if d.line is None else d.line, d.check)
        for d in result.discrepancies
    ]
    assert keys == sorted(keys)
    item = next(d for d in ws.report()["discrepancies"] if d["check"] == "wrongly_rejected")
    assert list(item) == [
        "rule", "check", "file", "line", "source_key", "unit_key", "evidence", "target_table",
        "target_id", "field", "expected", "actual", "message",
    ]


# --- RC-12 row details left to RC-11 (spec 12.4) -----------------------------------------------


def test_rule_with_unmade_comparisons_is_incomplete_never_pass() -> None:
    skipped = {"lines": 1, "expected_rows": 1, "recorded_rows": 0}
    none = {"lines": 0, "expected_rows": 0, "recorded_rows": 0}

    incomplete = reconcile.RuleResult(reconcile.RC.RC_12, 10, 0, None, skipped)
    failed_too = reconcile.RuleResult(reconcile.RC.RC_12, 10, 2, None, skipped)
    complete = reconcile.RuleResult(reconcile.RC.RC_12, 10, 0, None, none)

    assert (incomplete.result, incomplete.passed, incomplete.complete) == (
        "INCOMPLETE", False, False,
    )
    assert (failed_too.result, failed_too.complete) == ("FAIL", False)
    assert (complete.result, complete.passed) == ("PASS", True)
    assert reconcile.RuleResult(reconcile.RC.RC_01, 10, 0).result == "PASS"


def test_rc12_skipped_details_are_counted_disclosed_and_block_reconciliation(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The converter records borrowers.csv:12 (SV-02, one expected exceptions row) as an EX-01
    # exclusion. RC-11 reports the line; its rows are not compared again by RC-12, which finds
    # nothing else wrong. RC-12 must then say INCOMPLETE, never PASS.
    convert_with(ws, monkeypatch, B12=exclude_instead)

    result = ws.reconcile()

    assert result.status is RunStatus.FAILED and not result.passed
    rc12 = next(r for r in result.rules if r.rule == "RC-12")
    assert (rc12.discrepancies, rc12.result, rc12.passed) == (0, "INCOMPLETE", False)
    assert rc12.not_evaluated == {"lines": 1, "expected_rows": 1, "recorded_rows": 1}
    assert rc12.note == (
        "Incomplete: exception and exclusion row details of 1 lines were not compared "
        "(1 expected rows, 1 recorded rows), because RC-11 found their dispositions wrong. "
        "Those details are unexamined, not verified; their warnings were still compared."
    )
    assert "RC-12" not in failed(result)
    assert result.incomplete_rules == ("RC-12",)
    # RC-11 alone reports the line; RC-12 does not restate it.
    assert ("excluded_instead_of_rejected", B, 12) in where(result, "RC-11")
    assert not found(result, "RC-12")

    report = ws.report()
    assert report["result"] == "FAIL" and report["status"] == "FAILED"
    assert report["rules"][11] == {
        "rule": "RC-12", "title": "Independent report rows and warnings",
        "result": "INCOMPLETE", "checked": rc12.checked, "discrepancies": 0, "complete": False,
        "not_evaluated": {"lines": 1, "expected_rows": 1, "recorded_rows": 1}, "note": rc12.note,
    }
    assert report["eligibility"]["row_details"] == "incomplete"
    assert report["eligibility"]["not_evaluated_by_rc12"] == rc12.not_evaluated

    manifest = ws.manifest()
    record = manifest["reconciliation"]
    assert manifest["status"] == "FAILED"
    assert manifest["failure"]["rule"] == "RC-11"
    assert "RC-12" not in record["rules_failed"] and "RC-11" in record["rules_failed"]
    assert record["rules_incomplete"] == ["RC-12"]
    assert record["result"] == "failed"
    assert record["eligibility"]["independently_verified"] is False
    assert record["eligibility"]["row_details"] == "incomplete"
    assert not reconcile.verify_reconciliation("run-1", evidence_root=ws.evidence_root).verified

    view = conversions.load_reconciliation(ws.evidence_root, "run-1")
    assert view is not None and view.report_verified
    shown = {r.rule: r for r in view.rules}
    assert shown["RC-12"].result == "INCOMPLETE"
    assert shown["RC-12"].unexamined_lines == 1
    assert [r.rule for r in view.unexamined_rules] == ["RC-12"]


def test_rc12_failure_with_skipped_details_stays_disclosed(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Wrongful acceptance of 00010012 (borrowers.csv:13, EX-01). Its expected exclusion row is
    # left to RC-11, but the WN-01 warning the converter wrote for it is still compared.
    with monkeypatch.context() as patch:
        patch.setattr(contract, "DELETED", "Z")
        ws.convert()

    result = ws.reconcile()

    rc12 = next(r for r in result.rules if r.rule == "RC-12")
    assert rc12.result == "FAIL" and not rc12.complete
    assert rc12.not_evaluated == {"lines": 1, "expected_rows": 1, "recorded_rows": 0}
    assert where(result, "RC-12") == {("unexpected_warning", B, 13)}
    assert result.incomplete_rules == ("RC-12",)
    record = ws.manifest()["reconciliation"]
    assert "RC-12" in record["rules_failed"] and record["rules_incomplete"] == ["RC-12"]
    assert ws.manifest()["failure"]["rule"] == "RC-11"
    view = conversions.load_reconciliation(ws.evidence_root, "run-1")
    assert view is not None
    assert [(r.rule, r.result) for r in view.unexamined_rules] == [("RC-12", "FAIL")]


def test_rc12_skipped_details_are_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reports = []
    for name in ("first", "second"):
        ws = Workspace(tmp_path / name)
        with monkeypatch.context() as patch:
            patch.setattr(contract, "MIN_TERM_MONTHS", 61)
            ws.convert()
        ws.reconcile()
        report = ws.report()
        reports.append((report["rules"], report["eligibility"], report["discrepancies"]))

    assert reports[0] == reports[1]


# --- Defect injection: dispositions (RC-11) ----------------------------------------------------


def test_wrongful_acceptance_is_wrongly_loaded_and_unexpected_in_target(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The converter forgets that RECORD_STATUS D is a logical delete: 00010012 (borrowers.csv:13)
    # loads although spec 10 excludes it with EX-01.
    with monkeypatch.context() as patch:
        patch.setattr(contract, "DELETED", "Z")
        ws.convert()

    result = ws.reconcile()

    assert {("wrongly_loaded", B, 13), ("unexpected_in_target", B, 13)} <= where(result, "RC-11")
    loaded = next(d for d in found(result, "RC-11", "wrongly_loaded") if d.line == 13)
    assert (loaded.source_key, loaded.expected, loaded.actual, loaded.evidence) == (
        "00010012", "excluded", "loaded", "reports/exclusions.csv",
    )
    assert ("loaded_despite_exclusion", B, 13) in where(result, "RC-01")
    extra = next(d for d in found(result, "RC-11", "unexpected_in_target") if d.line == 13)
    assert extra.target_table == "borrower" and extra.target_id is not None
    assert ws.manifest()["failure"]["rule"] == "RC-11"


def test_exclusion_recorded_as_rejection(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    # The converter forgets that product 330 is out of scope (spec 10, EX-02).
    with monkeypatch.context() as patch:
        patch.setattr(contract, "EXCLUDED_PRODUCTS", frozenset({"900"}))
        ws.convert()

    result = ws.reconcile()

    assert {
        ("rejected_instead_of_excluded", A, 11), ("rejected_instead_of_excluded", P, 17),
    } <= where(result, "RC-11")
    assert ws.manifest()["failure"]["rule"] == "RC-11"


def test_rejection_recorded_as_exclusion(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    convert_with(ws, monkeypatch, B12=exclude_instead)

    result = ws.reconcile()

    [wrong] = found(result, "RC-11", "excluded_instead_of_rejected")
    assert (wrong.file, wrong.line, wrong.expected, wrong.actual) == (B, 12, "rejected", "excluded")
    assert wrong.evidence.startswith("reports/exclusions.csv:")
    # RC-01's raw-text cross-check sees it as well: status A meets no exclusion criterion.
    assert ("exclusion_without_criterion", B, 12) in where(result, "RC-01")


def test_line_recorded_twice(ws: Workspace) -> None:
    ws.convert()
    # borrowers.csv:12 (SV-02) also appears in exclusions.csv, with the manifest's report
    # accounting made to agree, as a converter writing both would have recorded it.
    raw = (SAMPLE_DIR / B).read_text("utf-8").splitlines()[11]
    reseal(ws, "exclusions", lambda rows: [
        [B, "12", "00010011", "", "exclusion", "EX-01", "N", "EX-01 borrowers.csv:12",
         "RECORD_STATUS", "A", "", "", raw],
        *rows,
    ])
    manifest = ws.manifest()
    manifest["validation"]["rows"][B]["excluded"] += 1
    entry = manifest["reports"]["files"]["exclusions"]
    entry["source_rows"][B] += 1
    entry["rows"] += 1
    entry["rules"]["EX-01"] += 1
    ws.write_manifest(manifest)
    assert check_ready("run-1", evidence_root=ws.evidence_root).ready

    result = ws.reconcile()

    [twice] = found(result, "RC-11", "recorded_twice")
    assert (twice.file, twice.line, twice.expected, twice.actual) == (
        B, 12, "rejected", "rejected and excluded",
    )
    assert twice.evidence.startswith("reports/exceptions.csv:")
    # Not also given a disposition check, and not evaluated by RC-12.
    assert not [d for d in found(result, "RC-11") if d.line == 12 and d.check != "recorded_twice"]
    assert not [d for d in found(result, "RC-12") if d.line == 12]
    assert ("disposition_total", B, None) in where(result, "RC-01")


def test_misplaced_report_row_is_named(ws: Workspace) -> None:
    ws.convert()

    def misplace(rows: list[list[str]]) -> list[list[str]]:
        return [[r[0], "99", *r[2:]] if r[:2] == [A, "8"] else r for r in rows]

    reseal(ws, "exceptions", misplace)
    # The row count per file is unchanged in the manifest record, so only the line moved.
    result = ws.reconcile()

    [orphan] = found(result, "RC-11", "report_row_without_source_line")
    assert (orphan.file, orphan.line, orphan.actual) == (A, 99, f"{A}:99")
    assert orphan.evidence.startswith("reports/exceptions.csv:")
    assert ("wrongly_loaded", A, 8) in where(result, "RC-11")


# --- Defect injection: report rows (RC-12) -----------------------------------------------------


def test_missing_rule_code(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    # applications.csv:10 has RF-07 and RF-06; the converter drops RF-06.
    convert_with(ws, monkeypatch, A10=issue_edit(lambda i: None, rule=Rule.RF_06))

    result = ws.reconcile()

    assert failed(result) == {"RC-12"}
    [missing] = found(result, "RC-12")
    assert (missing.check, missing.file, missing.line, missing.unit_key) == (
        "missing_rule", A, 10, "0000500109",
    )
    assert (missing.field, missing.expected, missing.actual, missing.evidence) == (
        "RULE_CODE", "RF-06 =", "none", "reports/exceptions.csv",
    )
    # RC-11 passing means failure.rule is the first failing rule.
    assert ws.manifest()["failure"]["rule"] == "RC-12"


def test_incorrect_rule_code(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    convert_with(ws, monkeypatch, A10=issue_edit(
        lambda i: dataclasses.replace(i, rule=Rule.RF_08), rule=Rule.RF_06,
    ))

    result = ws.reconcile()

    assert {(d.check, d.expected, d.actual) for d in found(result, "RC-12") if d.line == 10} >= {
        ("missing_rule", "RF-06 =", "none"), ("unexpected_rule", "none", "RF-08 ="),
    }


def test_rule_code_in_the_wrong_report_is_unexpected(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A rejected line gains an EX code in exceptions.csv.
    convert_with(ws, monkeypatch, A10=lambda row: dataclasses.replace(
        row, issues=(*row.issues, Issue(Rule.EX_02, "Injected.", "PROD_CD", "110")),
    ))

    result = ws.reconcile()

    [extra] = found(result, "RC-12", "unexpected_rule")
    assert (extra.line, extra.actual) == (10, "EX-02 PROD_CD=110")
    assert extra.evidence.startswith("reports/exceptions.csv:")


def test_incorrect_dependent_flag(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    # application_parties.csv:13 is RF-05 and dependent; the converter marks it independent.
    convert_with(ws, monkeypatch, P13=lambda row: dataclasses.replace(row, dependent=False))

    result = ws.reconcile()

    [flag] = found(result, "RC-12", "dependent_mismatch")
    assert (flag.file, flag.line, flag.field, flag.expected, flag.actual) == (
        P, 13, "DEPENDENT", "Y", "N",
    )
    # The manifest's dependent count is wrong too, which RC-11 compares.
    [count] = found(result, "RC-11", "disposition_count")
    assert (count.expected, count.actual) == ("rejected_dependent=3", "rejected_dependent=2")


def test_incorrect_unit_key(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    convert_with(ws, monkeypatch, P13=lambda row: dataclasses.replace(row, unit_key="0000500999"))

    result = ws.reconcile()

    assert failed(result) == {"RC-12"}
    [unit] = found(result, "RC-12")
    assert (unit.check, unit.line, unit.expected, unit.actual) == (
        "unit_key_mismatch", 13, "0000500107", "0000500999",
    )


def test_rf06_root_cause_naming_a_rejected_guarantor_is_c1(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The pre-Phase-1B converter behavior (spec 12.7 C1), as recorded in historical version 1
    # evidence: RF-06 on applications.csv:10 also names the rejected guarantor on line 16.
    def c1(issue: Issue) -> Issue:
        extra = Cause(Rule.RF_02, SourceRef(P, 16))
        return dataclasses.replace(issue, causes=(*issue.causes, extra))

    convert_with(ws, monkeypatch, A10=issue_edit(c1, rule=Rule.RF_06))

    result = ws.reconcile()

    assert failed(result) == {"RC-12"}
    [cause] = found(result, "RC-12")
    assert (cause.check, cause.line, cause.field) == ("root_cause_mismatch", 10, "ROOT_CAUSE")
    assert cause.expected == "RF-03 application_parties.csv:15"
    assert cause.actual == "RF-03 application_parties.csv:15; RF-02 application_parties.csv:16"


def test_unreadable_root_cause(ws: Workspace) -> None:
    ws.convert()
    reseal(ws, "exceptions", lambda rows: [
        [*r[:7], "SV-02 borrowers.csv:12, extra", *r[8:]] if r[:2] == [B, "12"] else r
        for r in rows
    ])

    result = ws.reconcile()

    [bad] = found(result, "RC-12", "root_cause_unreadable")
    assert (bad.file, bad.line, bad.actual) == (B, 12, "SV-02 borrowers.csv:12, extra")


@pytest.mark.parametrize(
    ("change", "check", "expected", "actual"),
    [
        # FIELD: the SV-02 row names the wrong column.
        (lambda i: dataclasses.replace(i, field="FIRST_NAME"), "missing_row",
         "SV-02 LAST_NAME=", "none"),
        # SOURCE_VALUE: the blank value is reported padded.
        (lambda i: dataclasses.replace(i, value=" "), "source_value_mismatch", "", " "),
    ],
    ids=["field", "source_value"],
)
def test_incorrect_field_or_source_value(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch,
    change: Callable[[Issue], Issue], check: str, expected: str, actual: str,
) -> None:
    # borrowers.csv:12 is SV-02 for a blank LAST_NAME (spec 15.1).
    convert_with(ws, monkeypatch, B12=issue_edit(change, rule=Rule.SV_02))

    result = ws.reconcile()

    assert failed(result) == {"RC-12"}
    found_here = {(d.check, d.expected, d.actual) for d in found(result, "RC-12")}
    assert (check, expected, actual) in found_here
    if check == "missing_row":
        assert ("unexpected_row", "none", "SV-02 FIRST_NAME=") in found_here


def test_incorrect_source_line(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    convert_with(ws, monkeypatch, B12=lambda row: dataclasses.replace(row, raw=row.raw + " "))

    result = ws.reconcile()

    [line] = found(result, "RC-12", "source_line_mismatch")
    assert line.actual == line.expected + " "


def test_missing_warning(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    # 00010008 (borrowers.csv:9) is WN-01 (spec 15.1); the converter omits the warning.
    convert_with(ws, monkeypatch, B9=warning_edit(lambda warnings: ()))

    result = ws.reconcile()

    assert failed(result) == {"RC-10", "RC-12"}
    [missing] = found(result, "RC-12")
    assert (missing.check, missing.line, missing.evidence) == (
        "missing_warning", 9, "reports/warnings.csv",
    )
    [listed] = found(result, "RC-10", "warning_list_mismatch")
    assert (listed.source_key, listed.expected, listed.actual) == ("00010008", "listed", "not listed")


def test_unexpected_warning_and_wrong_warning_causes(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    extra = Issue(Rule.WN_01, "Injected.")
    convert_with(
        ws, monkeypatch,
        # 00010001 (borrowers.csv:2) has loaded relationships, so no WN-01 applies.
        B2=warning_edit(lambda warnings: (*warnings, extra)),
        # 00010008's warning names only one of its two relationship lines.
        B9=warning_edit(lambda warnings: tuple(
            dataclasses.replace(w, causes=w.causes[:1]) for w in warnings
        )),
    )

    result = ws.reconcile()

    checks = {(d.check, d.line) for d in found(result, "RC-12")}
    assert checks == {("unexpected_warning", 2), ("warning_cause_mismatch", 9)}
    [cause] = found(result, "RC-12", "warning_cause_mismatch")
    assert cause.expected == "RF-05 application_parties.csv:14; EX-05 application_parties.csv:17"


# --- Non-sample extracts with hand-authored expected outcomes ----------------------------------


def write_extract(directory: Path, borrowers: list[str], applications: list[str],
                  parties: list[str]) -> None:
    directory.mkdir(parents=True)
    for name, lines in ((B, borrowers), (A, applications), (P, parties)):
        header = (SAMPLE_DIR / name).read_text("utf-8").splitlines()[0]
        (directory / name).write_bytes(("\r\n".join([header, *lines]) + "\r\n").encode("utf-8"))
    total = sum((Decimal(line.split(",")[3]) for line in applications), Decimal("0.00"))
    control = [
        "FILE_NAME,RECORD_COUNT,AMOUNT_TOTAL,EXTRACT_DATE",
        f"{B},{len(borrowers)},,20260930",
        f"{A},{len(applications)},{total},20260930",
        f"{P},{len(parties)},,20260930",
    ]
    (directory / "extract_control.csv").write_bytes(("\r\n".join(control) + "\r\n").encode())


# Every line's outcome below is derived by hand from spec sections 8 to 10 and 12.3.
NON_SAMPLE_BORROWERS = [
    "00020001,I,,Ames,Ruth,,A,20260101",           # 2: loaded
    "00020002,B,Birch Supply LLC,,,,A,20260101",   # 3: loaded; WN-01 (only RF-05 and EX-05 lines)
    "00020003,I,,Cole,Ana,,D,20260101",            # 4: EX-01
    "00020004,X,,Dunn,Lee,AB,A,20260101",          # 5: SV-07 CUST_TYPE and SV-08 MIDDLE_INIT (C2)
    "00020005,I,,Eng,Mo,,A,20260101",              # 6: loaded; WN-01, named only on an SV-01 line
    "00020006,I,,Fox,Jo, b ,A,20260101",           # 7: loaded; padded single-letter initial
]
NON_SAMPLE_APPLICATIONS = [
    "0000600001,110,S,1000.00,005000,600,20260101,001",  # 2: loaded (600 months is in range)
    "0000600002,110,S,2000.00,005000,12,20260101,001",   # 3: RF-08 (two valid primaries)
    "0000600003,330,S,3000.00,005000,12,20260101,001",   # 4: EX-02
    "0000600004,110,S,4000.00,005000,12,20260101,001",   # 5: RF-07 and RF-06 (PRI is RF-04)
]
NON_SAMPLE_PARTIES = [
    "0000600001,00020001,PRI",   # 2: loaded
    "0000600001,00020006,GTR",   # 3: loaded
    "0000600002,00020001,PRI",   # 4: RF-05, dependent
    "0000600002,00020002,PRI",   # 5: RF-05, dependent
    "0000600003,00020002,PRI",   # 6: EX-05, dependent
    "0000600004,00020003,PRI",   # 7: RF-04 (deleted customer)
    "0000600001,00020005",       # 8: SV-01 (two fields); no unit under Q9 Option A
]


@pytest.fixture
def non_sample(tmp_path: Path) -> Workspace:
    source = tmp_path / "extract"
    write_extract(source, NON_SAMPLE_BORROWERS, NON_SAMPLE_APPLICATIONS, NON_SAMPLE_PARTIES)
    return Workspace(tmp_path / "work", source)


def report_rows(ws: Workspace, kind: str) -> dict[tuple[str, int], list[dict[str, str]]]:
    rows: dict[tuple[str, int], list[dict[str, str]]] = {}
    text = (ws.evidence() / "reports" / f"{kind}.csv").read_text("utf-8")
    for row in csv.DictReader(io.StringIO(text, newline="")):
        rows.setdefault((row["FILE_NAME"], int(row["LINE_NO"])), []).append(row)
    return rows


def test_non_sample_extract_reconciles_with_hand_authored_outcomes(non_sample: Workspace) -> None:
    non_sample.convert()

    result = non_sample.reconcile()

    assert result.passed, result.discrepancies
    report = non_sample.report()
    assert report["totals"]["rows"] == {
        B: {"read": 6, "loaded": 4, "excluded": 1, "rejected": 1},
        A: {"read": 4, "loaded": 1, "excluded": 1, "rejected": 2},
        P: {"read": 7, "loaded": 2, "excluded": 1, "rejected": 4},
    }
    assert report["totals"]["requested_amount"]["loaded"] == "1000.00"
    assert report["totals"]["requested_amount"]["excluded"] == "3000.00"
    assert report["totals"]["requested_amount"]["rejected"] == "6000.00"
    assert report["eligibility"]["files"][P]["independent"]["rejected_dependent"] == 2
    assert report["eligibility"]["files"][P]["independent"]["excluded_dependent"] == 1
    assert report["eligibility"]["warnings"] == ["00020002", "00020005"]

    exceptions = report_rows(non_sample, "exceptions")
    # C2: an unknown customer type still gets the MIDDLE_INIT check (spec 12.3.2, step 4).
    assert [(r["RULE_CODE"], r["FIELD"], r["SOURCE_VALUE"]) for r in exceptions[(B, 5)]] == [
        ("SV-07", "CUST_TYPE", "X"), ("SV-08", "MIDDLE_INIT", "AB"),
    ]
    rf06 = next(r for r in exceptions[(A, 5)] if r["RULE_CODE"] == "RF-06")
    assert rf06["ROOT_CAUSE"] == "RF-04 application_parties.csv:7"
    assert [r["RULE_CODE"] for r in exceptions[(A, 3)]] == ["RF-08"]
    assert exceptions[(A, 3)][0]["ROOT_CAUSE"] == "RF-08 applications.csv:3"
    warnings = report_rows(non_sample, "warnings")
    assert warnings[(B, 3)][0]["ROOT_CAUSE"] == (
        "RF-05 application_parties.csv:5; EX-05 application_parties.csv:6"
    )
    assert warnings[(B, 6)][0]["ROOT_CAUSE"] == ""


def test_malformed_party_line_under_q9_option_a(non_sample: Workspace) -> None:
    non_sample.convert()

    result = non_sample.reconcile()

    assert result.passed, result.discrepancies
    [row] = report_rows(non_sample, "exceptions")[(P, 8)]
    # Spec 12.3.3 interim rule: SV-01, no key, no unit, never dependent; no application affected.
    assert (row["RULE_CODE"], row["SOURCE_KEY"], row["UNIT_KEY"], row["DEPENDENT"]) == (
        "SV-01", "", "", "N",
    )
    assert (row["FIELD"], row["SOURCE_VALUE"], row["SOURCE_LINE"]) == (
        "", "", "0000600001,00020005",
    )
    assert result.report["relationships"]["0000600001"]["actual"] == [
        ["00020001", "primary_borrower"], ["00020006", "guarantor"],
    ]


def test_malformed_party_attributed_to_a_unit_is_detected(
    non_sample: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A converter that assigns the SV-01 line to application 0000600001's unit contradicts
    # Option A; the line's disposition is unchanged, so RC-12 reports it.
    convert_with(non_sample, monkeypatch,
                 P8=lambda row: dataclasses.replace(row, unit_key="0000600001"))

    result = non_sample.reconcile()

    assert failed(result) == {"RC-12"}
    [unit] = found(result, "RC-12")
    assert (unit.check, unit.line, unit.source_key, unit.expected, unit.actual) == (
        "unit_key_mismatch", 8, "", "", "0000600001",
    )


def test_non_sample_c2_regression_is_a_missing_rule(
    non_sample: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The pre-Phase-1B converter skipped the MIDDLE_INIT check for an unknown type (spec 12.7 C2).
    convert_with(non_sample, monkeypatch, B5=issue_edit(lambda i: None, rule=Rule.SV_08))

    result = non_sample.reconcile()

    assert failed(result) == {"RC-12"}
    [missing] = found(result, "RC-12")
    assert (missing.check, missing.line, missing.expected) == ("missing_rule", 5, "SV-08 MIDDLE_INIT=AB")


# --- Historical runs, refusals, and report versions --------------------------------------------


def test_historical_run_without_row_level_reports_is_refused_without_side_effects(
    ws: Workspace,
) -> None:
    ws.convert()
    manifest = ws.manifest()
    manifest["manifest_version"] = 2
    del manifest["reports"]
    ws.write_manifest(manifest)
    for name in ("exceptions.csv", "exclusions.csv", "warnings.csv"):
        (ws.evidence() / "reports" / name).unlink()
    before = ws.snapshot()

    with pytest.raises(ReconciliationRefusedError) as raised:
        ws.reconcile()

    assert any("predates row-level reports" in p for p in raised.value.problems)
    assert ws.snapshot() == before
    assert ws.manifest()["status"] == "LOADED"
    assert ws.manifest()["reconciliation"] is None
    assert not ws.report_path().exists()


def test_unparseable_record_report_is_refused_without_side_effects(ws: Workspace) -> None:
    ws.convert()
    reseal(ws, "warnings", lambda rows: [[*rows[0], "extra field"], *rows[1:]])
    before = ws.snapshot()

    with pytest.raises(ReconciliationRefusedError) as raised:
        ws.reconcile()

    assert any("warnings.csv cannot be parsed" in p for p in raised.value.problems)
    assert ws.snapshot() == before


def test_changed_record_report_is_refused_without_side_effects(ws: Workspace) -> None:
    ws.convert()
    path = ws.evidence() / "reports" / "exclusions.csv"
    path.write_bytes(path.read_bytes().replace(b"EX-02", b"EX-03", 1))
    before = ws.snapshot()

    with pytest.raises(ReconciliationRefusedError):
        ws.reconcile()

    assert ws.snapshot() == before


def as_version_1(ws: Workspace) -> None:
    """Turn a passed report into the shape a version 1 reconciliation wrote, and re-seal it."""
    report = ws.report()
    report["report_version"] = 1
    report["rules"] = report["rules"][:10]
    del report["eligibility"]
    ws.report_path().write_text(json.dumps(report, indent=2) + "\n", "utf-8")
    manifest = ws.manifest()
    record = manifest["reconciliation"]
    for key in ("report_version", "eligibility"):
        record.pop(key, None)
    if record.get("report_sha256"):
        record["report_sha256"] = hashlib.sha256(ws.report_path().read_bytes()).hexdigest()
    ws.write_manifest(manifest)


def test_version_1_run_is_preserved_verified_and_shown_without_rc11_rc12(ws: Workspace) -> None:
    ws.convert()
    ws.reconcile()
    as_version_1(ws)
    before = ws.snapshot()

    assert reconcile.verify_reconciliation("run-1", evidence_root=ws.evidence_root).verified
    with pytest.raises(ReconciliationRefusedError):
        ws.reconcile()
    assert ws.snapshot() == before

    view = conversions.load_reconciliation(ws.evidence_root, "run-1")
    assert view is not None and view.report_verified
    assert view.report_version == 1
    assert [r.rule for r in view.rules] == ALL_RULES[:10]


def test_version_1_report_claimed_as_version_2_does_not_verify(ws: Workspace) -> None:
    ws.convert()
    ws.reconcile()
    as_version_1(ws)
    manifest = ws.manifest()
    manifest["reconciliation"]["report_version"] = 2
    ws.write_manifest(manifest)

    problems = reconcile.verify_reconciliation("run-1", evidence_root=ws.evidence_root).problems

    assert "The manifest and the report disagree about the report version." in problems


@pytest.mark.parametrize(
    ("report", "record", "problem"),
    [
        ({"report_version": 2, "rules": [{"rule": f"RC-{n:02d}", "result": "PASS"}
                                          for n in range(1, 12)]},
         {"report_version": 2}, "Not every rule RC-01 to RC-12 is recorded as PASS"),
        ({"report_version": 2, "rules": [
            *({"rule": f"RC-{n:02d}", "result": "PASS"} for n in range(1, 12)),
            {"rule": "RC-12", "result": "INCOMPLETE"},
        ]}, {"report_version": 2}, "Not every rule RC-01 to RC-12 is recorded as PASS"),
        ({"report_version": 3, "rules": []}, {}, "unknown report version 3"),
        ({"rules": []}, {}, "unknown report version None"),
        ({"report_version": 1, "rules": [{"rule": f"RC-{n:02d}", "result": "PASS"}
                                          for n in range(1, 11)]},
         {}, None),
    ],
    ids=["v2-without-rc12", "v2-rc12-incomplete", "unknown", "missing", "v1-complete"],
)
def test_reports_verify_against_their_own_version(
    report: dict[str, Any], record: dict[str, Any], problem: str | None
) -> None:
    problems = reconcile.report_version_problems(report, record)

    if problem is None:
        assert problems == []
    else:
        assert any(problem in p for p in problems), problems


def test_provisional_version_1_report_is_a_conflict_on_retry(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws.convert()
    marker_written = False
    real_write = run.write_json_atomic

    def write(path: Path, data: Any) -> None:
        nonlocal marker_written
        if path.name == "manifest.json":
            if marker_written:
                raise OSError(28, "No space left on device (injected)")
            marker_written = True
        real_write(path, data)

    with monkeypatch.context() as patch:
        patch.setattr(run, "write_json_atomic", write)
        with pytest.raises(reconcile.ReconciliationNotFinalizedError):
            ws.reconcile()
    assert ws.manifest()["reconciliation"]["state"] == "in_progress"
    # The interrupted attempt left a report in the version 1 layout.
    report = ws.report()
    report["report_version"] = 1
    report["rules"] = report["rules"][:10]
    del report["eligibility"]
    ws.report_path().write_text(json.dumps(report, indent=2) + "\n", "utf-8")
    provisional = ws.report_path().read_bytes()

    with pytest.raises(reconcile.ReconciliationConflictError) as raised:
        ws.reconcile()

    assert any("written under report version 1" in p for p in raised.value.problems)
    assert ws.report_path().read_bytes() == provisional
    manifest = ws.manifest()
    assert manifest["status"] == "LOADED"
    assert manifest["reconciliation"]["state"] == "conflict"
    assert manifest["release"] is None


def test_retry_of_a_failed_version_2_attempt_reuses_its_report(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(contract, "MIN_TERM_MONTHS", 61)
        ws.convert()

    def fail_finalizing(path: Path, data: Any) -> None:
        if path.name == "manifest.json" and data.get("status") == "FAILED":
            raise OSError(28, "No space left on device (injected)")
        real_write(path, data)

    real_write = run.write_json_atomic
    with monkeypatch.context() as patch:
        patch.setattr(run, "write_json_atomic", fail_finalizing)
        with pytest.raises(reconcile.ReconciliationNotFinalizedError):
            ws.reconcile()
    provisional = ws.report_path().read_bytes()
    assert ws.manifest()["reconciliation"]["state"] == "unfinalized"

    result = ws.reconcile()

    assert result.status is RunStatus.FAILED
    assert ws.report_path().read_bytes() == provisional
    manifest = ws.manifest()
    assert manifest["failure"]["rule"] == "RC-11"
    assert manifest["reconciliation"]["report_version"] == 2
    assert "finalized_by_retry_at" in manifest["reconciliation"]
