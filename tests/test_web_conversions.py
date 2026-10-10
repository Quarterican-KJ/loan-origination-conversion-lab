"""Read-only conversion management pages (/conversions).

Real runs are produced with the actual converter and reconciler; edited copies of them stand in
for incomplete, conflicting, malformed, and hostile evidence.
"""

import dataclasses
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from loan_lab.conversion.legacy import reconcile_run, run_conversion
from loan_lab.conversion.legacy import run as runs
from loan_lab.conversion.legacy.contract import Rule
from loan_lab.conversion.legacy.plan import Cause, Disposition, Issue
from loan_lab.main import create_app
from loan_lab.paths import find_project_root
from loan_lab.web import conversions, formatting

PROJECT_ROOT = find_project_root(Path(__file__))
SAMPLE_DIR = PROJECT_ROOT / "sample_data" / "legacy"
HOSTILE = '<script>alert("run")</script> & <img src=x onerror=alert(1)>'
SECRET = "SECRET-OUTSIDE-EVIDENCE-ROOT"
NOT_AVAILABLE = "Not available"


@dataclass(frozen=True)
class Lab:
    root: Path
    evidence: Path
    databases: Path


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", "utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text("utf-8"))


def edit(lab: Lab, run_id: str, name: str, change: Callable[[dict[str, Any]], None]) -> None:
    path = lab.evidence / run_id / name
    data = _read_json(path)
    change(data)
    _write_json(path, data)


def clone(lab: Lab, source: str, target: str) -> None:
    """Copy a run's evidence under a new ID, keeping it internally consistent."""
    shutil.copytree(lab.evidence / source, lab.evidence / target)
    directory = lab.evidence / target
    for name in ("reports/load_result.json", "reports/reconciliation.json"):
        if (directory / name).exists():
            edit(lab, target, name, lambda data: data.update(run_id=target))

    def manifest(data: dict[str, Any]) -> None:
        data["run_id"] = target
        report = directory / "reports" / "reconciliation.json"
        if isinstance(data.get("reconciliation"), dict) and report.exists():
            data["reconciliation"]["report_sha256"] = hashlib.sha256(
                report.read_bytes()
            ).hexdigest()

    edit(lab, target, "manifest.json", manifest)


def _convert(lab: Lab, source: Path, run_id: str) -> None:
    run_conversion(
        source, run_id, conversion_root=lab.databases, evidence_root=lab.evidence, batch_size=4
    )


