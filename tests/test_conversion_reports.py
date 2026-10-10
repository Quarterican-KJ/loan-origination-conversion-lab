"""Exception, exclusion, and warning reports (spec section 13; backlog CONV-002)."""

import csv
import hashlib
import io
import json
import os
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from loan_lab.conversion.legacy import (
    ReportRunFailedError,
    check_ready,
    plan_conversion,
    reconcile_run,
    recover_run,
    run_conversion,
)
from loan_lab.conversion.legacy import reports as report_module
from loan_lab.conversion.legacy import run as run_module
from loan_lab.conversion.legacy.reconcile import ReconciliationRefusedError
from loan_lab.conversion.legacy.reports import COLUMNS, build_reports, spreadsheet_safe
from loan_lab.main import create_app
from loan_lab.paths import find_project_root

PROJECT_ROOT = find_project_root(Path(__file__))
SAMPLE_DIR = PROJECT_ROOT / "sample_data" / "legacy"
BORROWERS, APPLICATIONS, PARTIES = "borrowers.csv", "applications.csv", "application_parties.csv"
REPORT_NAMES = ("exceptions.csv", "exclusions.csv", "warnings.csv")
FORMULA = '=HYPERLINK("http://example.invalid","<script>alert(1)</script>")'
FORMULA_LINE = '0000500112,"=HYPERLINK(""http://example.invalid"",""<script>alert(1)</script>"")",PRI'


class Lab:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.conversion = root / "data" / "conversion"
        self.evidence = root / "output" / "conversion"

    def run(self, run_id: str = "run-1", source: Path = SAMPLE_DIR) -> Any:
        return run_conversion(source, run_id, conversion_root=self.conversion,
                              evidence_root=self.evidence)

    def directory(self, run_id: str = "run-1") -> Path:
        return self.evidence / run_id

    def manifest(self, run_id: str = "run-1") -> dict[str, Any]:
        return json.loads((self.directory(run_id) / "manifest.json").read_text("utf-8"))

    def rows(self, name: str, run_id: str = "run-1") -> list[dict[str, str]]:
        data = (self.directory(run_id) / "reports" / name).read_bytes()
        return list(csv.DictReader(io.StringIO(data.decode("utf-8"), newline="")))

    def client(self) -> TestClient:
        return TestClient(create_app(database_path=self.root / "no-los.db",
                                     evidence_root=self.evidence))


@pytest.fixture
def lab(tmp_path: Path) -> Lab:
    return Lab(tmp_path)


@pytest.fixture(scope="module")
def sample(tmp_path_factory: pytest.TempPathFactory) -> Lab:
    lab = Lab(tmp_path_factory.mktemp("reports"))
    lab.run()
    return lab


@pytest.fixture(scope="module")
def hostile(tmp_path_factory: pytest.TempPathFactory) -> Lab:
    """The sample extract with a formula and markup as a malformed customer number (SV-03)."""
    lab = Lab(tmp_path_factory.mktemp("hostile"))
    source = lab.root / "extract"
    shutil.copytree(SAMPLE_DIR, source)
    parties = source / PARTIES
    data = parties.read_bytes()
    assert data.count(b"0000500112,10015,PRI") == 1
    parties.write_bytes(data.replace(b"0000500112,10015,PRI", FORMULA_LINE.encode()))
    lab.run(source=source)
    return lab


@pytest.fixture
def client(sample: Lab) -> Iterator[TestClient]:
    with sample.client() as test_client:
        yield test_client


def source_line(name: str, line: int) -> str:
    return (SAMPLE_DIR / name).read_text("utf-8").splitlines()[line - 1]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in root.rglob("*") if p.is_file()
    }


# --- Exact report contents ------------------------------------------------------------------


