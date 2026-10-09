"""Milestone 8: synthetic, demo-only conversion failure scenarios."""

import hashlib
import inspect
import json
import re
import shutil
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from loan_lab.conversion.legacy import cli as conversion_cli
from loan_lab.conversion.legacy import contract, plan_conversion, run_conversion
from loan_lab.conversion.legacy import reconcile_cli
from loan_lab.main import create_app
from loan_lab.paths import find_project_root
from loan_lab.scenarios import (
    RATE_DEFECT,
    Scenario,
    ScenarioRefusedError,
    ScenarioResult,
    inject_rate_defect,
    run_scenario,
)
from loan_lab.scenarios import cli as scenario_cli

PROJECT_ROOT = find_project_root(Path(__file__))
SAMPLE_DIR = PROJECT_ROOT / "sample_data" / "legacy"
PROTECTED = (
    PROJECT_ROOT / "output" / "conversion" / "DEMO-001",
    PROJECT_ROOT / "data" / "conversion" / "DEMO-001",
    PROJECT_ROOT / "data" / "loan_lab_dev.db",
    SAMPLE_DIR,
)
RUN_IDS = {
    Scenario.CONTROL_TOTALS: ("SYN-CTRL-first", "SYN-CTRL-second"),
    Scenario.BUSINESS_REJECTIONS: ("SYN-REJ-first", "SYN-REJ-second"),
    Scenario.LOADER_DEFECT: ("SYN-DEFECT-first", "SYN-DEFECT-second"),
}


@dataclass(frozen=True)
class Roots:
    conversion: Path
    evidence: Path
    scenarios: Path

    def kwargs(self) -> dict[str, Path]:
        return {
            "conversion_root": self.conversion,
            "evidence_root": self.evidence,
            "scenario_root": self.scenarios,
        }


@dataclass(frozen=True)
class Lab:
    roots: Roots
    results: dict[str, ScenarioResult]
    protected_before: dict[str, tuple[bytes, int]]
    neighbour_before: dict[str, tuple[bytes, int]]