@pytest.fixture(scope="module")
def lab(tmp_path_factory: pytest.TempPathFactory) -> Lab:
    root = tmp_path_factory.mktemp("conversions")
    lab = Lab(root, root / "output" / "conversion", root / "data" / "conversion")
    source = root / "source"
    shutil.copytree(SAMPLE_DIR, source)

    _convert(lab, source, "run-reconciled")
    reconcile_run("run-reconciled", evidence_root=lab.evidence)
    _convert(lab, source, "run-loaded")

    real_load = runs.load_plan

    def corrupted_load(*args: Any, **kwargs: Any) -> Any:
        result = real_load(*args, **kwargs)
        with closing(sqlite3.connect(result.database_path)) as conn:
            conn.execute(
                "UPDATE loan_application SET interest_rate = 66000 "
                "WHERE source_system_id = '0000500101'"
            )
            conn.commit()
        return result

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runs, "load_plan", corrupted_load)
        _convert(lab, source, "run-mismatch")
    reconcile_run("run-mismatch", evidence_root=lab.evidence)

    # The converter records borrowers.csv:12 (SV-02) as an EX-01 exclusion: RC-11 fails, and
    # RC-12 leaves that line's row details unexamined (INCOMPLETE).
    real_plan = runs.plan_conversion

    def excluded_b12(directory: Path) -> Any:
        plan = real_plan(directory)

        def fix(row: Any) -> Any:
            if (row.ref.file, row.ref.line) != ("borrowers.csv", 12):
                return row
            return dataclasses.replace(row, disposition=Disposition.EXCLUDED, issues=(
                Issue(Rule.EX_01, "Excluded (injected).", "RECORD_STATUS", "A",
                      (Cause(Rule.EX_01, row.ref),)),
            ))

        return dataclasses.replace(plan, borrowers=tuple(fix(r) for r in plan.borrowers))

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runs, "plan_conversion", excluded_b12)
        _convert(lab, source, "run-rc12-incomplete")
    reconcile_run("run-rc12-incomplete", evidence_root=lab.evidence)

    # A run reconciled under report version 1, before RC-11 and RC-12 existed.
    clone(lab, "run-reconciled", "run-v1")

    def version_1(report: dict[str, Any]) -> None:
        report.update(report_version=1, rules=report["rules"][:10])
        for item in report["rules"]:
            item.pop("complete", None)
        del report["eligibility"]

    def version_1_record(manifest: dict[str, Any]) -> None:
        record = manifest["reconciliation"]
        for key in ("report_version", "eligibility", "rules_incomplete"):
            del record[key]
        record["report_sha256"] = hashlib.sha256(
            (lab.evidence / "run-v1" / "reports" / "reconciliation.json").read_bytes()
        ).hexdigest()

    edit(lab, "run-v1", "reports/reconciliation.json", version_1)
    edit(lab, "run-v1", "manifest.json", version_1_record)

    # A LOADED run converted before row-level record reports existed (manifest version 2).
    def before_record_reports(manifest: dict[str, Any]) -> None:
        manifest["manifest_version"] = 2
        del manifest["reports"]

    clone(lab, "run-loaded", "run-legacy-loaded")
    edit(lab, "run-legacy-loaded", "manifest.json", before_record_reports)
    for name in ("exceptions.csv", "exclusions.csv", "warnings.csv"):
        (lab.evidence / "run-legacy-loaded" / "reports" / name).unlink()

    invalid = root / "invalid-source"
    shutil.copytree(SAMPLE_DIR, invalid)
    (invalid / "borrowers.csv").unlink()
    with pytest.raises(runs.SourceRunFailedError):
        _convert(lab, invalid, "run-invalid")

    clone(lab, "run-loaded", "run-incomplete")
    edit(lab, "run-incomplete", "manifest.json", lambda m: m.update(
        status="LOADING", ready_for_reconciliation=False,
        evidence={"state": "incomplete", "error": "OSError: disk full", "recovered_at": None},
    ))
    clone(lab, "run-loaded", "run-unknown")
    edit(lab, "run-unknown", "manifest.json", lambda m: m.update(
        status="UNKNOWN", database_transaction="unknown", ready_for_reconciliation=False,
        evidence={"state": "unverified", "error": None, "recovered_at": None},
    ))
    clone(lab, "run-reconciled", "run-unfinalized")
    edit(lab, "run-unfinalized", "manifest.json", lambda m: m.update(
        status="LOADED", ready_for_reconciliation=False, failure=None,
        reconciliation={**m["reconciliation"], "state": "unfinalized", "error": "OSError"},
    ))
    clone(lab, "run-reconciled", "run-tampered-report")
    edit(lab, "run-tampered-report", "reports/reconciliation.json",
         lambda r: r["totals"]["requested_amount"].update(loaded="9999999.00"))
    clone(lab, "run-reconciled", "run-decision-recorded")
    edit(lab, "run-decision-recorded", "manifest.json", lambda m: m.update(
        release={"decision": "RELEASED", "by": "nobody"}
    ))
    clone(lab, "run-reconciled", "run-no-report")
    (lab.evidence / "run-no-report" / "reports" / "reconciliation.json").unlink()
    clone(lab, "run-loaded", "run-load-mismatch")
    edit(lab, "run-load-mismatch", "reports/load_result.json",
         lambda r: r["loaded"].update(applications=7))
    clone(lab, "run-reconciled", "run-source-changed")
    archived = lab.evidence / "run-source-changed" / "source" / "applications.csv"
    archived.write_bytes(archived.read_bytes().replace(b"006500", b"006600", 1))

    clone(lab, "run-loaded", "run-odd-times")
    edit(lab, "run-odd-times", "manifest.json", lambda m: m.update(
        started_at="not a date", updated_at="2026-10-09T19:47:32+05:00", finished_at=None,
        evidence={**m["evidence"], "recovered_at": "2026-13-40T25:00:00Z"},
    ))

    (lab.evidence / "run-garbage").mkdir()
    (lab.evidence / "run-garbage" / "manifest.json").write_bytes(b'{"status": "LOADED",')
    (lab.evidence / "run-array").mkdir()
    (lab.evidence / "run-array" / "manifest.json").write_bytes(b'["LOADED"]')
    (lab.evidence / "run-nan").mkdir()
    (lab.evidence / "run-nan" / "manifest.json").write_bytes(b'{"status": NaN}')
    (lab.evidence / "run-binary").mkdir()
    (lab.evidence / "run-binary" / "manifest.json").write_bytes(b"\xff\xfe\x00garbage")
    (lab.evidence / "run-empty").mkdir()

    clone(lab, "run-mismatch", "run-hostile")
    edit(lab, "run-hostile", "manifest.json", lambda m: m.update(
        status="FAILED",
        failure={"stage": "reconciliation", "step": HOSTILE, "rule": "RC-07", "reason": HOSTILE},
        validation={**m["validation"], "customers_without_applications": [HOSTILE]},
    ))
    edit(lab, "run-hostile", "reports/reconciliation.json", lambda r: r["discrepancies"].append({
        "rule": "RC-07", "check": "field_mismatch", "file": "applications.csv", "line": 2,
        "source_key": HOSTILE, "target_table": "loan_application", "target_id": 1,
        "field": "interest_rate", "expected": HOSTILE, "actual": "</td></tr><script>x</script>",
        "message": HOSTILE,
    }))
    edit(lab, "run-hostile", "manifest.json", lambda m: m["reconciliation"].update(
        report_sha256=hashlib.sha256(
            (lab.evidence / "run-hostile" / "reports" / "reconciliation.json").read_bytes()
        ).hexdigest()
    ))

    # Not run directories: a stray file and a name that is not a valid run ID.
    (lab.evidence / "notes.txt").write_text("not a run", "utf-8")
    (lab.evidence / "bad.name").mkdir()

    outside = root / "output" / "secret-run"
    outside.mkdir()
    _write_json(outside / "manifest.json", {"run_id": "secret-run", "status": SECRET})
    return lab