def test_exceptions_match_the_specified_rejections(sample: Lab) -> None:
    rows = sample.rows("exceptions.csv")

    assert len(rows) == 18
    assert [(r["FILE_NAME"], int(r["LINE_NO"]), r["RULE_CODE"]) for r in rows] == [
        (BORROWERS, 12, "SV-02"),
        (BORROWERS, 14, "SV-09"),
        (BORROWERS, 15, "MP-01"),
        (BORROWERS, 16, "SV-09"),
        (APPLICATIONS, 8, "MP-02"),
        (APPLICATIONS, 9, "SV-05"),
        (APPLICATIONS, 10, "RF-07"),
        (APPLICATIONS, 10, "RF-06"),
        (APPLICATIONS, 13, "RF-07"),
        (APPLICATIONS, 13, "RF-06"),
        (APPLICATIONS, 14, "RF-06"),
        (PARTIES, 13, "RF-05"),
        (PARTIES, 14, "RF-05"),
        (PARTIES, 15, "RF-03"),
        (PARTIES, 16, "RF-02"),
        (PARTIES, 19, "SV-03"),
        (PARTIES, 20, "RF-05"),
        (PARTIES, 21, "RF-01"),
    ]


def test_exception_row_carries_the_full_specified_evidence(sample: Lab) -> None:
    row = next(
        r for r in sample.rows("exceptions.csv")
        if r["FILE_NAME"] == "application_parties.csv" and r["LINE_NO"] == "16"
    )

    assert row == {
        "FILE_NAME": PARTIES,
        "LINE_NO": "16",
        "SOURCE_KEY": "0000500109/00010099",
        "UNIT_KEY": "0000500109",
        "STAGE": "reference",
        "RULE_CODE": "RF-02",
        "DEPENDENT": "N",
        "ROOT_CAUSE": "RF-02 application_parties.csv:16",
        "FIELD": "CUST_NO",
        "SOURCE_VALUE": "00010099",
        "MESSAGE": "Customer 00010099 is not in borrowers.csv; no placeholder is created.",
        "REMEDIATION": report_module.REMEDIATION[report_module.Rule.RF_02],
        "SOURCE_LINE": "0000500109,00010099,GTR",
    }


def test_dependent_rejections_point_to_their_unit_root_cause(sample: Lab) -> None:
    rows = sample.rows("exceptions.csv")
    dependent = [r for r in rows if r["DEPENDENT"] == "Y"]

    assert [(r["FILE_NAME"], r["LINE_NO"]) for r in dependent] == [
        (PARTIES, "13"), (PARTIES, "14"), (PARTIES, "20"),
    ]
    assert {r["RULE_CODE"] for r in dependent} == {"RF-05"}
    by_line = {r["LINE_NO"]: r for r in dependent}
    assert by_line["13"]["ROOT_CAUSE"] == "MP-02 applications.csv:8"
    assert by_line["13"]["UNIT_KEY"] == "0000500107"
    assert by_line["14"]["ROOT_CAUSE"] == "SV-05 applications.csv:9"
    assert by_line["20"]["ROOT_CAUSE"] == "RF-06 applications.csv:14"
    # RF-07 names every rejected relationship; RF-06 only the rejected primary (spec 12.3.4).
    unit = {
        r["RULE_CODE"]: r for r in rows if r["FILE_NAME"] == APPLICATIONS and r["LINE_NO"] == "10"
    }
    assert unit["RF-07"]["ROOT_CAUSE"] == (
        "RF-03 application_parties.csv:15; RF-02 application_parties.csv:16"
    )
    assert unit["RF-06"]["ROOT_CAUSE"] == "RF-03 application_parties.csv:15"
    for row in unit.values():
        assert row["UNIT_KEY"] == "0000500109" and row["DEPENDENT"] == "N"


def test_identifiers_and_source_lines_are_exact(sample: Lab) -> None:
    rows = sample.rows("exceptions.csv")

    for row in rows:
        assert row["SOURCE_LINE"] == source_line(row["FILE_NAME"], int(row["LINE_NO"]))
    malformed = next(r for r in rows if r["RULE_CODE"] == "SV-03")
    # Leading zeros lost in the source are never restored, and no other key is matched.
    assert malformed["SOURCE_KEY"] == "0000500112/10015"
    assert malformed["SOURCE_VALUE"] == "10015"
    customer = next(r for r in rows if r["SOURCE_KEY"] == "00010011")
    assert customer["UNIT_KEY"] == "" and customer["RULE_CODE"] == "SV-02"
    orphan = next(r for r in rows if r["RULE_CODE"] == "RF-01")
    assert orphan["UNIT_KEY"] == ""