def snapshot(*paths: Path) -> dict[str, tuple[bytes, int]]:
    files: dict[str, tuple[bytes, int]] = {}
    for path in paths:
        candidates = [path] if path.is_file() else sorted(path.rglob("*")) if path.exists() else []
        for item in candidates:
            if item.is_file():
                files[item.as_posix()] = (item.read_bytes(), item.stat().st_mtime_ns)
    return files


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text("utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def lab(tmp_path_factory: pytest.TempPathFactory) -> Lab:
    root = tmp_path_factory.mktemp("scenarios")
    roots = Roots(root / "data" / "conversion", root / "output" / "conversion",
                  root / "data" / "scenarios")
    protected_before = snapshot(*PROTECTED)
    # An unrelated run in the same evidence root, which no scenario may touch.
    run_conversion(SAMPLE_DIR, "DEMO-001", conversion_root=roots.conversion,
                   evidence_root=roots.evidence)
    neighbour_before = snapshot(roots.evidence / "DEMO-001", roots.conversion / "DEMO-001")
    results = {
        run_id: run_scenario(scenario, run_id, **roots.kwargs())
        for scenario, run_ids in RUN_IDS.items()
        for run_id in run_ids
    }
    return Lab(roots, results, protected_before, neighbour_before)


def evidence(lab: Lab, run_id: str) -> Path:
    return lab.roots.evidence / run_id


def manifest(lab: Lab, run_id: str) -> dict[str, Any]:
    return read_json(evidence(lab, run_id) / "manifest.json")


# --- Every scenario -------------------------------------------------------------------------


@pytest.mark.parametrize("run_id", [ids[0] for ids in RUN_IDS.values()])
def test_scenarios_match_their_expected_outcomes(lab: Lab, run_id: str) -> None:
    result = lab.results[run_id]

    assert result.deviations == ()
    assert result.as_expected
    assert result.evidence_directory == evidence(lab, run_id)


@pytest.mark.parametrize("scenario", list(Scenario))
def test_each_run_is_labeled_synthetic_outside_its_evidence(lab: Lab, scenario: Scenario) -> None:
    run_id = RUN_IDS[scenario][0]
    descriptor = read_json(lab.roots.scenarios / run_id / "scenario.json")

    assert descriptor["synthetic"] is True
    assert descriptor["demo_only"] is True
    assert descriptor["label"].startswith("SYNTHETIC DEMO SCENARIO")
    assert descriptor["scenario"] == scenario
    assert descriptor["run_id"] == run_id
    assert not (evidence(lab, run_id) / "scenario.json").exists()
    assert manifest(lab, run_id)["release"] is None


# --- Scenario 1: control totals ------------------------------------------------------------


def test_control_total_failure_fails_validation_without_a_database(lab: Lab) -> None:
    run_id = "SYN-CTRL-first"
    recorded = manifest(lab, run_id)

    assert recorded["status"] == "FAILED"
    assert recorded["failure"]["stage"] == "validation"
    assert recorded["database_transaction"] == "not_started"
    assert recorded["database_path"] is None
    assert recorded["ready_for_reconciliation"] is False
    issues = {(i["rule"], i["file"], i["message"]) for i in recorded["validation"]["issues"]}
    assert issues == {
        ("RUN-04", "applications.csv", "File has 13 data rows; the control file says 14."),
        ("RUN-05", "applications.csv",
         "REQ_AMT total is 5467000.00; the control file says 5476000.00."),
    }
    assert not (lab.roots.conversion / run_id).exists()
    assert not (evidence(lab, run_id) / "reports").exists()


def test_control_total_failure_archives_the_exact_source(lab: Lab) -> None:
    run_id = "SYN-CTRL-first"
    recorded = manifest(lab, run_id)["source"]["files"]
    archive = evidence(lab, run_id) / "source"
    extract = lab.roots.scenarios / run_id / "extract"

    for name in (*contract.DATA_FILES, contract.CONTROL_FILE):
        assert recorded[name]["archived_sha256"] == sha256(archive / name) == sha256(extract / name)
    for name in contract.DATA_FILES:
        assert (archive / name).read_bytes() == (SAMPLE_DIR / name).read_bytes()
    control = (archive / contract.CONTROL_FILE).read_text("utf-8")
    assert "applications.csv,14,5476000.00,20260930" in control
    assert "applications.csv,13,5467000.00" in (SAMPLE_DIR / contract.CONTROL_FILE).read_text()
    altered = read_json(lab.roots.scenarios / run_id / "scenario.json")["altered"]
    assert altered["RECORD_COUNT"] == {"extract": "13", "scenario": "14"}
    assert altered["AMOUNT_TOTAL"] == {"extract": "5467000.00", "scenario": "5476000.00"}


# --- Scenario 2: business rejections -------------------------------------------------------


def test_business_rejections_are_documented_and_reconcile(lab: Lab) -> None:
    run_id = "SYN-REJ-first"
    recorded = manifest(lab, run_id)
    rows = recorded["validation"]["rows"]

    assert recorded["status"] == "RECONCILED"
    assert recorded["reconciliation"]["result"] == "passed"
    assert recorded["reconciliation"]["rules_failed"] == []
    assert [(rows[f]["read"], rows[f]["eligible"], rows[f]["excluded"], rows[f]["rejected"])
            for f in contract.DATA_FILES] == [(16, 11, 1, 4), (13, 6, 2, 5), (20, 10, 3, 7)]
    assert recorded["validation"]["requested_amount"] == {
        "eligible": "2281000.00", "excluded": "291000.00", "rejected": "2895000.00",
        "unparseable": 0,
    }
    report = read_json(evidence(lab, run_id) / "reports" / "reconciliation.json")
    assert report["result"] == "PASS"
    assert report["discrepancies"] == []
    findings = "\n".join(lab.results[run_id].findings)
    assert re.search(r"applications\.csv line 8 0000500107: MP-02", findings)
    assert re.search(r"applications\.csv line 14 0000500113: RF-06", findings)
    assert re.search(r"application_parties\.csv line 21 0000500199/00010003: RF-01", findings)


# --- Scenario 3: loader defect -------------------------------------------------------------


def test_loader_defect_is_caught_by_reconciliation(lab: Lab) -> None:
    run_id = "SYN-DEFECT-first"
    recorded = manifest(lab, run_id)

    assert recorded["status"] == "FAILED"
    assert recorded["failure"]["stage"] == "reconciliation"
    assert recorded["reconciliation"]["rules_failed"] == ["RC-07"]
    report = read_json(evidence(lab, run_id) / "reports" / "reconciliation.json")
    assert report["result"] == "FAIL"
    [discrepancy] = report["discrepancies"]
    assert discrepancy["rule"] == "RC-07"
    assert discrepancy["source_key"] == "0000500101"
    assert discrepancy["field"] == "interest_rate"
    assert (discrepancy["file"], discrepancy["line"]) == ("applications.csv", 2)
    assert (discrepancy["expected"], discrepancy["actual"]) == ("6.5000", "6.6000")
    assert [r["rule"] for r in report["rules"] if r["result"] == "FAIL"] == ["RC-07"]


def test_loader_defect_keeps_counts_and_dollars(lab: Lab) -> None:
    defect, clean = "SYN-DEFECT-first", "SYN-REJ-first"
    loaded = read_json(evidence(lab, defect) / "reports" / "load_result.json")["loaded"]

    assert loaded == read_json(evidence(lab, clean) / "reports" / "load_result.json")["loaded"]
    assert loaded == {"borrowers": 11, "applications": 6, "parties": 10,
                      "requested_amount": "2281000.00"}
    expected = manifest(lab, defect)["expected_target"]
    assert {k: str(v) for k, v in expected.items()} == {k: str(v) for k, v in loaded.items()}
    totals = read_json(evidence(lab, defect) / "reports" / "reconciliation.json")["totals"]
    assert totals["requested_amount"]["loaded"] == totals["requested_amount"]["target_total"]
    assert totals["requested_amount"]["target_total"] == "2281000.00"
    assert totals["target"] == {"borrowers": 11, "applications": 6, "parties": 10}


def test_loader_defect_is_in_the_database_and_its_recorded_checksum(lab: Lab) -> None:
    run_id = "SYN-DEFECT-first"
    database = lab.roots.conversion / run_id / "loan_lab_conversion.db"
    recorded = manifest(lab, run_id)

    assert recorded["database"]["sha256"] == sha256(database)
    load_report = read_json(evidence(lab, run_id) / "reports" / "load_result.json")
    assert load_report["database"]["sha256"] == sha256(database)
    rates = {}
    for name in (run_id, "SYN-REJ-first"):
        path = lab.roots.conversion / name / "loan_lab_conversion.db"
        with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as conn:
            rates[name] = dict(conn.execute(
                "SELECT source_system_id, interest_rate FROM loan_application"
            ).fetchall())
    assert rates[run_id]["0000500101"] == 66000
    assert rates["SYN-REJ-first"]["0000500101"] == 65000
    del rates[run_id]["0000500101"], rates["SYN-REJ-first"]["0000500101"]
    assert rates[run_id] == rates["SYN-REJ-first"]


def test_inject_rate_defect_changes_only_one_mapped_rate() -> None:
    plan = plan_conversion(SAMPLE_DIR)
    defective, defect = inject_rate_defect(plan)

    assert (defect.application, defect.line) == ("0000500101", 2)
    assert defect.injected_rate == defect.planned_rate + RATE_DEFECT == Decimal("6.6000")
    assert plan.applications_to_load[0].interest_rate == Decimal("6.5000")
    changed = [
        (before, after) for before, after in zip(
            plan.applications_to_load, defective.applications_to_load, strict=True)
        if before != after
    ]
    assert len(changed) == 1
    assert changed[0][1].interest_rate == Decimal("6.6000")
    assert defective.borrowers_to_load == plan.borrowers_to_load
    assert defective.parties_to_load == plan.parties_to_load
    assert defective.amounts == plan.amounts
    assert defective.source_checksums == plan.source_checksums
    for file in contract.DATA_FILES:
        assert defective.outcome_counts(file) == plan.outcome_counts(file)


# --- Repeatability -------------------------------------------------------------------------


def _normalized(lab: Lab, run_id: str) -> dict[str, Any]:
    recorded = manifest(lab, run_id)
    reports = evidence(lab, run_id) / "reports"
    normal: dict[str, Any] = {
        "status": recorded["status"],
        "failure": recorded["failure"] and {
            k: v for k, v in recorded["failure"].items() if k != "reason"
        },
        "validation": recorded["validation"],
        "expected_target": recorded.get("expected_target"),
        "source": {name: entry["archived_sha256"]
                   for name, entry in recorded["source"]["files"].items()},
        "rules_failed": (recorded.get("reconciliation") or {}).get("rules_failed"),
        "database_sha256": (recorded.get("database") or {}).get("sha256"),
    }
    if (reports / "load_result.json").exists():
        normal["loaded"] = read_json(reports / "load_result.json")["loaded"]
    if (reports / "reconciliation.json").exists():
        report = read_json(reports / "reconciliation.json")
        normal["report"] = {key: report[key] for key in ("result", "rules", "totals",
                                                         "discrepancies")}
    descriptor = read_json(lab.roots.scenarios / run_id / "scenario.json")
    normal["descriptor"] = {
        k: v for k, v in descriptor.items() if k not in ("run_id", "evidence_directory", "source")
    }
    normal["findings"] = lab.results[run_id].findings
    return normal


@pytest.mark.parametrize("scenario", list(Scenario))
def test_scenarios_are_repeatable(lab: Lab, scenario: Scenario) -> None:
    first, second = RUN_IDS[scenario]

    assert _normalized(lab, first) == _normalized(lab, second)
    assert first != second


def test_database_checksums_are_deterministic_per_scenario(lab: Lab) -> None:
    def database_sha(run_id: str) -> str:
        return manifest(lab, run_id)["database"]["sha256"]

    assert database_sha("SYN-DEFECT-first") == database_sha("SYN-DEFECT-second")
    assert database_sha("SYN-REJ-first") == database_sha("SYN-REJ-second")
    assert database_sha("SYN-DEFECT-first") != database_sha("SYN-REJ-first")


# --- Safety --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scenario", "run_id"),
    [
        (Scenario.LOADER_DEFECT, "DEMO-001"),
        (Scenario.LOADER_DEFECT, "SYN-REJ-labelled-wrong"),
        (Scenario.LOADER_DEFECT, "SYN-DEFECT-"),
        (Scenario.LOADER_DEFECT, "SYN-DEFECT-../escape"),
        (Scenario.CONTROL_TOTALS, "SYN-CTRL-" + "x" * 60),
        (Scenario.BUSINESS_REJECTIONS, "syn-rej-lowercase"),
    ],
)
def test_invalid_run_ids_are_refused_before_writing(
    tmp_path: Path, scenario: Scenario, run_id: str
) -> None:
    roots = Roots(tmp_path / "db", tmp_path / "evidence", tmp_path / "scenarios")

    with pytest.raises(ScenarioRefusedError):
        run_scenario(scenario, run_id, **roots.kwargs())
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("scenario", list(Scenario))
def test_used_run_ids_are_never_overwritten(lab: Lab, scenario: Scenario) -> None:
    run_id = RUN_IDS[scenario][0]
    before = snapshot(lab.roots.evidence, lab.roots.conversion, lab.roots.scenarios)

    with pytest.raises(ScenarioRefusedError, match="never reused"):
        run_scenario(scenario, run_id, **lab.roots.kwargs())
    assert snapshot(lab.roots.evidence, lab.roots.conversion, lab.roots.scenarios) == before