@pytest.fixture(scope="module")
def client(lab: Lab) -> Iterator[TestClient]:
    app = create_app(database_path=lab.root / "no-los.db", evidence_root=lab.evidence)
    with TestClient(app) as test_client:
        yield test_client


def row(html: str, run_id: str) -> str:
    match = re.search(rf'<tr data-run-id="{re.escape(run_id)}">(.*?)</tr>', html, re.DOTALL)
    assert match, run_id
    return match.group(1)


def cell(html: str, name: str) -> str:
    match = re.search(rf'data-field="{name}"[^>]*>(.*?)</(?:td|dd|strong)>', html, re.DOTALL)
    assert match, name
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", match.group(1))).strip()


def condition(html: str) -> str:
    match = re.search(r'data-condition="(\w+)"', html)
    assert match
    return match.group(1)


def rule_results(html: str) -> dict[str, str]:
    return dict(re.findall(r'<tr data-rule="(RC-\d\d)">.*?data-result="([^"]+)"', html, re.DOTALL))


def snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in root.rglob("*") if p.is_file()
    }


# --- List --------------------------------------------------------------------------------------


def test_list_shows_runs_with_counts_volume_and_outcome(client: TestClient) -> None:
    response = client.get("/conversions")

    assert response.status_code == 200
    html = response.text
    reconciled = row(html, "run-reconciled")
    assert 'data-run-status="RECONCILED"' in reconciled
    assert 'data-condition="reconciled"' in reconciled
    assert "Reconciled · awaiting release approval" in reconciled
    assert cell(reconciled, "source-rows") == "16 / 13 / 20"
    assert cell(reconciled, "target-rows") == "11 / 6 / 10"
    assert cell(reconciled, "loaded-volume") == "$2,281,000.00"
    assert cell(reconciled, "reconciliation") == "Passed"
    loaded = row(html, "run-loaded")
    assert 'data-condition="loaded"' in loaded
    assert cell(loaded, "reconciliation") == "Not run"
    assert 'data-condition="failed"' in row(html, "run-mismatch")
    assert cell(row(html, "run-mismatch"), "reconciliation") == "Failed"


def test_list_never_labels_a_run_released(client: TestClient) -> None:
    html = client.get("/conversions").text

    assert "Released" not in html
    assert "released" not in re.sub(r"(not|never|been) released|releases", "", html)
    assert 'data-condition="untrusted"' in row(html, "run-decision-recorded")


def test_list_marks_missing_evidence_not_available(client: TestClient) -> None:
    html = client.get("/conversions").text

    invalid = row(html, "run-invalid")
    assert 'data-condition="failed"' in invalid
    assert cell(invalid, "target-rows") == NOT_AVAILABLE
    assert cell(invalid, "loaded-volume") == NOT_AVAILABLE
    garbage = row(html, "run-garbage")
    assert 'data-condition="unavailable"' in garbage
    assert "$0.00" not in garbage and ">0<" not in garbage


def test_list_ignores_entries_that_are_not_run_directories(client: TestClient) -> None:
    html = client.get("/conversions").text

    assert 'data-skipped="2"' in html
    assert "bad.name" not in html
    assert "notes.txt" not in html
    assert SECRET not in html


def test_list_scan_is_bounded(
    client: TestClient, lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conversions, "MAX_LISTED_RUNS", 3)

    html = client.get("/conversions").text

    assert len(re.findall(r"<tr data-run-id=", html)) == 3
    assert "data-truncated" in html


def test_missing_evidence_root_is_reported_and_not_created(tmp_path: Path) -> None:
    missing = tmp_path / "output" / "conversion"
    app = create_app(database_path=tmp_path / "no.db", evidence_root=missing)

    with TestClient(app) as test_client:
        response = test_client.get("/conversions")

    assert response.status_code == 200
    assert 'data-evidence-root="missing"' in response.text
    assert "python -m loan_lab.conversion.legacy" in response.text
    assert not missing.exists()
    assert not (tmp_path / "output").exists()


# --- Run summary ---------------------------------------------------------------------------------


def test_reconciled_run_summary(client: TestClient) -> None:
    response = client.get("/conversions/run-reconciled")

    assert response.status_code == 200
    html = response.text
    assert condition(html) == "reconciled"
    assert 'data-trust="reconciled"' in html
    assert "has <strong>not</strong> been released" in html
    assert cell(html, "release") == "Not released"
    assert cell(html, "evidence-state") == "Complete"
    assert cell(html, "transaction") == "Committed"
    assert cell(html, "control-amount") == "$5,467,000.00"
    assert cell(html, "amount-eligible") == "$2,281,000.00"
    assert cell(html, "amount-excluded") == "$291,000.00"
    assert cell(html, "amount-rejected") == "$2,895,000.00"
    assert cell(html, "wn01") == "00010008 , 00010009 , 00010015"
    assert cell(html, "rejections") == "18"
    assert cell(html, "reconciliation-outcome") == "Passed"
    assert html.count('<span class="badge result-pass">Matches</span>') == 4
    dispositions = re.search(r'data-file="applications.csv">(.*?)</tr>', html, re.DOTALL).group(1)
    assert cell(dispositions, "read") == "13"
    assert cell(dispositions, "eligible") == "6"
    assert cell(dispositions, "excluded") == "2"
    assert cell(dispositions, "rejected") == "5"