def test_exclusions_distinguish_own_rule_from_dependent(sample: Lab) -> None:
    rows = sample.rows("exclusions.csv")

    assert [(r["FILE_NAME"], r["LINE_NO"], r["RULE_CODE"], r["DEPENDENT"]) for r in rows] == [
        (BORROWERS, "13", "EX-01", "N"),
        (APPLICATIONS, "11", "EX-02", "N"),
        (APPLICATIONS, "12", "EX-03", "N"),
        (PARTIES, "10", "EX-04", "N"),
        (PARTIES, "17", "EX-05", "Y"),
        (PARTIES, "18", "EX-05", "Y"),
    ]
    assert {r["STAGE"] for r in rows} == {"exclusion"}
    by_line = {(r["FILE_NAME"], r["LINE_NO"]): r for r in rows}
    assert by_line[(PARTIES, "17")]["ROOT_CAUSE"] == "EX-02 applications.csv:11"
    assert by_line[(PARTIES, "18")]["ROOT_CAUSE"] == "EX-03 applications.csv:12"
    assert by_line[(PARTIES, "17")]["UNIT_KEY"] == "0000500110"


def test_warnings_list_the_standalone_customers(sample: Lab) -> None:
    rows = sample.rows("warnings.csv")

    assert [(r["SOURCE_KEY"], r["RULE_CODE"], r["STAGE"]) for r in rows] == [
        ("00010008", "WN-01", "warning"),
        ("00010009", "WN-01", "warning"),
        ("00010015", "WN-01", "warning"),
    ]
    assert {r["FILE_NAME"] for r in rows} == {BORROWERS}
    assert {r["DEPENDENT"] for r in rows} == {"N"}
    delgado = rows[2]
    assert delgado["LINE_NO"] == "17"
    assert delgado["ROOT_CAUSE"] == "EX-05 application_parties.csv:18"
    assert "0000500111 (excluded)" in delgado["MESSAGE"]


def test_files_are_utf8_csv_with_the_specified_header(sample: Lab) -> None:
    for name in REPORT_NAMES:
        data = (sample.directory() / "reports" / name).read_bytes()
        assert not data.startswith(b"\xef\xbb\xbf")
        assert data.split(b"\r\n", 1)[0].decode() == ",".join(COLUMNS)
        assert b"\n" not in data.replace(b"\r\n", b"")


def test_reports_agree_with_the_plan_and_never_overlap(sample: Lab) -> None:
    plan = plan_conversion(SAMPLE_DIR)
    exceptions = {(r["FILE_NAME"], r["LINE_NO"]) for r in sample.rows("exceptions.csv")}
    exclusions = {(r["FILE_NAME"], r["LINE_NO"]) for r in sample.rows("exclusions.csv")}

    assert not exceptions & exclusions
    for file in (BORROWERS, APPLICATIONS, PARTIES):
        counts = plan.disposition_counts(file)
        assert sum(1 for f, _ in exceptions if f == file) == counts["rejected"]
        assert sum(1 for f, _ in exclusions if f == file) == counts["excluded"]
    assert len(sample.rows("exceptions.csv")) == len(plan.issues)


def test_reports_are_deterministic(sample: Lab) -> None:
    plan = plan_conversion(SAMPLE_DIR)
    for report in build_reports(plan):
        assert report.data == (sample.directory() / "reports" / report.kind.file_name).read_bytes()


# --- Manifest integrity ------------------------------------------------------------------------