def test_run_ids_already_used_by_evidence_are_refused(tmp_path: Path) -> None:
    roots = Roots(tmp_path / "db", tmp_path / "evidence", tmp_path / "scenarios")
    (roots.evidence / "SYN-DEFECT-taken").mkdir(parents=True)

    with pytest.raises(ScenarioRefusedError, match="never reused"):
        run_scenario(Scenario.LOADER_DEFECT, "SYN-DEFECT-taken", **roots.kwargs())
    assert not roots.scenarios.exists()
    assert not roots.conversion.exists()


def test_other_runs_and_project_data_are_untouched(lab: Lab) -> None:
    assert snapshot(*PROTECTED) == lab.protected_before
    neighbour = snapshot(lab.roots.evidence / "DEMO-001", lab.roots.conversion / "DEMO-001")
    assert neighbour == lab.neighbour_before
    assert manifest(lab, "DEMO-001")["status"] == "LOADED"


def test_default_run_ids_carry_the_scenario_prefix(tmp_path: Path) -> None:
    roots = Roots(tmp_path / "db", tmp_path / "evidence", tmp_path / "scenarios")
    result = run_scenario(Scenario.CONTROL_TOTALS, **roots.kwargs())

    assert re.fullmatch(r"SYN-CTRL-\d{8}T\d{6}Z-[0-9a-f]{6}", result.run_id)
    assert result.as_expected