def test_loaded_run_is_not_reconciled(client: TestClient) -> None:
    html = client.get("/conversions/run-loaded").text

    assert condition(html) == "loaded"
    assert "Loaded · not reconciled" in html
    assert cell(html, "reconciliation-outcome") == "Not run"
    assert cell(html, "ready") == "Yes"


def test_validation_failure_shows_issues_and_no_target(client: TestClient) -> None:
    html = client.get("/conversions/run-invalid").text

    assert condition(html) == "failed"
    assert cell(html, "failure-stage") == "Validation"
    assert cell(html, "run-level-checks") == "Failed"
    assert "RUN-01" in html
    target = re.search(r'data-section="target">(.*?)</table>', html, re.DOTALL).group(1)
    assert target.count(NOT_AVAILABLE) == 8
    assert "$0.00" not in target


@pytest.mark.parametrize(
    ("run_id", "expected", "problem"),
    [
        ("run-incomplete", "untrusted", "The evidence is not complete (state: incomplete)."),
        ("run-unknown", "unknown", None),
        ("run-unfinalized", "untrusted", "A reconciliation attempt is unfinalized"),
        ("run-tampered-report", "untrusted", "does not match the checksum in the manifest"),
        ("run-decision-recorded", "untrusted", "records a release decision"),
        ("run-no-report", "untrusted", "reports/reconciliation.json: Not available: missing."),
        ("run-load-mismatch", "untrusted", "do not match the expected target"),
        ("run-source-changed", "untrusted", "archived applications.csv does not match"),
    ],
)
def test_untrusted_evidence_is_never_presented_as_success(
    client: TestClient, run_id: str, expected: str, problem: str | None
) -> None:
    html = client.get(f"/conversions/{run_id}").text

    assert condition(html) == expected
    assert 'data-trust="reconciled"' not in html
    assert "Reconciled · awaiting release approval" not in html
    if problem:
        assert 'data-trust="untrusted"' in html
        assert problem in html


def test_recorded_status_is_shown_separately_from_trust(client: TestClient) -> None:
    html = client.get("/conversions/run-tampered-report").text

    assert 'data-run-status="RECONCILED"' in html
    assert condition(html) == "untrusted"
    assert cell(html, "reconciliation-outcome") == "Unverified: the report does not verify"


def test_source_change_is_detected_by_rehashing_the_archive(client: TestClient) -> None:
    html = client.get("/conversions/run-source-changed").text

    changed = re.search(r'data-source-file="applications.csv">(.*?)</tr>', html, re.DOTALL)
    assert "Differs" in cell(changed.group(1), "archive-check")


@pytest.mark.parametrize(
    ("run_id", "state"),
    [
        ("run-garbage", "malformed"),
        ("run-array", "malformed"),
        ("run-nan", "malformed"),
        ("run-binary", "malformed"),
        ("run-empty", "missing"),
    ],
)
def test_unreadable_manifest_is_reported_not_guessed(
    client: TestClient, run_id: str, state: str
) -> None:
    response = client.get(f"/conversions/{run_id}")

    assert response.status_code == 200
    html = response.text
    assert condition(html) == "unavailable"
    assert re.search(rf'data-evidence-file="manifest.json" data-state="{state}"', html)
    assert "Traceback" not in html
    assert "$0.00" not in html
    recon = client.get(f"/conversions/{run_id}/reconciliation").text
    assert set(rule_results(recon).values()) <= set()
    assert "PASS" not in recon


def test_oversized_evidence_is_not_read(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conversions, "MAX_MANIFEST_BYTES", 100)

    html = client.get("/conversions/run-reconciled").text

    assert condition(html) == "unavailable"
    assert re.search(r'data-evidence-file="manifest.json" data-state="too_large"', html)
    assert "the limit is 100" in html


def test_oversized_report_is_not_available(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conversions, "MAX_RECONCILIATION_REPORT_BYTES", 100)

    html = client.get("/conversions/run-reconciled/reconciliation").text

    assert 'data-report="unavailable"' in html
    assert "too large" in html
    assert "PASS" not in html


# --- Reconciliation page ---------------------------------------------------------------------