def test_manifest_records_complete_reports_with_checksums(sample: Lab) -> None:
    manifest = sample.manifest()
    record = manifest["reports"]

    assert manifest["manifest_version"] == 3
    assert record["state"] == "complete" and record["error"] is None
    assert record["columns"] == list(COLUMNS)
    for name in REPORT_NAMES:
        entry = record["files"][name.removesuffix(".csv")]
        path = sample.directory() / "reports" / name
        assert entry["path"] == f"reports/{name}"
        assert entry["sha256"] == sha256(path)
        assert entry["bytes"] == path.stat().st_size
    exceptions = record["files"]["exceptions"]
    assert exceptions["rows"] == 18 == manifest["validation"]["rejections"]
    assert exceptions["source_rows"] == {BORROWERS: 4, APPLICATIONS: 5, PARTIES: 7}
    assert exceptions["dependent_source_rows"] == {BORROWERS: 0, APPLICATIONS: 0, PARTIES: 3}
    exclusions = record["files"]["exclusions"]
    assert exclusions["source_rows"] == {BORROWERS: 1, APPLICATIONS: 2, PARTIES: 3}
    assert exclusions["dependent_source_rows"] == {BORROWERS: 0, APPLICATIONS: 0, PARTIES: 2}
    assert record["files"]["warnings"]["rules"] == {"WN-01": 3}
    assert manifest["evidence"]["state"] == "complete"
    assert check_ready("run-1", evidence_root=sample.evidence).ready