def test_production_commands_offer_no_defect_injection() -> None:
    for parser in (conversion_cli.build_parser(), reconcile_cli.build_parser()):
        options = " ".join(o for a in parser._actions for o in a.option_strings)
        assert not re.search(r"(?i)defect|inject|scenario|synthetic|demo", options)
    assert not re.search(r"(?i)defect|inject", str(inspect.signature(run_conversion)))
    package = PROJECT_ROOT / "src" / "loan_lab" / "conversion"
    for path in package.rglob("*.py"):
        assert "loan_lab.scenarios" not in path.read_text("utf-8"), path
        assert "inject" not in path.read_text("utf-8").lower(), path


def test_generated_output_is_git_ignored() -> None:
    if shutil.which("git") is None or not (PROJECT_ROOT / ".git").exists():
        pytest.skip("git is not available")
    paths = [
        "data/scenarios/SYN-DEFECT-x/scenario.json",
        "data/scenarios/SYN-CTRL-x/extract/extract_control.csv",
        "data/conversion/SYN-DEFECT-x/loan_lab_conversion.db",
        "output/conversion/SYN-DEFECT-x/manifest.json",
        "output/conversion/SYN-DEFECT-x/reports/reconciliation.json",
        "output/conversion/SYN-CTRL-x/source/applications.csv",
    ]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", *paths], cwd=PROJECT_ROOT,
        capture_output=True, text=True, check=False,
    )
    assert sorted(result.stdout.split()) == sorted(paths)