def test_reconciliation_page_lists_every_rule_as_pass(client: TestClient) -> None:
    response = client.get("/conversions/run-reconciled/reconciliation")

    assert response.status_code == 200
    html = response.text
    assert rule_results(html) == {f"RC-{n:02d}": "PASS" for n in range(1, 13)}
    assert 'data-report="pass"' in html
    assert "All rules RC-01 to RC-12 passed" in html
    assert 'data-report-version="1"' not in html
    assert "No discrepancies recorded." in html
    assert cell(html, "source-total") == "$5,467,000.00"
    assert cell(html, "loaded-total") == "$2,281,000.00"
    assert cell(html, "target-total") == "$2,281,000.00"


def test_reconciliation_page_shows_field_level_discrepancies(client: TestClient) -> None:
    html = client.get("/conversions/run-mismatch/reconciliation").text

    results = rule_results(html)
    assert results["RC-07"] == "FAIL"
    assert results["RC-01"] == "PASS"
    assert 'data-report="fail"' in html
    section = re.search(r'data-discrepancies="RC-07">(.*?)</article>', html, re.DOTALL).group(1)
    assert "<code>applications.csv:2</code>" in section
    assert "<code>0000500101</code>" in section
    assert "<code>interest_rate</code>" in section
    assert cell(section, "expected") == "6.5000"
    assert cell(section, "actual") == "6.6000"


def not_performed_reason(html: str) -> str:
    match = re.search(r'data-field="not-performed-reason">(.*?)</span>', html, re.DOTALL)
    assert match
    return match.group(1)


def assert_no_rule_results(html: str) -> None:
    """No rule is presented as evaluated: no rules table, rows, results, or pass notice."""
    assert 'id="rules"' not in html
    assert '<tr data-rule="' not in html
    assert "data-result=" not in html
    assert 'data-report="pass"' not in html
    assert "RC-01" not in html


def test_reconciliation_page_before_reconciliation(client: TestClient) -> None:
    html = client.get("/conversions/run-loaded/reconciliation").text

    assert 'data-report="not-performed"' in html
    assert "Reconciliation not yet performed." in html
    assert 'data-report="unavailable"' not in html
    assert cell(html, "reconciliation-outcome") == "Not run"
    assert not_performed_reason(html) == (
        "The run is loaded and ready. An operator reconciles it from the command line "
        "(python -m loan_lab.conversion.legacy.reconcile_cli run-loaded); this page never "
        "starts one."
    )
    assert_no_rule_results(html)
    assert 'data-report-version="1"' not in html


def test_reconciliation_page_for_a_run_that_failed_before_reconciliation(
    client: TestClient,
) -> None:
    html = client.get("/conversions/run-invalid/reconciliation").text

    assert 'data-report="not-performed"' in html
    assert not_performed_reason(html).startswith("The run failed at stage ")
    assert "so it is never reconciled" in not_performed_reason(html)
    assert_no_rule_results(html)


def test_reconciliation_page_for_a_loaded_run_without_record_reports(client: TestClient) -> None:
    html = client.get("/conversions/run-legacy-loaded/reconciliation").text

    assert 'data-report="not-performed"' in html
    assert "predates row-level record reports (manifest version 2)" in not_performed_reason(html)
    assert "would be refused" in not_performed_reason(html)
    assert_no_rule_results(html)


@pytest.mark.parametrize("run_id", ["run-no-report", "run-incomplete", "run-unknown"])
def test_missing_report_is_not_called_not_performed_when_it_should_exist(
    client: TestClient, run_id: str
) -> None:
    html = client.get(f"/conversions/{run_id}/reconciliation").text

    if run_id == "run-no-report":
        # The manifest records a final reconciliation whose report is gone: evidence problem.
        assert 'data-report="unavailable"' in html
        assert 'data-report="not-performed"' not in html
    else:
        # Not loaded (LOADING, UNKNOWN): not yet performed, and the status says why.
        assert 'data-report="not-performed"' in html
        assert "Only a LOADED run can be reconciled." in not_performed_reason(html)
    assert_no_rule_results(html)


def test_reconciliation_page_for_a_version_1_report(client: TestClient) -> None:
    html = client.get("/conversions/run-v1/reconciliation").text

    assert rule_results(html) == {f"RC-{n:02d}": "PASS" for n in range(1, 11)}
    assert 'data-rule="RC-11"' not in html and 'data-rule="RC-12"' not in html
    assert 'data-report="pass"' in html
    assert "All rules RC-01 to RC-10 passed" in html
    assert 'data-report-version="1"' in html
    assert "reconciled before independent disposition verification" in html
    assert '<span data-field="report-version">1</span>' in html
    assert "data-unexamined=" not in html


def test_reconciliation_page_for_a_version_2_report_with_incomplete_rc12(
    client: TestClient,
) -> None:
    html = client.get("/conversions/run-rc12-incomplete/reconciliation").text

    results = rule_results(html)
    assert list(results) == [f"RC-{n:02d}" for n in range(1, 13)]
    assert results["RC-11"] == "FAIL"
    assert results["RC-12"] == "INCOMPLETE"
    assert 'data-report="fail"' in html
    assert 'data-report-version="1"' not in html
    notice = re.search(r'data-unexamined="RC-12">(.*?)</div>', html, re.DOTALL).group(1)
    assert '<span data-field="unexamined-lines">1</span>' in notice
    assert "unexamined, not verified" in notice
    rc12 = re.search(r'<tr data-rule="RC-12">(.*?)</tr>', html, re.DOTALL).group(1)
    assert "Incomplete: exception and exclusion row details of 1 lines" in rc12
    summary = client.get("/conversions/run-rc12-incomplete").text
    assert cell(summary, "rules-incomplete") == "RC-12"


