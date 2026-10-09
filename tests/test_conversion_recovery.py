"""Run lifecycle hardening: validation-failure evidence, post-commit evidence failures, recovery.

Every database here is a private temporary copy; recovery must only ever read it.
"""

import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from loan_lab.conversion.legacy import (
    EvidenceIncompleteError,
    LoadRun,
    LoadRunFailedError,
    Rule,
    RunStatus,
    SourceRunFailedError,
    TargetPreconditionError,
    check_ready,
    fail_run,
    plan_conversion,
    recover_run,
    run_conversion,
    run_load,
)
from loan_lab.conversion.legacy import run
from loan_lab.conversion.legacy.loader import DATABASE_NAME, LoadFailedError, LoadStep
from loan_lab.paths import find_project_root

PROJECT_ROOT = find_project_root(Path(__file__))
SAMPLE_DIR = PROJECT_ROOT / "sample_data" / "legacy"
SOURCE_FILES = (
    "borrowers.csv", "applications.csv", "application_parties.csv", "extract_control.csv"
)


class Workspace:
    def __init__(self, root: Path) -> None:
        self.source = root / "source"
        shutil.copytree(SAMPLE_DIR, self.source)
        self.databases = root / "data" / "conversion"
        self.evidence_root = root / "output" / "conversion"

    def convert(self, run_id: str = "run-1") -> LoadRun:
        return run_conversion(
            self.source, run_id, conversion_root=self.databases,
            evidence_root=self.evidence_root, batch_size=4,
        )

    def load(self, run_id: str = "run-1") -> LoadRun:
        return run_load(
            plan_conversion(self.source), self.source, run_id, conversion_root=self.databases,
            evidence_root=self.evidence_root, batch_size=4,
        )

    def source_failure(self, run_id: str = "run-1") -> SourceRunFailedError:
        with pytest.raises(SourceRunFailedError) as raised:
            self.convert(run_id)
        return raised.value

    def recover(self, run_id: str = "run-1") -> run.RecoveryResult:
        return recover_run(run_id, evidence_root=self.evidence_root)

    def ready(self, run_id: str = "run-1") -> run.Readiness:
        return check_ready(run_id, evidence_root=self.evidence_root)

    def fail_run(self, reason: str, run_id: str = "run-1") -> None:
        fail_run(run_id, reason, evidence_root=self.evidence_root)

    def evidence(self, run_id: str = "run-1") -> Path:
        return self.evidence_root / run_id

    def database(self, run_id: str = "run-1") -> Path:
        return self.databases / run_id / DATABASE_NAME

    def manifest(self, run_id: str = "run-1") -> dict[str, Any]:
        return json.loads((self.evidence(run_id) / "manifest.json").read_text("utf-8"))

    def report(self, run_id: str = "run-1") -> dict[str, Any]:
        path = self.evidence(run_id) / "reports" / "load_result.json"
        return json.loads(path.read_text("utf-8"))

    def has_report(self, run_id: str = "run-1") -> bool:
        return (self.evidence(run_id) / "reports" / "load_result.json").exists()


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hashes(directory: Path) -> dict[str, str]:
    return {name: sha256(directory / name) for name in SOURCE_FILES if (directory / name).exists()}


def fail_json_writes(monkeypatch: pytest.MonkeyPatch, when: Any) -> None:
    real_write = run.write_json_atomic

    def write(path: Path, data: Any) -> None:
        if when(path, data):
            raise OSError(28, "No space left on device (injected)")
        real_write(path, data)

    monkeypatch.setattr(run, "write_json_atomic", write)


