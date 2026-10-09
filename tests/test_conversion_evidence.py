"""Durable per-run evidence around the Phase 2 load (spec sections 2.2, 11 and 13)."""

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, event

from loan_lab.conversion.legacy import (
    ConversionPlan,
    LoadRun,
    LoadRunFailedError,
    Rule,
    RunStatus,
    SourceChangedError,
    TargetPreconditionError,
    plan_conversion,
    run_load,
)
from loan_lab.conversion.legacy import loader, run
from loan_lab.conversion.legacy.loader import DATABASE_NAME, LoadFailedError
from loan_lab.paths import default_evidence_root, find_project_root

SAMPLE_DIR = find_project_root(Path(__file__)) / "sample_data" / "legacy"
SOURCE_FILES = (
    "borrowers.csv", "applications.csv", "application_parties.csv", "extract_control.csv"
)
LOS_TABLES = ("borrower", "loan_application", "application_party")
EXPECTED = {"borrowers": 11, "applications": 6, "parties": 10, "requested_amount": "2281000.00"}
NOTHING_LOADED = {"borrowers": 0, "applications": 0, "parties": 0, "requested_amount": "0.00"}


class Workspace:
    """A private copy of the sample extract plus separate database and evidence roots."""

    def __init__(self, root: Path) -> None:
        self.source = root / "source"
        shutil.copytree(SAMPLE_DIR, self.source)
        self.databases = root / "data" / "conversion"
        self.evidence_root = root / "output" / "conversion"
        self.plan = plan_conversion(self.source)

    def run(self, run_id: str = "run-1", plan: ConversionPlan | None = None) -> LoadRun:
        return run_load(
            plan or self.plan,
            self.source,
            run_id,
            conversion_root=self.databases,
            evidence_root=self.evidence_root,
            batch_size=4,
        )

    def fail(self, run_id: str = "run-1") -> LoadRunFailedError:
        with pytest.raises(LoadRunFailedError) as raised:
            self.run(run_id)
        return raised.value

    def evidence(self, run_id: str = "run-1") -> Path:
        return self.evidence_root / run_id

    def database(self, run_id: str = "run-1") -> Path:
        return self.databases / run_id / DATABASE_NAME

    def manifest(self, run_id: str = "run-1") -> dict[str, Any]:
        return json.loads((self.evidence(run_id) / "manifest.json").read_text("utf-8"))

    def report(self, run_id: str = "run-1") -> dict[str, Any]:
        path = self.evidence(run_id) / "reports" / "load_result.json"
        return json.loads(path.read_text("utf-8"))


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def files_under(directory: Path) -> list[str]:
    return sorted(p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file())


def table_counts(database: Path) -> dict[str, int]:
    with closing(sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)) as conn:
        tables = {name for (name,) in conn.execute("SELECT name FROM sqlite_master")}
        return {
            name: conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]  # noqa: S608
            for name in LOS_TABLES
            if name in tables
        }


def utc(timestamp: str) -> datetime:
    assert timestamp.endswith("Z")
    return datetime.fromisoformat(timestamp)