@pytest.mark.parametrize("run_id", ["run-tampered-report", "run-unfinalized"])
def test_unverified_report_results_are_marked_unverified(client: TestClient, run_id: str) -> None:
    html = client.get(f"/conversions/{run_id}/reconciliation").text

    assert 'data-report="unverified"' in html
    assert set(rule_results(html).values()) == {"PASS-unverified"}
    assert 'data-report="pass"' not in html


# --- Escaping --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url", ["/conversions", "/conversions/run-hostile", "/conversions/run-hostile/reconciliation"]
)
def test_evidence_strings_are_escaped(client: TestClient, url: str) -> None:
    html = client.get(url).text

    assert "<script>" not in html
    assert "<img src=x" not in html
    assert "</td></tr><script>" not in html


def test_hostile_values_are_shown_as_text(client: TestClient) -> None:
    detail = client.get("/conversions/run-hostile").text
    recon = client.get("/conversions/run-hostile/reconciliation").text
    escaped = "&lt;script&gt;alert(&#34;run&#34;)&lt;/script&gt; &amp; &lt;img src=x onerror=alert(1)&gt;"

    assert escaped in cell(detail, "failure-reason") or escaped in detail
    assert escaped in recon
    assert "&lt;/td&gt;&lt;/tr&gt;&lt;script&gt;x&lt;/script&gt;" in recon


# --- Run ID validation and containment ---------------------------------------------------------


@pytest.mark.parametrize(
    "run_id",
    ["%2e%2e", ".hidden", "-run", "a" * 65, "run.1", "run%20one", "run%00", "CON", "nul",
     "lpt1", "run%5C..%5Csecret-run", "%E2%80%AEevil"],
)
def test_invalid_run_ids_are_rejected(client: TestClient, run_id: str) -> None:
    for suffix in ("", "/reconciliation"):
        response = client.get(f"/conversions/{run_id}{suffix}")

        assert response.status_code in (400, 404), run_id
        assert SECRET not in response.text
        assert "Traceback" not in response.text


def test_invalid_run_id_message(client: TestClient) -> None:
    response = client.get("/conversions/run.1")

    assert response.status_code == 400
    assert "Invalid run ID" in response.text