# --- Command line --------------------------------------------------------------------------


def test_cli_runs_a_scenario_and_labels_it(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    roots = Roots(tmp_path / "db", tmp_path / "evidence", tmp_path / "scenarios")
    args = ["loader-defect", "--run-id", "SYN-DEFECT-cli", "--conversion-root",
            str(roots.conversion), "--evidence-root", str(roots.evidence),
            "--scenario-root", str(roots.scenarios)]

    assert scenario_cli.main(args) == scenario_cli.EXIT_AS_EXPECTED
    out = capsys.readouterr().out
    assert "SYNTHETIC DEMO SCENARIO" in out
    assert "0000500101 (applications.csv line 2) interest rate 6.5000 -> 6.6000" in out
    assert "RC-07 applications.csv:2 0000500101 interest_rate: expected 6.5000, actual 6.6000" in out
    assert "/conversions/SYN-DEFECT-cli" in out
    assert "Outcome matches the scenario's expectation." in out

    assert scenario_cli.main(args) == scenario_cli.EXIT_REFUSED
    assert "never reused" in capsys.readouterr().err


def test_cli_all_runs_every_scenario(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    roots = Roots(tmp_path / "db", tmp_path / "evidence", tmp_path / "scenarios")
    args = ["all", "--conversion-root", str(roots.conversion), "--evidence-root",
            str(roots.evidence), "--scenario-root", str(roots.scenarios)]

    assert scenario_cli.main(args) == scenario_cli.EXIT_AS_EXPECTED
    assert capsys.readouterr().out.count("Outcome matches the scenario's expectation.") == 3
    prefixes = sorted(p.name.split("-")[1] for p in roots.evidence.iterdir())
    assert prefixes == ["CTRL", "DEFECT", "REJ"]


def test_cli_refuses_run_id_with_all_and_wrong_prefixes(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        scenario_cli.main(["all", "--run-id", "SYN-CTRL-x"])
    assert exit_info.value.code == 2
    args = ["control-totals", "--run-id", "DEMO-002", "--evidence-root", str(tmp_path / "e"),
            "--conversion-root", str(tmp_path / "c"), "--scenario-root", str(tmp_path / "s")]
    assert scenario_cli.main(args) == scenario_cli.EXIT_REFUSED
    assert "must start with 'SYN-CTRL-'" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


# --- Read-only web interface ---------------------------------------------------------------


@pytest.fixture(scope="module")
def client(lab: Lab) -> TestClient:
    app = create_app(database_path=lab.roots.evidence.parent / "no-los.db",
                     evidence_root=lab.roots.evidence)
    with TestClient(app) as test_client:
        yield test_client


def condition(html: str, run_id: str) -> str:
    row = re.search(rf'<tr data-run-id="{run_id}">(.*?)</tr>', html, re.DOTALL).group(1)
    return re.search(r'data-condition="(\w+)"', row).group(1)


def test_web_list_reads_the_scenario_evidence(lab: Lab, client: TestClient) -> None:
    html = client.get("/conversions").text

    assert condition(html, "SYN-CTRL-first") == "failed"
    assert condition(html, "SYN-REJ-first") == "reconciled"
    assert condition(html, "SYN-DEFECT-first") == "failed"


def test_web_shows_the_defect_discrepancy(lab: Lab, client: TestClient) -> None:
    html = client.get("/conversions/SYN-DEFECT-first/reconciliation").text

    assert 'data-report="fail"' in html
    assert 'data-discrepancies="RC-07"' in html
    section = re.search(r'data-discrepancies="RC-07"(.*?)</article>', html, re.DOTALL).group(1)
    assert "0000500101" in section and "6.5000" in section and "6.6000" in section
    assert re.findall(r'<tr data-rule="(RC-\d\d)">.*?data-result="(\w+)"', html, re.DOTALL) == [
        (f"RC-{n:02d}", "FAIL" if n == 7 else "PASS") for n in range(1, 11)
    ]


def test_web_shows_the_validation_failure(lab: Lab, client: TestClient) -> None:
    html = client.get("/conversions/SYN-CTRL-first").text

    assert 'data-condition="failed"' in html
    assert "RUN-04" in html and "RUN-05" in html
    assert "5476000.00" in html
    target = re.search(r'data-section="target">(.*?)</table>', html, re.DOTALL).group(1)
    assert "Not available" in target and "$0.00" not in target