def crash_without_cleanup(monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    """Simulate the process dying at ``target``: no handler gets to record anything."""

    def crash(*_args: Any, **_kwargs: Any) -> Any:
        raise KeyboardInterrupt("process killed (simulated)")

    monkeypatch.setattr(run._Evidence, "fail_unexpected", lambda *_args: None)
    if target == "succeed":
        monkeypatch.setattr(run._Evidence, "succeed", crash)
    else:
        monkeypatch.setattr(run, target, crash)


def loaded_then_crashed(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    """The load committed, then the process died before any final evidence was written."""
    with monkeypatch.context() as patch:
        crash_without_cleanup(patch, "succeed")
        with pytest.raises(KeyboardInterrupt):
            ws.convert()
    manifest = ws.manifest()
    assert manifest["status"] == "LOADING"
    assert manifest["database_transaction"] == "in_progress"
    assert ws.database().is_file()


# --- run_conversion -------------------------------------------------------------------------


def test_run_conversion_loads_and_marks_the_run_ready(ws: Workspace) -> None:
    loaded = ws.convert()

    assert loaded.status is RunStatus.LOADED
    assert loaded.plan.control_sha256 == sha256(ws.source / "extract_control.csv")
    manifest = ws.manifest()
    assert manifest["status"] == "LOADED"
    assert manifest["database_transaction"] == "committed"
    assert manifest["evidence"] == {"state": "complete", "error": None, "recovered_at": None}
    assert manifest["ready_for_reconciliation"] is True
    assert manifest["validation"]["run_level_checks"] == "passed"
    assert manifest["source"]["verified"] is True
    assert ws.ready().ready, ws.ready().problems


# --- Source-validation failure --------------------------------------------------------------


def test_missing_file_fails_the_run_with_persisted_evidence(ws: Workspace) -> None:
    (ws.source / "applications.csv").unlink()
    before = source_hashes(ws.source)

    error = ws.source_failure()

    assert error.rule is Rule.RUN_01
    assert error.stage == "validation"
    assert error.evidence_directory == ws.evidence()
    manifest = ws.manifest()
    assert manifest["status"] == "FAILED"
    assert manifest["failure"]["stage"] == "validation"
    assert manifest["failure"]["step"] == "validate_source"
    assert manifest["failure"]["rule"] == "RUN-01"
    assert manifest["database_transaction"] == "not_started"
    assert manifest["load"]["outcome"] == "not_started"
    assert manifest["ready_for_reconciliation"] is False
    assert manifest["expected_target"] is None
    assert manifest["database_path"] is None
    assert manifest["finished_at"] is not None
    validation = manifest["validation"]
    assert validation["run_level_checks"] == "failed"
    assert {"rule": "RUN-01", "file": "applications.csv", "line": None,
            "message": "File is missing."} in validation["issues"]
    # The files that do exist are archived byte for byte, with their checksums.
    files = manifest["source"]["files"]
    assert files["applications.csv"]["error"] == "missing"
    assert files["applications.csv"]["source_sha256"] is None
    for name, digest in before.items():
        assert files[name]["source_sha256"] == files[name]["archived_sha256"] == digest
        assert (ws.evidence() / "source" / name).read_bytes() == (ws.source / name).read_bytes()
        assert files[name]["planned_sha256"] is None
    assert manifest["source"]["verified"] is None
    assert not (ws.evidence() / "source" / "applications.csv").exists()
    # No plan means no target database, and no load report.
    assert not ws.databases.exists()
    assert not ws.has_report()
    assert source_hashes(ws.source) == before
    assert not ws.ready().ready


def test_missing_source_directory_still_records_a_failed_run(ws: Workspace) -> None:
    shutil.rmtree(ws.source)

    error = ws.source_failure()

    assert {issue.file for issue in error.issues} == set(SOURCE_FILES)
    manifest = ws.manifest()
    assert manifest["status"] == "FAILED"
    assert all(f["error"] == "missing" for f in manifest["source"]["files"].values())
    assert list((ws.evidence() / "source").iterdir()) == []
    assert not ws.databases.exists()


def test_malformed_control_archives_its_raw_bytes(ws: Workspace) -> None:
    control = ws.source / "extract_control.csv"
    raw = (
        b"FILE_NAME,RECORD_COUNT,AMOUNT_TOTAL,EXTRACT_DATE\r\n"
        b"borrowers.csv,sixteen,,20260930\r\n"
        b"applications.csv,13,5467000.00,2026-09-30\r\n"
        b"application_parties.csv,20,,20260930,EXTRA\r\n"
    )
    control.write_bytes(raw)

    error = ws.source_failure()

    assert error.rule is Rule.RUN_06
    manifest = ws.manifest()
    assert manifest["status"] == "FAILED"
    issues = manifest["validation"]["issues"]
    assert issues and {issue["rule"] for issue in issues} == {"RUN-06"}
    assert all(issue["file"] == "extract_control.csv" for issue in issues)
    entry = manifest["source"]["files"]["extract_control.csv"]
    assert entry["source_sha256"] == entry["archived_sha256"] == hashlib.sha256(raw).hexdigest()
    assert entry["bytes"] == len(raw)
    assert entry["verified"] is False
    assert (ws.evidence() / "source" / "extract_control.csv").read_bytes() == raw
    assert control.read_bytes() == raw
    assert not ws.databases.exists()


def test_a_failed_run_id_is_never_reused(ws: Workspace) -> None:
    (ws.source / "borrowers.csv").unlink()
    ws.source_failure("run-x")
    failed = (ws.evidence("run-x") / "manifest.json").read_bytes()
    shutil.copy(SAMPLE_DIR / "borrowers.csv", ws.source / "borrowers.csv")

    with pytest.raises(TargetPreconditionError):
        ws.convert("run-x")
    with pytest.raises(TargetPreconditionError):
        ws.load("run-x")

    assert (ws.evidence("run-x") / "manifest.json").read_bytes() == failed
    assert not ws.databases.exists()
    assert ws.convert("run-y").status is RunStatus.LOADED


# --- Post-commit evidence failure -----------------------------------------------------------


def test_report_failure_after_commit_never_looks_unloaded(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        fail_json_writes(patch, lambda path, _data: path.name == "load_result.json")
        with pytest.raises(EvidenceIncompleteError) as raised:
            ws.convert()

    assert "No space left" in raised.value.error
    manifest = ws.manifest()
    assert manifest["status"] == "LOADING"
    assert manifest["database_transaction"] == "committed"
    assert manifest["evidence"]["state"] == "incomplete"
    assert "No space left" in manifest["evidence"]["error"]
    assert manifest["load"]["outcome"] == "committed"
    assert manifest["ready_for_reconciliation"] is False
    assert not ws.has_report()
    readiness = ws.ready()
    assert not readiness.ready
    assert "Status is LOADING, not LOADED." in readiness.problems
    assert "Evidence is not complete." in readiness.problems


def test_recovery_repairs_evidence_without_touching_the_database(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        fail_json_writes(patch, lambda path, _data: path.name == "load_result.json")
        with pytest.raises(EvidenceIncompleteError):
            ws.convert()
    database = ws.database()
    digest, modified = sha256(database), database.stat().st_mtime_ns
    databases_before = sorted(p.name for p in ws.databases.rglob("*"))

    result = ws.recover()

    assert result.status is RunStatus.LOADED
    assert result.database_transaction is run.TransactionState.COMMITTED
    assert result.changed is True
    assert sha256(database) == digest
    assert database.stat().st_mtime_ns == modified
    assert sorted(p.name for p in ws.databases.rglob("*")) == databases_before
    manifest = ws.manifest()
    assert manifest["status"] == "LOADED"
    assert manifest["evidence"]["state"] == "complete"
    assert manifest["evidence"]["recovered_at"] is not None
    assert manifest["ready_for_reconciliation"] is True
    assert manifest["database"]["sha256"] == digest
    report = ws.report()
    assert report["success"] is True
    assert report["recovered"]
    assert report["loaded"] == {
        "borrowers": 11, "applications": 6, "parties": 10, "requested_amount": "2281000.00"
    }
    assert ws.ready().ready, ws.ready().problems
    # Recovering a final run changes nothing.
    manifest_bytes = (ws.evidence() / "manifest.json").read_bytes()
    assert ws.recover().changed is False
    assert (ws.evidence() / "manifest.json").read_bytes() == manifest_bytes


def test_final_manifest_failure_after_commit_is_not_ready(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        fail_json_writes(
            patch,
            lambda path, data: path.name == "manifest.json" and data["status"] == "LOADED",
        )
        with pytest.raises(EvidenceIncompleteError):
            ws.convert()

    # The report was written, but the manifest still says the load is unsettled.
    assert ws.report()["success"] is True
    manifest = ws.manifest()
    assert manifest["status"] == "LOADING"
    assert manifest["database_transaction"] == "committed"
    assert manifest["evidence"]["state"] == "incomplete"
    assert not ws.ready().ready

    assert ws.recover().status is RunStatus.LOADED
    assert ws.ready().ready, ws.ready().problems


def test_all_evidence_writes_failing_after_commit_leave_loading(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    committed: list[bool] = []
    real_load = run.load_plan

    def load(*args: Any, **kwargs: Any) -> Any:
        result = real_load(*args, **kwargs)
        committed.append(True)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(run, "load_plan", load)
        fail_json_writes(patch, lambda _path, _data: bool(committed))
        with pytest.raises(EvidenceIncompleteError):
            ws.convert()

    manifest = ws.manifest()
    # Written before the load started: never VALIDATED once a load may have run.
    assert manifest["status"] == "LOADING"
    assert manifest["database_transaction"] == "in_progress"
    assert manifest["database_path"] == str(ws.database())
    assert not ws.ready().ready

    result = ws.recover()
    assert result.status is RunStatus.LOADED
    assert ws.ready().ready, ws.ready().problems


# --- Interrupted evidence writes ------------------------------------------------------------


def test_an_interrupted_json_write_keeps_the_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "manifest.json"
    run.write_json_atomic(target, {"status": "VALIDATED"})
    before = target.read_bytes()

    def interrupted(*_args: Any) -> None:
        raise OSError(5, "I/O error (injected)")

    with monkeypatch.context() as patch:
        patch.setattr(run.os, "replace", interrupted)
        with pytest.raises(OSError):
            run.write_json_atomic(target, {"status": "LOADED"})

    assert target.read_bytes() == before
    assert os.listdir(tmp_path) == ["manifest.json"]


def test_a_crash_after_commit_is_recovered_as_loaded(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded_then_crashed(ws, monkeypatch)
    assert not ws.has_report()
    assert not ws.ready().ready
    digest = sha256(ws.database())

    result = ws.recover()

    assert result.status is RunStatus.LOADED
    assert sha256(ws.database()) == digest
    assert ws.ready().ready, ws.ready().problems


def test_a_crash_before_the_database_exists_is_recovered_as_failed(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        crash_without_cleanup(patch, "load_plan")
        with pytest.raises(KeyboardInterrupt):
            ws.convert()
    assert ws.manifest()["status"] == "LOADING"

    result = ws.recover()

    assert result.status is RunStatus.FAILED
    assert result.database_transaction is run.TransactionState.NOT_COMMITTED
    manifest = ws.manifest()
    assert manifest["status"] == "FAILED"
    assert manifest["failure"]["step"] == "recovered"
    assert manifest["load"]["outcome"] == "interrupted"
    assert ws.report()["business_rows_committed"] is False
    assert not ws.databases.exists()
    assert not ws.ready().ready


def test_a_crash_during_validation_is_recovered_as_failed(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        crash_without_cleanup(patch, "plan_conversion")
        with pytest.raises(KeyboardInterrupt):
            ws.convert()
    assert ws.manifest()["status"] == "STARTED"

    result = ws.recover()

    assert result.status is RunStatus.FAILED
    manifest = ws.manifest()
    assert manifest["failure"]["stage"] == "validation"
    assert manifest["database_transaction"] == "not_started"
    assert not ws.databases.exists()


def test_a_crash_before_loading_is_recovered_as_failed(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        crash_without_cleanup(patch, "_archive_sources")
        with pytest.raises(KeyboardInterrupt):
            ws.load()
    assert ws.manifest()["status"] == "VALIDATED"

    result = ws.recover()

    assert result.status is RunStatus.FAILED
    assert result.database_transaction is run.TransactionState.NOT_STARTED
    assert ws.manifest()["failure"]["stage"] == "load"
    assert not ws.databases.exists()


def test_unreadable_manifest_is_reported_not_guessed(ws: Workspace) -> None:
    ws.convert()
    (ws.evidence() / "manifest.json").write_bytes(b"{truncated")

    readiness = ws.ready()
    assert not readiness.ready
    with pytest.raises(run.EvidenceUnreadableError):
        ws.recover()


# --- Unverifiable commit outcomes -----------------------------------------------------------


def assert_unknown_then_formally_failed(ws: Workspace) -> None:
    database = ws.database()
    digest = sha256(database) if database.is_file() else None

    result = ws.recover()

    assert result.status is RunStatus.UNKNOWN
    assert result.database_transaction is run.TransactionState.UNKNOWN
    manifest = ws.manifest()
    assert manifest["status"] == "UNKNOWN"
    assert manifest["database_transaction"] == "unknown"
    assert manifest["evidence"]["state"] == "unverified"
    assert manifest["load"]["outcome"] == "unknown"
    assert manifest["ready_for_reconciliation"] is False
    report = ws.report()
    assert report["success"] is None
    assert report["status"] == "UNKNOWN"
    assert report["business_rows_committed"] is None
    assert not ws.ready().ready
    if digest is not None:
        assert sha256(database) == digest

    ws.fail_run("Outcome could not be verified; abandoned by the operator.")

    manifest = ws.manifest()
    assert manifest["status"] == "FAILED"
    assert manifest["failure"]["step"] == "formally_failed"
    assert manifest["database_transaction"] == "unknown"
    assert not ws.ready().ready
    assert ws.recover().changed is False
    with pytest.raises(ValueError, match="already FAILED"):
        ws.fail_run("again")
    if digest is not None:
        assert sha256(database) == digest


def test_leftover_journal_is_unknown(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    loaded_then_crashed(ws, monkeypatch)
    journal = ws.database().with_name(DATABASE_NAME + "-journal")
    journal.write_bytes(b"\x00" * 512)

    assert_unknown_then_formally_failed(ws)
    assert journal.read_bytes() == b"\x00" * 512


def test_unreadable_database_is_unknown(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    loaded_then_crashed(ws, monkeypatch)
    ws.database().write_bytes(b"this is not an SQLite database" * 100)

    assert_unknown_then_formally_failed(ws)


def test_unexpected_row_counts_are_unknown(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded_then_crashed(ws, monkeypatch)
    with closing(sqlite3.connect(ws.database())) as conn:
        conn.execute("DELETE FROM application_party WHERE id = (SELECT max(id) FROM application_party)")
        conn.commit()

    assert_unknown_then_formally_failed(ws)


def test_committed_database_that_went_missing_is_unknown(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        fail_json_writes(patch, lambda path, _data: path.name == "load_result.json")
        with pytest.raises(EvidenceIncompleteError):
            ws.convert()
    ws.database().unlink()

    assert_unknown_then_formally_failed(ws)


def test_committed_run_with_an_empty_database_is_unknown(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        fail_json_writes(patch, lambda path, _data: path.name == "load_result.json")
        with pytest.raises(EvidenceIncompleteError):
            ws.convert()
    with closing(sqlite3.connect(ws.database())) as conn:
        for table in ("application_party", "loan_application", "borrower"):
            conn.execute(f"DELETE FROM {table}")  # noqa: S608
        conn.commit()

    assert_unknown_then_formally_failed(ws)


def test_in_progress_run_with_an_empty_database_did_not_commit(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded_then_crashed(ws, monkeypatch)
    with closing(sqlite3.connect(ws.database())) as conn:
        for table in ("application_party", "loan_application", "borrower"):
            conn.execute(f"DELETE FROM {table}")  # noqa: S608
        conn.commit()

    result = ws.recover()

    assert result.status is RunStatus.FAILED
    assert result.database_transaction is run.TransactionState.NOT_COMMITTED
    assert ws.report()["business_rows_committed"] is False


def test_reported_rollback_that_the_database_contradicts_is_unknown(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_load = run.load_plan

    def load(plan: Any, run_id: str, **kwargs: Any) -> Any:
        result = real_load(plan, run_id, **kwargs)
        raise LoadFailedError(
            run_id, result.database_path, result.counts, result.requested_amount,
            RuntimeError("commit reported failure (injected)"), LoadStep.COMMIT, True,
        )

    with monkeypatch.context() as patch:
        patch.setattr(run, "load_plan", load)
        with pytest.raises(LoadRunFailedError) as raised:
            ws.convert()

    assert raised.value.status is RunStatus.UNKNOWN
    assert "UNKNOWN at stage load (commit)" in str(raised.value)
    manifest = ws.manifest()
    assert manifest["status"] == "UNKNOWN"
    assert manifest["database_transaction"] == "unknown"
    assert manifest["ready_for_reconciliation"] is False
    assert ws.report()["business_rows_committed"] is None
    assert not ws.ready().ready
    # Recovery can still verify the commit read-only: the rows are exactly the plan's.
    digest = sha256(ws.database())
    assert ws.recover().status is RunStatus.LOADED
    assert sha256(ws.database()) == digest
    assert ws.ready().ready, ws.ready().problems


def test_unknown_run_can_be_failed_without_recovery(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded_then_crashed(ws, monkeypatch)
    digest = sha256(ws.database())

    ws.fail_run("Operator abandoned the run.")

    manifest = ws.manifest()
    assert manifest["status"] == "FAILED"
    assert manifest["failure"]["reason"] == "Operator abandoned the run."
    assert sha256(ws.database()) == digest
    assert not ws.ready().ready


# --- Readiness and repository hygiene -------------------------------------------------------


def test_a_changed_database_is_no_longer_ready(ws: Workspace) -> None:
    ws.convert()
    assert ws.ready().ready
    with ws.database().open("ab") as handle:
        handle.write(b"\x00")

    readiness = ws.ready()

    assert readiness.problems == (
        "The conversion database no longer matches its recorded checksum.",
    )


def test_a_missing_report_is_not_ready(ws: Workspace) -> None:
    ws.convert()
    (ws.evidence() / "reports" / "load_result.json").unlink()

    assert not ws.ready().ready


def test_output_and_data_are_excluded_from_git() -> None:
    lines = {line.strip() for line in (PROJECT_ROOT / ".gitignore").read_text("utf-8").splitlines()}

    assert "output/" in lines
    assert "data/" in lines