@pytest.mark.parametrize(
    "path", ["/conversions/..%2Fsecret-run", "/conversions/%2e%2e/secret-run",
             "/conversions/../secret-run", "/conversions/run-loaded/../../secret-run"],
)
def test_paths_cannot_escape_the_evidence_root(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert SECRET not in response.text


def test_unknown_run_returns_404(client: TestClient) -> None:
    for url in ("/conversions/no-such-run", "/conversions/no-such-run/reconciliation"):
        response = client.get(url)
        assert response.status_code == 404
        assert "Conversion run not found" in response.text


def test_symlinked_run_directory_is_not_followed(lab: Lab, client: TestClient) -> None:
    link = lab.evidence / "run-link"
    try:
        os.symlink(lab.root / "output" / "secret-run", link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symbolic links are not available to this user.")
    try:
        assert client.get("/conversions/run-link").status_code == 404
        assert SECRET not in client.get("/conversions").text
    finally:
        link.unlink()


@pytest.mark.skipif(sys.platform != "win32", reason="Directory junctions are Windows-only.")
def test_junctioned_run_directory_is_not_followed(lab: Lab, client: TestClient) -> None:
    import _winapi

    link = lab.evidence / "run-junction"
    _winapi.CreateJunction(str(lab.root / "output" / "secret-run"), str(link))
    try:
        assert client.get("/conversions/run-junction").status_code == 404
        assert client.get("/conversions/run-junction/reconciliation").status_code == 404
        assert SECRET not in client.get("/conversions").text
    finally:
        os.rmdir(link)
    assert (lab.root / "output" / "secret-run" / "manifest.json").exists()


def test_symlinked_evidence_file_is_not_followed(lab: Lab, client: TestClient) -> None:
    run_dir = lab.evidence / "run-file-link"
    run_dir.mkdir()
    try:
        try:
            os.symlink(lab.root / "output" / "secret-run" / "manifest.json",
                       run_dir / "manifest.json")
        except (OSError, NotImplementedError):
            pytest.skip("Symbolic links are not available to this user.")
        html = client.get("/conversions/run-file-link").text
        assert SECRET not in html
        assert 'data-state="unsafe"' in html
    finally:
        shutil.rmtree(run_dir)


def test_run_directory_requires_the_exact_name(lab: Lab) -> None:
    assert conversions.run_directory(lab.evidence, "run-loaded") is not None
    assert conversions.run_directory(lab.evidence, "RUN-LOADED") is None
    with pytest.raises(conversions.InvalidRunId):
        conversions.run_directory(lab.evidence, "../secret-run")


# --- Read-only behavior ------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize(
    "url", ["/conversions", "/conversions/run-loaded", "/conversions/run-loaded/reconciliation"]
)
def test_write_methods_are_not_allowed(client: TestClient, method: str, url: str) -> None:
    response = client.request(method, url)

    assert response.status_code == 405
    assert response.headers["allow"] == "GET"


def test_browsing_changes_no_evidence_or_database(lab: Lab, client: TestClient) -> None:
    dev_database = PROJECT_ROOT / "data" / "loan_lab_dev.db"
    dev_before = dev_database.read_bytes() if dev_database.exists() else None
    before = snapshot(lab.root)

    client.get("/conversions")
    for run_dir in sorted(lab.evidence.iterdir()):
        if run_dir.is_dir():
            client.get(f"/conversions/{run_dir.name}")
            client.get(f"/conversions/{run_dir.name}/reconciliation")
            client.post(f"/conversions/{run_dir.name}")

    assert snapshot(lab.root) == before
    assert (dev_database.read_bytes() if dev_database.exists() else None) == dev_before
    assert not (lab.root / "no-los.db").exists()


def test_conversion_pages_never_open_a_database(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("A database was opened.")

    monkeypatch.setattr(sqlite3, "connect", refuse)

    for url in ("/conversions", "/conversions/run-reconciled",
                "/conversions/run-reconciled/reconciliation", "/conversions/run-mismatch"):
        assert client.get(url).status_code == 200


def test_pages_offer_no_actions(client: TestClient) -> None:
    for url in ("/conversions", "/conversions/run-reconciled",
                "/conversions/run-reconciled/reconciliation"):
        html = client.get(url).text
        forms = re.findall(r"<form[^>]*>", html)
        assert all('role="search"' in form for form in forms)
        controls = re.findall(r"(?is)<(?:a|button)\b[^>]*>(.*?)</(?:a|button)>", html)
        actions = r"(?i)approve|release|decline|upload|promote|execute|start|run conversion|rerun"
        assert controls
        assert not [label for label in controls if re.search(actions, label)]


def test_navigation_links_to_conversions(client: TestClient) -> None:
    html = client.get("/conversions").text

    assert re.search(r'href="http://testserver/conversions"\s+aria-current="page"', html)
    assert 'data-theme-toggle' in html


# --- Display formatting --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        ("2026-10-09T19:47:32.291411Z", "Oct 9, 2026 · 7:47 PM UTC"),
        ("2026-01-05T00:05:00+00:00", "Jan 5, 2026 · 12:05 AM UTC"),
        ("2026-06-30T12:00:59Z", "Jun 30, 2026 · 12:00 PM UTC"),
        ("2026-12-31T23:59:00.5Z", "Dec 31, 2026 · 11:59 PM UTC"),
    ],
)
def test_utc_timestamps_are_formatted(value: str, shown: str) -> None:
    assert formatting.utc_timestamp(value) == shown


@pytest.mark.parametrize(
    "value",
    [None, "", "not a date", "2026-10-09", "2026-10-09T19:47:32", "2026-10-09 19:47:32Z",
     "2026-10-09T19:47:32+05:00", "2026-10-09T19:47:32-00:00", "2026-13-40T25:00:00Z",
     " 2026-10-09T19:47:32Z", "2026-10-09T19:47:32Z<script>", "２０２６-10-09T19:47:32Z",
     1760039252, Decimal("1760039252")],
)
def test_invalid_or_non_utc_timestamps_are_not_formatted(value: object) -> None:
    assert formatting.utc_timestamp(value) is None


def time_element(html: str, field: str) -> tuple[str, str]:
    match = re.search(
        rf'data-field="{field}"[^>]*>\s*<time class="timestamp" datetime="([^"]+)"[^>]*>([^<]+)</time>',
        html,
    )
    assert match, field
    return match.group(1), match.group(2)


def test_timestamps_show_formatted_and_keep_the_recorded_value(lab: Lab, client: TestClient) -> None:
    manifest = _read_json(lab.evidence / "run-reconciled" / "manifest.json")
    report = _read_json(lab.evidence / "run-reconciled" / "reports" / "reconciliation.json")
    pages = {
        "list": row(client.get("/conversions").text, "run-reconciled"),
        "summary": client.get("/conversions/run-reconciled").text,
        "reconciliation": client.get("/conversions/run-reconciled/reconciliation").text,
    }
    expected = [
        ("list", "started", manifest["started_at"]),
        ("list", "finished", manifest["finished_at"]),
        ("summary", "started", manifest["started_at"]),
        ("summary", "updated", manifest["updated_at"]),
        ("summary", "finished", manifest["finished_at"]),
        ("summary", "reconciled-at", manifest["reconciliation"]["reconciled_at"]),
        ("reconciliation", "generated-at", report["generated_at"]),
    ]
    for page, field, recorded in expected:
        assert time_element(pages[page], field) == (recorded, formatting.utc_timestamp(recorded))
    assert "(UTC)" not in pages["summary"]