def test_reports_are_written_before_the_load(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[bool] = []
    real_load = run_module.load_plan

    def load(*args: Any, **kwargs: Any) -> Any:
        seen.append(all((lab.directory() / "reports" / n).is_file() for n in REPORT_NAMES))
        return real_load(*args, **kwargs)

    monkeypatch.setattr(run_module, "load_plan", load)
    lab.run()
    assert seen == [True]


@pytest.mark.parametrize("change", ["edit", "delete"])
def test_a_changed_report_blocks_reconciliation(lab: Lab, change: str) -> None:
    lab.run()
    path = lab.directory() / "reports" / "exclusions.csv"
    if change == "edit":
        path.write_bytes(path.read_bytes().replace(b"EX-04", b"EX-01"))
    else:
        path.unlink()

    problems = check_ready("run-1", evidence_root=lab.evidence).problems
    expected = "does not match its recorded checksum" if change == "edit" else "is missing"
    assert any("exclusions.csv" in p and expected in p for p in problems)
    with pytest.raises(ReconciliationRefusedError):
        reconcile_run("run-1", evidence_root=lab.evidence)


def test_inconsistent_manifest_counts_block_reconciliation(lab: Lab) -> None:
    lab.run()
    manifest_path = lab.directory() / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["reports"]["files"]["exceptions"]["dependent_source_rows"][PARTIES] = 2
    run_module.write_json_atomic(manifest_path, manifest)

    problems = check_ready("run-1", evidence_root=lab.evidence).problems
    assert any("exceptions.csv disagrees" in p for p in problems)


def test_runs_that_predate_reports_stay_ready_but_cannot_be_reconciled(lab: Lab) -> None:
    lab.run()
    manifest_path = lab.directory() / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["manifest_version"] = 2
    del manifest["reports"]
    run_module.write_json_atomic(manifest_path, manifest)
    for name in REPORT_NAMES:
        (lab.directory() / "reports" / name).unlink()
    before = manifest_path.read_bytes()

    assert check_ready("run-1", evidence_root=lab.evidence).ready
    with pytest.raises(ReconciliationRefusedError, match="predates row-level reports"):
        reconcile_run("run-1", evidence_root=lab.evidence)
    assert manifest_path.read_bytes() == before
    assert not (lab.directory() / "reports" / "reconciliation.json").exists()


# --- Report-writing failures -----------------------------------------------------------------


def fail_report_writes(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    real = run_module.write_bytes_atomic

    def write(path: Path, payload: bytes) -> None:
        if path.name == name:
            raise OSError("injected report write failure")
        real(path, payload)

    monkeypatch.setattr(run_module, "write_bytes_atomic", write)


def test_report_write_failure_fails_the_run_before_any_load(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    fail_report_writes(monkeypatch, "exclusions.csv")

    with pytest.raises(ReportRunFailedError) as caught:
        lab.run()

    assert caught.value.stage == "reports"
    manifest = lab.manifest()
    assert manifest["status"] == "FAILED"
    assert manifest["failure"]["stage"] == "reports"
    assert manifest["failure"]["step"] == "write_reports"
    assert manifest["database_transaction"] == "not_started"
    assert manifest["ready_for_reconciliation"] is False
    assert manifest["evidence"]["state"] == "incomplete"
    assert manifest["reports"]["state"] == "failed"
    assert "injected report write failure" in manifest["reports"]["error"]
    # The report written before the failure is kept and recorded; the failed one is absent.
    assert set(manifest["reports"]["files"]) == {"exceptions"}
    assert sorted(p.name for p in (lab.directory() / "reports").iterdir()) == ["exceptions.csv"]
    assert manifest["reports"]["files"]["exceptions"]["sha256"] == sha256(
        lab.directory() / "reports" / "exceptions.csv"
    )
    # Nothing was loaded: no database and no run directory under the conversion root.
    assert not (lab.conversion / "run-1").exists()
    assert sorted(p.name for p in (lab.directory() / "source").iterdir()) == sorted(
        p.name for p in SAMPLE_DIR.iterdir()
    )


def test_interrupted_report_write_leaves_no_partial_file(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_replace = os.replace

    def replace(source: Any, target: Any) -> None:
        if str(target).endswith("warnings.csv"):
            raise OSError("injected interruption before rename")
        real_replace(source, target)

    monkeypatch.setattr(run_module.os, "replace", replace)
    with pytest.raises(ReportRunFailedError):
        lab.run()
    monkeypatch.undo()

    reports = lab.directory() / "reports"
    assert sorted(p.name for p in reports.iterdir()) == ["exceptions.csv", "exclusions.csv"]
    assert not list(lab.directory().rglob("*.tmp"))
    assert lab.manifest()["reports"]["state"] == "failed"


def test_report_accounting_mismatch_writes_no_report(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_rows = report_module._rows

    def drop_one(plan: Any, kind: Any) -> Any:
        rows = real_rows(plan, kind)
        return rows[1:] if kind is report_module.ReportKind.EXCLUSIONS else rows

    monkeypatch.setattr(report_module, "_rows", drop_one)
    with pytest.raises(ReportRunFailedError, match="ReportAccountingError"):
        lab.run()

    assert not (lab.directory() / "reports").exists()
    manifest = lab.manifest()
    assert manifest["reports"]["state"] == "failed"
    assert manifest["evidence"]["state"] == "incomplete"
    assert not (lab.conversion / "run-1").exists()


def test_unrecorded_reports_are_never_settled_as_complete(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = run_module.write_json_atomic

    def write(path: Path, data: Any) -> None:
        if (data.get("reports") or {}).get("state") in ("complete", "failed"):
            raise OSError("injected manifest failure")
        real(path, data)

    monkeypatch.setattr(run_module, "write_json_atomic", write)
    with pytest.raises(ReportRunFailedError):
        lab.run()
    monkeypatch.undo()

    manifest = lab.manifest()
    assert manifest["status"] == "VALIDATED"
    assert manifest["reports"]["state"] == "in_progress"
    recover_run("run-1", evidence_root=lab.evidence)
    recovered = lab.manifest()
    assert recovered["status"] == "FAILED"
    assert recovered["evidence"]["state"] == "incomplete"
    assert not check_ready("run-1", evidence_root=lab.evidence).ready


# --- Hostile source values ------------------------------------------------------------------


def test_hostile_values_are_kept_exactly_in_the_archived_report(hostile: Lab) -> None:
    row = next(r for r in hostile.rows("exceptions.csv") if r["RULE_CODE"] == "SV-03")

    assert row["SOURCE_VALUE"] == FORMULA
    assert row["SOURCE_KEY"] == f"0000500112/{FORMULA}"
    assert row["SOURCE_LINE"] == FORMULA_LINE
    assert len(hostile.rows("exceptions.csv")) == 18


def test_spreadsheet_safe_neutralizes_formula_prefixes() -> None:
    for value in ("=1+1", "+1", "-1", "@SUM(A1)", "\tx", "\rx"):
        assert spreadsheet_safe(value) == "'" + value
    for value in ("00010015", "", "Jordan", "a=b", "6.875"):
        assert spreadsheet_safe(value) == value


def test_download_is_spreadsheet_safe_and_leaves_evidence_unchanged(hostile: Lab) -> None:
    path = hostile.directory() / "reports" / "exceptions.csv"
    before = sha256(path)
    with hostile.client() as client:
        response = client.get("/conversions/run-1/reports/exceptions/download")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"] == 'attachment; filename="run-1-exceptions.csv"'
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.content.startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"), newline="")))
    assert len(rows) == 18
    hostile_row = next(r for r in rows if r["RULE_CODE"] == "SV-03")
    assert hostile_row["SOURCE_VALUE"] == "'" + FORMULA
    assert hostile_row["SOURCE_KEY"] == f"0000500112/{FORMULA}"
    assert not any(v.startswith(("=", "+", "-", "@")) for r in rows for v in r.values())
    assert sha256(path) == before


def test_hostile_values_are_escaped_in_html(hostile: Lab) -> None:
    with hostile.client() as client:
        html = client.get("/conversions/run-1/reports/exceptions").text

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


# --- Web interface -----------------------------------------------------------------------------


def row_count(html: str) -> int:
    return html.count("<tr data-report-row")


def test_report_pages_show_every_row(client: TestClient) -> None:
    for kind, rows in (("exceptions", 18), ("exclusions", 6), ("warnings", 3)):
        response = client.get(f"/conversions/run-1/reports/{kind}")
        assert response.status_code == 200
        assert row_count(response.text) == rows
        assert f'data-total="{rows}"' in response.text
        assert 'data-download' in response.text


@pytest.mark.parametrize(
    ("query", "rows"),
    [
        ("rule=RF-05", 3),
        ("dependent=Y", 3),
        ("dependent=N", 15),
        (f"file={BORROWERS}", 4),
        ("q=00010099", 1),
        ("q=unit+rejected&dependent=Y", 0),
        ("file=application_parties.csv&rule=RF-05", 3),
    ],
)
def test_report_filters(client: TestClient, query: str, rows: int) -> None:
    html = client.get(f"/conversions/run-1/reports/exceptions?{query}").text

    assert row_count(html) == rows
    assert f'data-matched="{rows}"' in html


def test_invalid_filter_values_are_ignored(client: TestClient) -> None:
    html = client.get(
        "/conversions/run-1/reports/exceptions?file=../manifest.json&rule=<x>&dependent=maybe"
    ).text

    assert row_count(html) == 18
    assert "&lt;x&gt;" not in html


def test_detail_page_links_the_reports(client: TestClient) -> None:
    html = client.get("/conversions/run-1").text

    assert 'data-report-state="complete"' in html
    for kind, rows in (("exceptions", 18), ("exclusions", 6), ("warnings", 3)):
        assert f'/conversions/run-1/reports/{kind}">{rows} rows</a>' in html
    assert "not produced yet" not in html


@pytest.mark.parametrize(
    ("url", "status"),
    [
        ("/conversions/run-1/reports/manifest", 404),
        ("/conversions/run-1/reports/..%2Fmanifest.json", 404),
        ("/conversions/run-1/reports/exceptions.csv", 404),
        ("/conversions/run-1/reports/load_result/download", 404),
        ("/conversions/no-such-run/reports/exceptions", 404),
        ("/conversions/no-such-run/reports/exceptions/download", 404),
        ("/conversions/..%2Fsecret/reports/exceptions", 404),
        ("/conversions/CON/reports/exceptions/download", 400),
    ],
)
def test_report_paths_are_restricted(client: TestClient, url: str, status: int) -> None:
    response = client.get(url)

    assert response.status_code == status
    assert "text/csv" not in response.headers["content-type"]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize(
    "url", ["/conversions/run-1/reports/exceptions", "/conversions/run-1/reports/exceptions/download"]
)
def test_report_routes_are_read_only(client: TestClient, method: str, url: str) -> None:
    response = client.request(method, url)

    assert response.status_code == 405
    assert response.headers["allow"] == "GET"


def test_browsing_reports_writes_nothing(sample: Lab, client: TestClient) -> None:
    before = snapshot(sample.root)

    for kind in ("exceptions", "exclusions", "warnings"):
        client.get(f"/conversions/run-1/reports/{kind}")
        client.get(f"/conversions/run-1/reports/{kind}?q=RF&dependent=Y")
        client.get(f"/conversions/run-1/reports/{kind}/download")
        client.post(f"/conversions/run-1/reports/{kind}")
    client.get("/conversions/run-1")

    assert snapshot(sample.root) == before
    assert not (sample.root / "no-los.db").exists()


def test_tampered_report_is_not_shown_or_downloaded(lab: Lab) -> None:
    lab.run()
    path = lab.directory() / "reports" / "warnings.csv"
    path.write_bytes(path.read_bytes().replace(b"00010015", b"00010016"))

    with lab.client() as client:
        page = client.get("/conversions/run-1/reports/warnings").text
        download = client.get("/conversions/run-1/reports/warnings/download")
        detail = client.get("/conversions/run-1").text

    assert row_count(page) == 0
    assert "does not match the checksum recorded in the manifest" in page
    assert download.status_code == 404
    assert 'data-trust="untrusted"' in detail
    assert "reports/warnings.csv does not match its recorded checksum" in detail


def test_report_replaced_by_a_directory_is_not_read(lab: Lab) -> None:
    lab.run()
    path = lab.directory() / "reports" / "exclusions.csv"
    path.unlink()
    path.mkdir()

    with lab.client() as client:
        page = client.get("/conversions/run-1/reports/exclusions").text
    assert row_count(page) == 0
    assert "not a regular file inside the run directory" in page
    assert not check_ready("run-1", evidence_root=lab.evidence).ready


@pytest.mark.skipif(sys.platform != "win32", reason="Directory junctions are Windows-only.")
def test_junctioned_reports_directory_is_not_followed(lab: Lab) -> None:
    import _winapi

    lab.run()
    reports = lab.directory() / "reports"
    outside = lab.root / "outside"
    shutil.copytree(reports, outside)
    shutil.rmtree(reports)
    _winapi.CreateJunction(str(outside), str(reports))
    try:
        with lab.client() as client:
            page = client.get("/conversions/run-1/reports/exceptions").text
            download = client.get("/conversions/run-1/reports/exceptions/download")
        assert row_count(page) == 0
        assert download.status_code == 404
    finally:
        os.rmdir(reports)


def test_failed_report_run_is_shown_as_failed(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    fail_report_writes(monkeypatch, "warnings.csv")
    with pytest.raises(ReportRunFailedError):
        lab.run()
    monkeypatch.undo()

    with lab.client() as client:
        detail = client.get("/conversions/run-1").text
        page = client.get("/conversions/run-1/reports/exceptions").text
    assert 'data-report-state="failed"' in detail
    assert 'data-condition="failed"' in detail
    assert "injected report write failure" in detail
    assert row_count(page) == 0
    assert 'data-report-available="false"' in page


def test_runs_that_predate_reports_are_labeled(lab: Lab) -> None:
    lab.run()
    manifest_path = lab.directory() / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["manifest_version"] = 2
    del manifest["reports"]
    run_module.write_json_atomic(manifest_path, manifest)

    with lab.client() as client:
        detail = client.get("/conversions/run-1").text
        page = client.get("/conversions/run-1/reports/exceptions").text
    assert 'data-report-state="not_produced"' in detail
    assert 'data-condition="loaded"' in detail
    assert "predates the exception, exclusion, and warning reports" in page


def test_source_validation_failure_produces_no_reports(lab: Lab) -> None:
    source = lab.root / "extract"
    shutil.copytree(SAMPLE_DIR, source)
    (source / "extract_control.csv").write_text("not,a,control,file\n", "utf-8")
    with pytest.raises(run_module.SourceRunFailedError):
        lab.run(source=source)

    manifest = lab.manifest()
    assert manifest["reports"]["state"] == "not_started"
    assert manifest["evidence"]["state"] == "complete"
    assert not (lab.directory() / "reports").exists()
    with lab.client() as client:
        assert 'data-report-state="not_started"' in client.get("/conversions/run-1").text