def inject_statement_failure(monkeypatch: pytest.MonkeyPatch, on: Callable[[str], bool]) -> None:
    real_create = loader.create_db_engine

    def create(url: str, **kwargs: Any) -> Engine:
        engine = real_create(url, **kwargs)

        def fail(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
            if on(statement):
                raise RuntimeError("injected failure")

        event.listen(engine, "before_cursor_execute", fail)
        return engine

    monkeypatch.setattr(loader, "create_db_engine", create)


def inject_commit_failure(monkeypatch: pytest.MonkeyPatch, commit_number: int) -> None:
    real_create = loader.create_db_engine
    commits: list[int] = []

    def create(url: str, **kwargs: Any) -> Engine:
        engine = real_create(url, **kwargs)

        def fail(_conn: Any) -> None:
            commits.append(1)
            if len(commits) == commit_number:
                raise RuntimeError("injected commit failure")

        event.listen(engine, "commit", fail)
        return engine

    monkeypatch.setattr(loader, "create_db_engine", create)


# --- Successful run ------------------------------------------------------------------------


def test_successful_run_produces_the_evidence_package(ws: Workspace) -> None:
    result = ws.run()

    assert result.status is RunStatus.LOADED
    assert result.evidence_directory == ws.evidence()
    assert files_under(ws.evidence()) == [
        "manifest.json",
        "reports/load_result.json",
        *(f"source/{name}" for name in sorted(SOURCE_FILES)),
    ]
    assert files_under(ws.databases) == [f"run-1/{DATABASE_NAME}"]


def test_archived_sources_are_exact_verified_copies(ws: Workspace) -> None:
    result = ws.run()

    files = ws.manifest()["source"]["files"]
    assert ws.manifest()["source"]["verified"] is True
    assert ws.manifest()["source"]["directory"] == str(ws.source.resolve())
    for name in SOURCE_FILES:
        archived = ws.evidence() / "source" / name
        assert archived.read_bytes() == (SAMPLE_DIR / name).read_bytes()
        digest = sha256(SAMPLE_DIR / name)
        assert files[name] == {
            "planned_sha256": digest,
            "source_sha256": digest,
            "archived_sha256": digest,
            "bytes": archived.stat().st_size,
            "verified": True,
        }
        assert ws.plan.source_checksums[name] == digest
    assert all(source.verified for source in result.sources)


def test_manifest_records_the_run(ws: Workspace) -> None:
    ws.run()
    manifest = ws.manifest()

    assert manifest["run_id"] == "run-1"
    assert manifest["status"] == "LOADED"
    assert manifest["failure"] is None
    assert utc(manifest["started_at"]) <= utc(manifest["updated_at"])
    assert utc(manifest["finished_at"]) <= utc(manifest["updated_at"])
    assert manifest["expected_target"] == EXPECTED
    assert manifest["load"] == {"outcome": "committed", "report": "reports/load_result.json"}
    assert manifest["reconciliation"] is None
    assert manifest["release"] is None
    assert manifest["database"]["path"] == str(ws.database())
    assert manifest["database"]["sha256"] == sha256(ws.database())

    validation = manifest["validation"]
    assert validation["extract_date"] == "20260930"
    assert validation["control"] == {
        "record_counts": {
            "borrowers.csv": 16, "applications.csv": 13, "application_parties.csv": 20,
        },
        "amount_total": "5467000.00",
        "amount_total_verified": True,
    }
    assert validation["rows"]["borrowers.csv"] == {
        "read": 16, "eligible": 11, "excluded": 1, "rejected": 4,
        "excluded_dependent": 0, "rejected_dependent": 0,
    }
    assert validation["rows"]["application_parties.csv"] == {
        "read": 20, "eligible": 10, "excluded": 3, "rejected": 7,
        "excluded_dependent": 2, "rejected_dependent": 3,
    }
    assert validation["requested_amount"] == {
        "eligible": "2281000.00", "excluded": "291000.00", "rejected": "2895000.00",
        "unparseable": 0,
    }
    assert validation["rejections"] == 18
    assert validation["customers_without_applications"] == ["00010008", "00010009", "00010015"]


def test_load_report_holds_actual_counts_and_exact_amount(ws: Workspace) -> None:
    result = ws.run()
    report = ws.report()

    assert report["success"] is True
    assert report["status"] == "LOADED"
    assert report["expected"] == EXPECTED
    assert report["loaded"] == EXPECTED
    assert report["inserted"] == {"borrowers": 11, "applications": 6, "parties": 10}
    assert report["transactions"] == {"schema": "committed", "load": "committed"}
    assert report["business_rows_committed"] is True
    assert report["failure"] is None
    assert report["database"]["sha256"] == sha256(ws.database())
    # The amount is a decimal string, never a JSON number that a reader could parse as a float.
    raw = (ws.evidence() / "reports" / "load_result.json").read_text("utf-8")
    assert '"requested_amount": "2281000.00"' in raw
    assert result.database.requested_amount == Decimal("2281000.00")
    assert table_counts(ws.database()) == {
        "borrower": 11, "loan_application": 6, "application_party": 10,
    }


def test_manifest_says_loading_before_the_load_starts(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []
    real_load = run.load_plan

    def observe(*args: Any, **kwargs: Any) -> Any:
        seen.append(ws.manifest())
        return real_load(*args, **kwargs)

    monkeypatch.setattr(run, "load_plan", observe)
    ws.run()

    # Never VALIDATED once a load could have begun, so a crash cannot hide a committed load.
    assert seen[0]["status"] == "LOADING"
    assert seen[0]["database_transaction"] == "in_progress"
    assert seen[0]["database_path"] == str(ws.database())
    assert seen[0]["ready_for_reconciliation"] is False
    assert seen[0]["source"]["verified"] is True
    assert seen[0]["load"]["outcome"] == "not_started"
    assert seen[0]["finished_at"] is None
    assert ws.manifest()["status"] == "LOADED"
    assert ws.manifest()["database_transaction"] == "committed"
    assert ws.manifest()["evidence"]["state"] == "complete"
    assert ws.manifest()["ready_for_reconciliation"] is True


def test_run_uses_the_given_plan_and_never_replans(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from loan_lab.conversion.legacy import planner, source

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the run must not re-read or re-plan the extract")

    monkeypatch.setattr(planner, "plan_conversion", forbidden)
    monkeypatch.setattr(planner, "build_plan", forbidden)
    monkeypatch.setattr(source, "read_extract", forbidden)

    assert ws.run().result.counts.applications == 6


def test_original_source_files_are_unchanged(tmp_path: Path) -> None:
    before = {name: sha256(SAMPLE_DIR / name) for name in SOURCE_FILES}
    stamps = {name: (SAMPLE_DIR / name).stat().st_mtime_ns for name in SOURCE_FILES}
    plan = plan_conversion(SAMPLE_DIR)

    run_load(plan, SAMPLE_DIR, "run-1", conversion_root=tmp_path / "data",
             evidence_root=tmp_path / "output")

    assert {name: sha256(SAMPLE_DIR / name) for name in SOURCE_FILES} == before
    assert {name: (SAMPLE_DIR / name).stat().st_mtime_ns for name in SOURCE_FILES} == stamps
    assert sorted(p.name for p in SAMPLE_DIR.iterdir()) == sorted(SOURCE_FILES)


def test_default_evidence_root_is_git_ignored_output() -> None:
    assert default_evidence_root() == find_project_root(Path(__file__)) / "output" / "conversion"


# --- Source changed after planning (RUN-08) ------------------------------------------------


@pytest.mark.parametrize("name", SOURCE_FILES)
def test_source_modified_after_planning_blocks_loading(ws: Workspace, name: str) -> None:
    changed = ws.source / name
    changed.write_bytes(changed.read_bytes() + b"\n")
    modified = changed.read_bytes()

    failure = ws.fail()

    assert failure.rule is Rule.RUN_08
    assert failure.step == "verify_source"
    assert isinstance(failure.__cause__, SourceChangedError)
    assert [issue.file for issue in failure.__cause__.issues] == [name]
    assert not ws.databases.exists()
    assert changed.read_bytes() == modified
    # The archive keeps what was actually found, so the difference can be examined.
    assert (ws.evidence() / "source" / name).read_bytes() == modified

    manifest = ws.manifest()
    assert manifest["status"] == "FAILED"
    assert manifest["failure"]["stage"] == "load"
    assert manifest["failure"]["step"] == "verify_source"
    assert manifest["failure"]["rule"] == "RUN-08"
    assert name in manifest["failure"]["reason"]
    assert manifest["source"]["verified"] is False
    assert manifest["source"]["files"][name]["verified"] is False
    assert manifest["source"]["files"][name]["source_sha256"] == sha256(changed)
    assert manifest["database"] is None
    assert manifest["load"]["outcome"] == "refused"

    report = ws.report()
    assert report["success"] is False
    assert report["loaded"] is None
    assert report["business_rows_committed"] is False
    assert report["transactions"] == {"schema": "not_started", "load": "not_started"}


def test_deleted_source_file_blocks_loading(ws: Workspace) -> None:
    (ws.source / "application_parties.csv").unlink()

    failure = ws.fail()

    assert failure.rule is Rule.RUN_08
    assert "could not be read" in failure.reason
    assert ws.manifest()["source"]["files"]["application_parties.csv"]["source_sha256"] is None
    assert not ws.databases.exists()


def test_identical_rewrite_is_not_a_change(ws: Workspace) -> None:
    path = ws.source / "borrowers.csv"
    path.write_bytes(path.read_bytes())

    assert ws.run().status is RunStatus.LOADED


# --- Failures keep accurate evidence -------------------------------------------------------


def test_schema_creation_failure_is_recorded(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    inject_statement_failure(monkeypatch, lambda sql: "CREATE TABLE application_party" in sql)

    failure = ws.fail()

    assert failure.step == "create_schema"
    assert failure.rule is None
    assert isinstance(failure.__cause__, LoadFailedError)
    manifest, report = ws.manifest(), ws.report()
    assert manifest["status"] == "FAILED"
    assert manifest["failure"] == {
        "stage": "load", "step": "create_schema", "rule": None,
        "reason": "RuntimeError: injected failure",
    }
    assert manifest["load"]["outcome"] == "rolled_back"
    assert manifest["database"]["exists"] is True
    assert manifest["database"]["tables"] == []
    assert manifest["database"]["sha256"] == sha256(ws.database())
    assert report["transactions"] == {"schema": "rolled_back", "load": "not_started"}
    assert report["loaded"] is None
    assert report["business_rows_committed"] is False
    assert table_counts(ws.database()) == {}


def test_insertion_failure_is_recorded(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    inject_statement_failure(monkeypatch, lambda sql: sql.startswith("INSERT INTO application_party"))

    failure = ws.fail()

    assert failure.step == "insert_parties"
    report = ws.report()
    assert report["success"] is False
    assert report["status"] == "FAILED"
    assert report["expected"] == EXPECTED
    assert report["loaded"] == NOTHING_LOADED
    assert report["inserted"] is None
    assert report["transactions"] == {"schema": "committed", "load": "rolled_back"}
    assert report["business_rows_committed"] is False
    assert report["failure"]["step"] == "insert_parties"
    assert ws.manifest()["status"] == "FAILED"
    assert ws.manifest()["database"]["tables"] == [
        "application_party", "borrower", "collateral", "collateral_pledge", "lien",
        "loan_application",
    ]
    assert table_counts(ws.database()) == dict.fromkeys(LOS_TABLES, 0)


def test_commit_failure_is_recorded(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    inject_commit_failure(monkeypatch, commit_number=2)

    failure = ws.fail()

    assert failure.step == "commit"
    assert "injected commit failure" in failure.reason
    report = ws.report()
    assert report["transactions"] == {"schema": "committed", "load": "rolled_back"}
    assert report["loaded"] == NOTHING_LOADED
    assert report["business_rows_committed"] is False
    assert ws.manifest()["status"] == "FAILED"
    assert ws.manifest()["failure"]["step"] == "commit"
    assert table_counts(ws.database()) == dict.fromkeys(LOS_TABLES, 0)


def test_unexpected_error_still_leaves_a_failed_record(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(*_args: Any, **_kwargs: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(run, "load_plan", interrupted)

    with pytest.raises(KeyboardInterrupt):
        ws.run()

    # The database was inspected: it was never created, so nothing can have committed.
    assert ws.manifest()["status"] == "FAILED"
    assert ws.manifest()["failure"]["step"] == "load"
    assert ws.manifest()["load"]["outcome"] == "interrupted"
    assert ws.manifest()["database_transaction"] == "not_committed"
    assert ws.report()["business_rows_committed"] is False


# --- Existing-run protection ---------------------------------------------------------------


def test_existing_evidence_directory_is_refused_without_writing(ws: Workspace) -> None:
    ws.evidence().mkdir(parents=True)

    with pytest.raises(TargetPreconditionError) as raised:
        ws.run()

    assert raised.value.issue.rule is Rule.RUN_07
    assert list(ws.evidence().iterdir()) == []
    assert not ws.databases.exists()


def test_completed_run_is_never_overwritten(ws: Workspace) -> None:
    ws.run()
    before = {path: sha256(ws.evidence() / path) for path in files_under(ws.evidence())}
    database = sha256(ws.database())

    with pytest.raises(TargetPreconditionError):
        ws.run()

    assert {path: sha256(ws.evidence() / path) for path in files_under(ws.evidence())} == before
    assert sha256(ws.database()) == database


def test_failed_run_is_never_reused(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    inject_statement_failure(monkeypatch, lambda sql: sql.startswith("INSERT INTO borrower"))
    ws.fail()
    monkeypatch.undo()
    before = ws.manifest()

    with pytest.raises(TargetPreconditionError):
        ws.run()

    assert ws.manifest() == before
    assert ws.run("run-2").status is RunStatus.LOADED


def test_existing_database_directory_fails_the_run_with_evidence(ws: Workspace) -> None:
    (ws.databases / "run-1").mkdir(parents=True)
    (ws.databases / "run-1" / "other.txt").write_text("not ours", "utf-8")

    failure = ws.fail()

    assert failure.rule is Rule.RUN_07
    assert failure.step == "reserve_database"
    assert ws.manifest()["status"] == "FAILED"
    assert ws.manifest()["failure"]["rule"] == "RUN-07"
    assert ws.manifest()["database"] is None
    assert ws.report()["business_rows_committed"] is False
    assert files_under(ws.databases / "run-1") == ["other.txt"]


def test_invalid_run_id_creates_nothing(ws: Workspace) -> None:
    with pytest.raises(ValueError, match="Invalid run ID"):
        ws.run("../escape")

    assert not ws.evidence_root.exists()
    assert not ws.databases.exists()


# --- Atomic evidence files -----------------------------------------------------------------


def test_no_temporary_files_are_left(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    ws.run()
    inject_statement_failure(monkeypatch, lambda sql: sql.startswith("INSERT INTO borrower"))
    ws.fail("run-2")

    for run_id in ("run-1", "run-2"):
        assert not [p for p in ws.evidence(run_id).rglob("*") if p.name.endswith(".tmp")]


def test_interrupted_write_keeps_the_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "manifest.json"
    run.write_json_atomic(target, {"status": "VALIDATED"})

    def disk_full(_fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(run.os, "fsync", disk_full)
    with pytest.raises(OSError, match="disk full"):
        run.write_json_atomic(target, {"status": "LOADED"})

    assert json.loads(target.read_text("utf-8")) == {"status": "VALIDATED"}
    assert [p.name for p in tmp_path.iterdir()] == ["manifest.json"]