def test_missing_and_malformed_timestamps_show_not_available(client: TestClient) -> None:
    html = client.get("/conversions/run-odd-times").text

    for field in ("started", "updated", "finished", "recovered"):
        assert cell(html, field) == NOT_AVAILABLE, field
    assert html.count('title="The recorded value is not a valid UTC timestamp."') == 3
    assert "not a date" not in html
    listed = row(client.get("/conversions").text, "run-odd-times")
    assert cell(listed, "started") == NOT_AVAILABLE
    assert cell(listed, "finished") == NOT_AVAILABLE
    assert "<time" not in listed


def test_run_list_states_the_count_order(client: TestClient) -> None:
    html = client.get("/conversions").text
    head = re.search(r"<thead>(.*?)</thead>", html, re.DOTALL).group(1)

    notes = re.findall(r'<span class="th-note" data-order>([^<]+)</span>', head)
    assert notes == ["Borrowers / Applications / Parties"] * 2
    assert head.index("Source rows") < head.index("Target rows") < head.index("Loaded volume")


TERM_DEFINITIONS = {
    "/conversions": {"read-back"},
    "/conversions/run-reconciled": {"eligible", "excluded", "rejected", "expected", "read-back"},
    "/conversions/run-reconciled/reconciliation": {"checked", "loaded", "excluded", "rejected"},
}


@pytest.mark.parametrize("url", sorted(TERM_DEFINITIONS))
def test_terms_are_explained_accessibly(client: TestClient, url: str) -> None:
    html = client.get(url).text

    described = set(re.findall(r'class="term" title="[^"]+" aria-describedby="term-([\w-]+)"', html))
    defined = re.findall(r'<dd id="term-([\w-]+)">', html)
    assert described == TERM_DEFINITIONS[url]
    assert sorted(defined) == sorted(set(defined)) == sorted(described)
    for term_id in described:
        title = re.search(rf'title="([^"]+)" aria-describedby="term-{term_id}"', html).group(1)
        definition = re.search(rf'<dd id="term-{term_id}">([^<]+)</dd>', html).group(1)
        assert title == definition


def test_items_checked_is_not_a_record_count(client: TestClient) -> None:
    html = client.get("/conversions/run-reconciled/reconciliation").text
    definition = re.search(r'<dd id="term-checked">([^<]+)</dd>', html).group(1)

    assert "Comparisons made by that rule" in definition
    assert "not necessarily a count of distinct source records" in definition
    header = re.search(r"<thead>(.*?)</thead>", html, re.DOTALL).group(1)
    assert 'aria-describedby="term-checked">Items checked</span>' in header


def test_verified_reconciled_run_reads_already_reconciled(lab: Lab, client: TestClient) -> None:
    recorded = _read_json(lab.evidence / "run-reconciled" / "manifest.json")
    html = client.get("/conversions/run-reconciled").text

    assert cell(html, "ready") == "Already reconciled"
    assert f'data-recorded="{str(recorded["ready_for_reconciliation"]).lower()}"' in html
    assert "<dt>Ready for reconciliation</dt>" not in html
    assert _read_json(lab.evidence / "run-reconciled" / "manifest.json") == recorded


@pytest.mark.parametrize(
    ("run_id", "ready"),
    [("run-tampered-report", "No"), ("run-source-changed", "No"), ("run-incomplete", "No"),
     ("run-unfinalized", "No"), ("run-mismatch", "No"), ("run-loaded", "Yes")],
)
def test_unverified_runs_do_not_read_already_reconciled(
    client: TestClient, run_id: str, ready: str
) -> None:
    html = client.get(f"/conversions/{run_id}").text

    assert "Already reconciled" not in html
    assert "<dt>Ready for reconciliation</dt>" in html
    assert cell(html, "ready") == ready


def test_reconciliation_page_shows_one_success_message(client: TestClient) -> None:
    html = client.get("/conversions/run-reconciled/reconciliation").text

    assert html.count("notice-success") == 1
    assert 'data-report="pass"' in html
    assert 'data-trust="reconciled"' not in html
    assert html.count("has <strong>not</strong> been released") == 1
    summary = client.get("/conversions/run-reconciled").text
    assert summary.count("notice-success") == 1
    assert 'data-trust="reconciled"' in summary


@pytest.mark.parametrize(
    ("run_id", "messages"),
    [
        ("run-tampered-report", ('data-trust="untrusted"', 'data-report="unverified"')),
        ("run-source-changed", ('data-trust="untrusted"', 'data-report="unverified"')),
        ("run-no-report", ('data-trust="untrusted"', 'data-report="unavailable"')),
        ("run-mismatch", ('data-report="fail"',)),
    ],
)
def test_reconciliation_page_keeps_warnings_for_untrusted_and_failed_runs(
    client: TestClient, run_id: str, messages: tuple[str, ...]
) -> None:
    html = client.get(f"/conversions/{run_id}/reconciliation").text

    for message in messages:
        assert message in html
    assert "notice-success" not in html
    assert 'data-report="pass"' not in html
