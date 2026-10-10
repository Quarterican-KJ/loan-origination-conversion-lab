"""Phase 3: independent reconciliation (spec section 12).

Target corruption is injected *during* the load, before the database checksum is recorded, so it
looks exactly like a loader or mapping defect. Corrupting the database afterwards is a different
case: reconciliation must refuse it because the checksum no longer matches.
"""

import hashlib
import json
import os
import shutil
import sqlite3
from collections.abc import Callable
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from loan_lab.conversion.legacy import (
    EvidenceIncompleteError,
    ReconciliationRefusedError,
    ReconciliationResult,
    ReconciliationRule,
    RunStatus,
    check_ready,
    contract,
    fail_run,
    recover_run,
    reconcile_run,
    run_conversion,
)
from loan_lab.conversion.legacy import reconcile, reconcile_cli, run
from loan_lab.conversion.legacy.loader import DATABASE_NAME
from loan_lab.models.enums import ApplicationStatus
from loan_lab.paths import find_project_root

SAMPLE_DIR = find_project_root(Path(__file__)) / "sample_data" / "legacy"
RC = ReconciliationRule
ALL_RULES = [f"RC-{n:02d}" for n in range(1, 11)]


class Workspace:
    def __init__(self, root: Path) -> None:
        self.source = root / "source"
        shutil.copytree(SAMPLE_DIR, self.source)
        self.databases = root / "data" / "conversion"
        self.evidence_root = root / "output" / "conversion"

    def convert(self, run_id: str = "run-1") -> None:
        run_conversion(
            self.source, run_id, conversion_root=self.databases,
            evidence_root=self.evidence_root, batch_size=4,
        )

    def reconcile(self, run_id: str = "run-1") -> ReconciliationResult:
        return reconcile_run(run_id, evidence_root=self.evidence_root)

    def refused(self, run_id: str = "run-1") -> ReconciliationRefusedError:
        with pytest.raises(ReconciliationRefusedError) as raised:
            self.reconcile(run_id)
        return raised.value

    def evidence(self, run_id: str = "run-1") -> Path:
        return self.evidence_root / run_id

    def database(self, run_id: str = "run-1") -> Path:
        return self.databases / run_id / DATABASE_NAME

    def manifest(self, run_id: str = "run-1") -> dict[str, Any]:
        return json.loads((self.evidence(run_id) / "manifest.json").read_text("utf-8"))

    def report_path(self, run_id: str = "run-1") -> Path:
        return self.evidence(run_id) / "reports" / "reconciliation.json"

    def report(self, run_id: str = "run-1") -> dict[str, Any]:
        return json.loads(self.report_path(run_id).read_text("utf-8"))

    def snapshot(self, run_id: str = "run-1") -> dict[str, bytes]:
        """Every evidence file and the database, byte for byte."""
        files = {
            p.relative_to(self.evidence(run_id)).as_posix(): p.read_bytes()
            for p in self.evidence(run_id).rglob("*") if p.is_file()
        }
        files["<database>"] = self.database(run_id).read_bytes()
        return files


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def corrupt_during_load(
    monkeypatch: pytest.MonkeyPatch, change: Callable[[sqlite3.Connection], None]
) -> None:
    """Commit ``change`` to the database right after the real load, as a defective loader would."""
    real_load = run.load_plan

    def load(*args: Any, **kwargs: Any) -> Any:
        result = real_load(*args, **kwargs)
        with closing(sqlite3.connect(result.database_path)) as conn:
            change(conn)
            conn.commit()
        return result

    monkeypatch.setattr(run, "load_plan", load)


def loaded_with(ws: Workspace, monkeypatch: pytest.MonkeyPatch, *statements: str) -> None:
    with monkeypatch.context() as patch:
        corrupt_during_load(patch, lambda conn: [conn.execute(s) for s in statements])
        ws.convert()
    assert ws.manifest()["status"] == "LOADED"
    assert check_ready("run-1", evidence_root=ws.evidence_root).ready


def application_id(appl_no: str) -> str:
    return f"(SELECT id FROM loan_application WHERE source_system_id = '{appl_no}')"


def borrower_id(cust_no: str) -> str:
    return f"(SELECT id FROM borrower WHERE source_system_id = '{cust_no}')"


def found(result: ReconciliationResult, rule: str, check: str | None = None) -> list[Any]:
    return [
        d for d in result.discrepancies
        if d.rule == rule and (check is None or d.check == check)
    ]


def failed(result: ReconciliationResult) -> set[str]:
    return {str(rule) for rule in result.failed_rules}


def assert_failed_and_preserved(ws: Workspace, result: ReconciliationResult, before: dict) -> None:
    """A mismatch fails the run, blocks release, and changes no existing evidence."""
    assert result.status is RunStatus.FAILED
    manifest = ws.manifest()
    assert manifest["status"] == "FAILED"
    assert manifest["failure"]["stage"] == "reconciliation"
    assert manifest["failure"]["rule"] in ALL_RULES
    assert manifest["reconciliation"]["result"] == "failed"
    assert manifest["reconciliation"]["release_review"] == "blocked"
    assert manifest["release"] is None
    assert manifest["database_transaction"] == "committed"
    after = ws.snapshot()
    for name, data in before.items():
        if name != "manifest.json":
            assert after[name] == data, name
    assert ws.report()["result"] == "FAIL"
    # A failed run is never reconciled again, recovered, or formally failed a second time.
    ws.refused()
    assert recover_run("run-1", evidence_root=ws.evidence_root).changed is False


# --- Clean run -------------------------------------------------------------------------------


def test_clean_sample_run_passes_every_rule(ws: Workspace) -> None:
    ws.convert()
    before = ws.snapshot()

    result = ws.reconcile()

    assert result.passed, result.discrepancies
    assert result.status is RunStatus.RECONCILED
    assert [str(r.rule) for r in result.rules] == ALL_RULES
    assert all(r.passed and r.checked > 0 for r in result.rules)
    assert result.discrepancies == ()
    report = ws.report()
    assert report["result"] == "PASS"
    assert report["status"] == "RECONCILED"
    assert report["totals"]["rows"] == {
        "borrowers.csv": {"read": 16, "loaded": 11, "excluded": 1, "rejected": 4},
        "applications.csv": {"read": 13, "loaded": 6, "excluded": 2, "rejected": 5},
        "application_parties.csv": {"read": 20, "loaded": 10, "excluded": 3, "rejected": 7},
    }
    assert report["totals"]["requested_amount"] == {
        "source_total": "5467000.00",
        "control_total": "5467000.00",
        "loaded": "2281000.00",
        "excluded": "291000.00",
        "rejected": "2895000.00",
        "unparseable": 0,
        "target_total": "2281000.00",
    }
    assert report["totals"]["target"] == {"borrowers": 11, "applications": 6, "parties": 10}
    distributions = report["distributions"]
    assert distributions["loan_product"]["actual"] == {
        "commercial_real_estate": 1, "commercial_term": 2, "consumer_auto": 1,
        "home_equity": 1, "residential_mortgage": 1,
    }
    assert distributions["status"]["actual"] == {
        "approved": 1, "declined": 1, "draft": 1, "in_review": 2, "submitted": 1,
    }
    assert distributions["borrower_type"]["actual"] == {"business": 3, "individual": 8}
    assert distributions["role"]["actual"] == {
        "co_borrower": 1, "guarantor": 3, "primary_borrower": 6,
    }
    assert [c["cust_no"] for c in report["customers_without_applications"]] == [
        "00010008", "00010009", "00010015",
    ]
    assert report["discrepancies"] == []

    manifest = ws.manifest()
    assert manifest["status"] == "RECONCILED"
    assert manifest["failure"] is None
    assert manifest["reconciliation"]["result"] == "passed"
    assert manifest["reconciliation"]["release_review"] == "awaiting_approval"
    assert manifest["reconciliation"]["report"] == "reports/reconciliation.json"
    assert manifest["reconciliation"]["database_sha256"] == manifest["database"]["sha256"]
    # Never released or declined automatically.
    assert manifest["release"] is None
    assert "release" in report and "Not released" in report["release"]
    # Only the manifest changed and one report was added; the database was only read.
    after = ws.snapshot()
    assert set(after) == set(before) | {"reports/reconciliation.json"}
    assert all(after[name] == data for name, data in before.items() if name != "manifest.json")
    assert not ws.database().with_name(DATABASE_NAME + "-journal").exists()


def test_relationships_are_verified_from_source_to_target(ws: Workspace) -> None:
    ws.convert()

    relationships = ws.reconcile().report["relationships"]

    assert sorted(relationships) == [f"000050010{n}" for n in range(1, 7)]
    assert relationships["0000500101"]["expected"] == [
        ["00010001", "primary_borrower"], ["00010002", "guarantor"],
    ]
    # The signer on 0000500104 is excluded, so it is not an expected relationship.
    assert relationships["0000500104"]["expected"] == [
        ["00010005", "primary_borrower"], ["00010006", "guarantor"],
    ]
    assert relationships["0000500103"]["expected"] == [
        ["00010003", "primary_borrower"], ["00010004", "co_borrower"],
    ]
    for pair in relationships.values():
        assert pair["actual"] == pair["expected"]


def test_reconciled_run_is_settled(ws: Workspace) -> None:
    ws.convert()
    ws.reconcile()
    before = ws.snapshot()

    assert "Status is RECONCILED, not LOADED." in ws.refused().problems
    assert recover_run("run-1", evidence_root=ws.evidence_root).changed is False
    with pytest.raises(ValueError, match="already RECONCILED"):
        fail_run("run-1", "no", evidence_root=ws.evidence_root)
    assert ws.snapshot() == before


# --- Independent transformations -------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [("006500", "6.5000"), ("007125", "7.1250"), ("011990", "11.9900"),
     ("000000", "0.0000"), ("999999", "999.9990")],
)
def test_rate_is_recomputed_exactly(text: str, expected: str) -> None:
    value = reconcile.expected_rate(text)
    assert isinstance(value, Decimal)
    assert value == Decimal(expected)
    assert str(value) == expected


@pytest.mark.parametrize("text", ["6.875", "0.06875", "6875", "6.875%", " 06500"])
def test_undocumented_rate_notations_are_not_converted(text: str) -> None:
    with pytest.raises(reconcile.Unconvertible):
        reconcile.expected_rate(text)


def test_names_and_amounts_follow_section_7() -> None:
    individual = {
        "CUST_TYPE": "I", "BUSINESS_NAME": "", "FIRST_NAME": "Grace  ",
        "LAST_NAME": "  Haverford ", "MIDDLE_INIT": "",
    }
    assert reconcile.expected_legal_name(individual) == "Grace Haverford"
    assert reconcile.expected_legal_name({**individual, "MIDDLE_INIT": "r"}) == "Grace R. Haverford"
    business = {**individual, "CUST_TYPE": "B", "BUSINESS_NAME": " Cedar  Hollow LLC "}
    assert reconcile.expected_legal_name(business) == "Cedar Hollow LLC"
    with pytest.raises(reconcile.Unconvertible):
        reconcile.expected_legal_name({**individual, "CUST_TYPE": "T"})
    assert reconcile.expected_amount("1250000.00") == Decimal("1250000.00")
    with pytest.raises(reconcile.Unconvertible):
        reconcile.expected_amount("1,250,000.00")
    assert reconcile.stored_decimal(125000000, 2) == Decimal("1250000.00")
    assert reconcile.stored_decimal(1250000.0, 2) is None


# --- Corruption that counts and totals cannot see ---------------------------------------------


def test_swapped_amounts_with_matching_totals_are_detected(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded_with(
        ws, monkeypatch,
        f"UPDATE loan_application SET requested_amount = 35000000 WHERE id = {application_id('0000500102')}",
        f"UPDATE loan_application SET requested_amount = 18000000 WHERE id = {application_id('0000500104')}",
    )
    before = ws.snapshot()

    result = ws.reconcile()

    assert failed(result) == {"RC-07"}
    by_key = {d.source_key: d for d in found(result, "RC-07", "field_mismatch")}
    assert set(by_key) == {"0000500102", "0000500104"}
    first = by_key["0000500102"]
    assert (first.file, first.line, first.field) == ("applications.csv", 3, "requested_amount")
    assert (first.expected, first.actual) == ("180000.00", "350000.00")
    assert first.target_table == "loan_application" and first.target_id is not None
    assert (by_key["0000500104"].expected, by_key["0000500104"].actual) == (
        "350000.00", "180000.00",
    )
    totals = result.report["totals"]
    assert totals["requested_amount"]["target_total"] == "2281000.00"
    assert totals["target"] == {"borrowers": 11, "applications": 6, "parties": 10}
    assert_failed_and_preserved(ws, result, before)


def test_incorrect_rate_is_detected(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    loaded_with(
        ws, monkeypatch,
        f"UPDATE loan_application SET interest_rate = 65001 WHERE id = {application_id('0000500101')}",
    )

    result = ws.reconcile()

    assert failed(result) == {"RC-07"}
    [rate] = found(result, "RC-07")
    assert (rate.field, rate.expected, rate.actual, rate.line) == (
        "interest_rate", "6.5000", "6.5001", 2,
    )


def test_rate_stored_as_float_is_detected(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    loaded_with(
        ws, monkeypatch,
        f"UPDATE loan_application SET interest_rate = 6.5 WHERE id = {application_id('0000500101')}",
    )

    [rate] = found(ws.reconcile(), "RC-07")

    assert rate.field == "interest_rate"
    assert rate.actual == "6.5"


def test_mismatched_status_is_detected(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    loaded_with(
        ws, monkeypatch,
        f"UPDATE loan_application SET status = 'in_review' WHERE id = {application_id('0000500102')}",
    )

    result = ws.reconcile()

    assert failed(result) == {"RC-06", "RC-07"}
    [status] = found(result, "RC-07")
    assert (status.source_key, status.field, status.expected, status.actual) == (
        "0000500102", "status", "approved", "in_review",
    )
    distribution = {d.source_key: (d.expected, d.actual) for d in found(result, "RC-06")}
    assert distribution == {"approved": ("1", "0"), "in_review": ("2", "3")}


def test_mapping_defect_is_caught_by_independent_code_tables(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The planner maps U (underwriting) wrongly; reconciliation keeps its own code table.
    with monkeypatch.context() as patch:
        patch.setitem(contract.STATUSES, "U", ApplicationStatus.APPROVED)
        ws.convert()

    result = ws.reconcile()

    mismatches = {(d.source_key, d.expected, d.actual) for d in found(result, "RC-07")}
    assert mismatches == {
        ("0000500101", "in_review", "approved"), ("0000500104", "in_review", "approved"),
    }
    assert "RC-06" in failed(result)


def test_altered_name_is_detected(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    loaded_with(
        ws, monkeypatch,
        f"UPDATE borrower SET legal_name = 'Grace  Haverford' WHERE id = {borrower_id('00010010')}",
    )

    [name] = found(ws.reconcile(), "RC-07")

    assert (name.file, name.line, name.field) == ("borrowers.csv", 11, "legal_name")
    assert (name.expected, name.actual) == ("Grace Haverford", "Grace  Haverford")


# --- Missing, extra, and duplicate records ---------------------------------------------------


def test_missing_application_is_detected(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    loaded_with(
        ws, monkeypatch,
        f"DELETE FROM application_party WHERE application_id = {application_id('0000500106')}",
        f"DELETE FROM loan_application WHERE id = {application_id('0000500106')}",
    )

    result = ws.reconcile()

    assert {"RC-03", "RC-04", "RC-05", "RC-06", "RC-10"} <= failed(result)
    [missing] = found(result, "RC-04", "missing_key")
    assert (missing.file, missing.line, missing.source_key) == ("applications.csv", 7, "0000500106")
    [amount] = found(result, "RC-05", "loaded_amount_total")
    assert (amount.expected, amount.actual) == ("2281000.00", "2221000.00")
    [standalone] = found(result, "RC-10")
    assert standalone.source_key == "00010010"


def test_extra_record_is_detected(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    loaded_with(
        ws, monkeypatch,
        "INSERT INTO borrower (source_system, source_system_id, legal_name, borrower_type) "
        "VALUES ('LEGACY_LOS', '00010014', 'Whitcombe Family Trust', 'business')",
    )

    result = ws.reconcile()

    assert {"RC-03", "RC-04", "RC-06", "RC-10"} <= failed(result)
    [extra] = found(result, "RC-04", "unexpected_key")
    assert (extra.file, extra.line, extra.source_key) == ("borrowers.csv", 15, "00010014")
    assert "rejected (MP-01)" in extra.message


def test_duplicate_target_record_is_detected(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws.convert()
    real_read = reconcile.read_target

    def duplicated(path: Path) -> reconcile.TargetSnapshot:
        # The unique constraint blocks this in SQLite, so the duplicate is injected on read.
        snapshot = real_read(path)
        copy = next(a for a in snapshot.applications if a.source_system_id == "0000500105")
        clone = reconcile.TargetApplication(**{**copy.__dict__, "id": 999})
        return reconcile.TargetSnapshot(
            snapshot.borrowers, (*snapshot.applications, clone), snapshot.parties
        )

    monkeypatch.setattr(reconcile, "read_target", duplicated)

    result = ws.reconcile()

    [duplicate] = found(result, "RC-04", "duplicate_key")
    assert (duplicate.source_key, duplicate.target_id, duplicate.actual) == ("0000500105", 999, "2")
    assert "RC-09" in failed(result)
    assert "RC-05" in failed(result)


# --- Relationships ---------------------------------------------------------------------------


def test_guarantor_on_the_wrong_application_is_detected(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded_with(
        ws, monkeypatch,
        f"UPDATE application_party SET borrower_id = {borrower_id('00010006')} "
        f"WHERE application_id = {application_id('0000500101')} AND role = 'guarantor'",
    )

    result = ws.reconcile()

    # Counts, totals, and role distributions all still match.
    assert failed(result) == {"RC-08"}
    missing = found(result, "RC-08", "missing_relationship")
    unexpected = found(result, "RC-08", "unexpected_relationship")
    assert [(d.source_key, d.line, d.expected) for d in missing] == [
        ("0000500101/00010002", 3, "guarantor"),
    ]
    assert [(d.source_key, d.actual) for d in unexpected] == [("0000500101/00010006", "guarantor")]


def test_wrong_role_is_detected(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    loaded_with(
        ws, monkeypatch,
        "UPDATE application_party SET role = 'guarantor' WHERE role = 'co_borrower'",
    )

    result = ws.reconcile()

    assert failed(result) == {"RC-06", "RC-08"}
    [role] = found(result, "RC-08", "role_mismatch")
    assert (role.source_key, role.line, role.expected, role.actual) == (
        "0000500103/00010004", 7, "co_borrower", "guarantor",
    )


def test_missing_primary_borrower_is_detected(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded_with(
        ws, monkeypatch,
        "UPDATE application_party SET role = 'co_borrower' "
        f"WHERE application_id = {application_id('0000500105')}",
    )

    result = ws.reconcile()

    assert {"RC-06", "RC-08", "RC-09"} == failed(result)
    [primary] = found(result, "RC-09")
    assert (primary.source_key, primary.line, primary.expected, primary.actual) == (
        "0000500105", 6, "1", "0",
    )


def test_standalone_borrower_given_a_relationship_is_detected(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded_with(
        ws, monkeypatch,
        "INSERT INTO application_party (application_id, borrower_id, role) VALUES "
        f"({application_id('0000500102')}, {borrower_id('00010008')}, 'guarantor')",
    )

    result = ws.reconcile()

    assert {"RC-03", "RC-06", "RC-08", "RC-10"} == failed(result)
    [extra] = found(result, "RC-08", "unexpected_relationship")
    assert extra.source_key == "0000500102/00010008"
    [standalone] = found(result, "RC-10")
    assert (standalone.check, standalone.source_key) == ("standalone_customer_related", "00010008")


# --- Refusals --------------------------------------------------------------------------------


def test_refuses_a_run_that_is_not_ready(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    real_write = run.write_json_atomic

    def write(path: Path, data: Any) -> None:
        if path.name == "load_result.json":
            raise OSError("disk full (injected)")
        real_write(path, data)

    with monkeypatch.context() as patch:
        patch.setattr(run, "write_json_atomic", write)
        with pytest.raises(EvidenceIncompleteError):
            ws.convert()
    before = ws.snapshot()

    error = ws.refused()

    assert "Status is LOADING, not LOADED." in error.problems
    assert ws.snapshot() == before
    assert not ws.report_path().exists()


def test_refuses_when_an_archived_file_changed(ws: Workspace) -> None:
    ws.convert()
    archived = ws.evidence() / "source" / "applications.csv"
    archived.write_bytes(archived.read_bytes().replace(b"180000.00", b"180000.01"))
    before = ws.snapshot()

    error = ws.refused()

    assert any("applications.csv no longer matches" in p for p in error.problems)
    assert ws.snapshot() == before
    assert ws.manifest()["status"] == "LOADED"


def test_refuses_when_the_database_changed_after_loading(ws: Workspace) -> None:
    ws.convert()
    with closing(sqlite3.connect(ws.database())) as conn:
        conn.execute("UPDATE loan_application SET interest_rate = 70000 WHERE id = 1")
        conn.commit()
    before = ws.snapshot()

    error = ws.refused()

    assert "The conversion database no longer matches its recorded checksum." in error.problems
    assert ws.snapshot() == before
    assert ws.manifest()["status"] == "LOADED"


def test_refuses_when_a_transaction_file_is_left_behind(ws: Workspace) -> None:
    ws.convert()
    ws.database().with_name(DATABASE_NAME + "-journal").write_bytes(b"\x00")

    assert any("-journal exists" in p for p in ws.refused().problems)


def test_refuses_a_failed_run(ws: Workspace) -> None:
    (ws.source / "borrowers.csv").unlink()
    with pytest.raises(run.SourceRunFailedError):
        ws.convert()

    assert "Status is FAILED, not LOADED." in ws.refused().problems


def test_database_changing_during_reconciliation_is_refused(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws.convert()
    real_read = reconcile.read_target

    def read_then_change(path: Path) -> reconcile.TargetSnapshot:
        snapshot = real_read(path)
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("UPDATE borrower SET legal_name = legal_name || '.' WHERE id = 1")
            conn.commit()
        return snapshot

    monkeypatch.setattr(reconcile, "read_target", read_then_change)

    error = ws.refused()

    assert "changed while it was being reconciled" in str(error)
    assert ws.manifest()["status"] == "LOADED"
    assert not ws.report_path().exists()


# --- Atomic reporting and failure handling ---------------------------------------------------


def failing_writes(
    monkeypatch: pytest.MonkeyPatch,
    should_fail: Callable[[Path, dict[str, Any]], bool],
    error: BaseException | None = None,
) -> None:
    """Make ``write_json_atomic`` raise, without writing, whenever ``should_fail`` says so."""
    real_write = run.write_json_atomic

    def write(path: Path, data: Any) -> None:
        if should_fail(path, data):
            raise error or OSError(28, "No space left on device (injected)")
        real_write(path, data)

    monkeypatch.setattr(run, "write_json_atomic", write)


def is_report(path: Path, _data: dict[str, Any]) -> bool:
    return path.name == reconcile.RECONCILIATION_REPORT_NAME


def is_final_manifest(path: Path, data: dict[str, Any]) -> bool:
    return path.name == "manifest.json" and data.get("status") in ("RECONCILED", "FAILED")


def verified(ws: Workspace) -> reconcile.ReconciliationCheck:
    return reconcile.verify_reconciliation("run-1", evidence_root=ws.evidence_root)


def assert_not_reconciled(ws: Workspace, state: str) -> None:
    """The run must not be treated as ready, reconciled, or released."""
    manifest = ws.manifest()
    assert manifest["status"] == "LOADED"
    assert manifest["reconciliation"]["state"] == state
    assert manifest["ready_for_reconciliation"] is False
    assert manifest["release"] is None
    readiness = check_ready("run-1", evidence_root=ws.evidence_root)
    assert f"A reconciliation attempt exists (state {state})." in readiness.problems
    assert "Status is LOADED, not RECONCILED." in verified(ws).problems


def unfinalized(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> bytes:
    """Reconcile with the finalizing manifest write failing; returns the provisional report."""
    ws.convert()
    with monkeypatch.context() as patch:
        failing_writes(patch, is_final_manifest)
        with pytest.raises(reconcile.ReconciliationNotFinalizedError):
            ws.reconcile()
    return ws.report_path().read_bytes()


def in_progress(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> bytes:
    """Reconcile with every manifest write after the attempt marker failing."""
    ws.convert()
    marker_written = False

    def after_marker(path: Path, _data: dict[str, Any]) -> bool:
        nonlocal marker_written
        if path.name != "manifest.json":
            return False
        if not marker_written:
            marker_written = True
            return False
        return True

    with monkeypatch.context() as patch:
        failing_writes(patch, after_marker)
        with pytest.raises(reconcile.ReconciliationNotFinalizedError):
            ws.reconcile()
    return ws.report_path().read_bytes()


def test_attempt_is_recorded_before_the_report_is_written(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws.convert()
    seen: list[dict[str, Any]] = []

    def record_manifest(path: Path, _data: dict[str, Any]) -> bool:
        if is_report(path, _data):
            seen.append(ws.manifest())
        return False

    failing_writes(monkeypatch, record_manifest)
    result = ws.reconcile()

    [at_report_time] = seen
    assert at_report_time["status"] == "LOADED"
    assert at_report_time["reconciliation"]["state"] == "in_progress"
    assert at_report_time["ready_for_reconciliation"] is False
    final = ws.manifest()["reconciliation"]
    assert final["state"] == "final"
    assert final["attempt"] == at_report_time["reconciliation"]["attempt"]
    assert final["attempt"] == result.report["attempt"]
    assert final["report_sha256"] == sha256(ws.report_path())
    assert verified(ws).verified


def test_report_write_failure_restores_the_manifest(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws.convert()
    before = ws.snapshot()

    with monkeypatch.context() as patch:
        failing_writes(patch, is_report)
        with pytest.raises(OSError, match="injected"):
            ws.reconcile()

    # The attempt marker was undone byte for byte; no report and no temporary file remain.
    assert ws.snapshot() == before
    assert sorted(os.listdir(ws.evidence() / "reports")) == [
        "exceptions.csv", "exclusions.csv", "load_result.json", "warnings.csv",
    ]
    assert check_ready("run-1", evidence_root=ws.evidence_root).ready
    assert ws.reconcile().passed


def test_marker_write_failure_changes_nothing(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws.convert()
    before = ws.snapshot()

    def interrupted(*_args: Any) -> None:
        raise OSError(5, "I/O error (injected)")

    with monkeypatch.context() as patch:
        patch.setattr(run.os, "replace", interrupted)
        with pytest.raises(OSError):
            ws.reconcile()

    assert ws.snapshot() == before
    assert ws.reconcile().passed


def test_manifest_write_failure_records_an_unfinalized_report(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = unfinalized(ws, monkeypatch)

    assert_not_reconciled(ws, "unfinalized")
    record = ws.manifest()["reconciliation"]
    assert record["result"] == "passed"
    assert record["report"] == "reports/reconciliation.json"
    assert record["report_sha256"] == hashlib.sha256(report).hexdigest()
    assert "No space left on device" in record["error"]
    assert json.loads(report)["attempt"] == record["attempt"]


def test_retry_finalizes_the_unfinalized_report_without_rewriting_it(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = unfinalized(ws, monkeypatch)
    attempt = ws.manifest()["reconciliation"]["attempt"]
    database = sha256(ws.database())

    result = ws.reconcile()

    assert result.passed
    assert ws.report_path().read_bytes() == report
    manifest = ws.manifest()
    assert manifest["status"] == "RECONCILED"
    assert manifest["reconciliation"]["state"] == "final"
    assert manifest["reconciliation"]["attempt"] == attempt
    assert "finalized_by_retry_at" in manifest["reconciliation"]
    assert manifest["release"] is None
    assert sha256(ws.database()) == database
    assert verified(ws).verified


def test_retry_after_every_later_manifest_write_failed(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = in_progress(ws, monkeypatch)

    # Neither the finalization nor the unfinalized record could be written.
    assert_not_reconciled(ws, "in_progress")
    assert "report_sha256" not in ws.manifest()["reconciliation"]

    assert ws.reconcile().passed
    assert ws.report_path().read_bytes() == report
    assert verified(ws).verified


def test_failed_reconciliation_is_finalized_by_retry_as_failed(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    corrupt_during_load(monkeypatch, lambda conn: conn.execute(
        f"UPDATE loan_application SET term_months = 239 WHERE id = {application_id('0000500101')}"
    ))
    report = unfinalized(ws, monkeypatch)
    assert ws.manifest()["reconciliation"]["result"] == "failed"
    assert_not_reconciled(ws, "unfinalized")

    result = ws.reconcile()

    assert result.status is RunStatus.FAILED
    assert ws.report_path().read_bytes() == report
    assert ws.manifest()["status"] == "FAILED"
    assert ws.manifest()["failure"]["rule"] == "RC-07"
    assert not verified(ws).verified


@pytest.mark.parametrize("where", ["report", "finalization"])
def test_interrupted_reconciliation_can_be_retried(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    ws.convert()
    with monkeypatch.context() as patch:
        if where == "report":
            failing_writes(patch, is_report, KeyboardInterrupt())
        else:
            def interrupt(*_args: Any) -> None:
                raise KeyboardInterrupt

            patch.setattr(run._Evidence, "reconciled", interrupt)
        with pytest.raises(KeyboardInterrupt):
            ws.reconcile()

    if where == "report":
        assert ws.manifest()["reconciliation"] is None
        assert check_ready("run-1", evidence_root=ws.evidence_root).ready
    else:
        assert_not_reconciled(ws, "in_progress")
        report = ws.report_path().read_bytes()
    assert ws.reconcile().passed
    if where == "finalization":
        assert ws.report_path().read_bytes() == report
    assert verified(ws).verified


def test_database_change_before_retry_is_refused(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = unfinalized(ws, monkeypatch)
    manifest = ws.manifest()
    with closing(sqlite3.connect(ws.database())) as conn:
        conn.execute("UPDATE borrower SET legal_name = 'Changed' WHERE id = 1")
        conn.commit()

    error = ws.refused()

    assert "The conversion database no longer matches its recorded checksum." in error.problems
    assert ws.manifest() == manifest
    assert ws.report_path().read_bytes() == report


def test_source_change_before_retry_is_refused(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = unfinalized(ws, monkeypatch)
    manifest = ws.manifest()
    archived = ws.evidence() / "source" / "applications.csv"
    archived.write_bytes(archived.read_bytes().replace(b"0650", b"0651", 1))

    error = ws.refused()

    assert any("applications.csv no longer matches" in p for p in error.problems)
    assert ws.manifest() == manifest
    assert ws.report_path().read_bytes() == report


def assert_conflict_preserved(ws: Workspace, report: bytes | None, *messages: str) -> None:
    with pytest.raises(reconcile.ReconciliationConflictError) as raised:
        ws.reconcile()
    for message in messages:
        assert any(message in problem for problem in raised.value.problems), raised.value
    if report is not None:
        assert ws.report_path().read_bytes() == report
    assert_not_reconciled(ws, "conflict")
    record = ws.manifest()["reconciliation"]
    assert record["existing_report_sha256"] == (
        hashlib.sha256(report).hexdigest() if report is not None else None
    )
    # A conflict is sticky: retrying does not overwrite the evidence either.
    with pytest.raises(reconcile.ReconciliationConflictError):
        ws.reconcile()
    if report is not None:
        assert ws.report_path().read_bytes() == report

    fail_run("run-1", "Conflicting reconciliation evidence.", evidence_root=ws.evidence_root)

    manifest = ws.manifest()
    assert manifest["status"] == "FAILED"
    assert manifest["failure"]["stage"] == "reconciliation"
    assert manifest["reconciliation"]["state"] == "conflict"
    assert manifest["release"] is None
    if report is not None:
        assert ws.report_path().read_bytes() == report


def test_unreferenced_existing_report_is_never_overwritten(ws: Workspace) -> None:
    ws.convert()
    stray = b'{"run_id": "run-1", "result": "PASS"}\n'
    ws.report_path().write_bytes(stray)

    assert_conflict_preserved(
        ws, stray, "exists but the manifest records no reconciliation attempt"
    )


def test_tampered_provisional_report_is_a_conflict(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    unfinalized(ws, monkeypatch)
    report = ws.report()
    report["result"] = "FAIL"
    report["discrepancies"] = [{"rule": "RC-07", "message": "fabricated"}]
    ws.report_path().write_text(json.dumps(report, indent=2) + "\n", "utf-8")
    tampered = ws.report_path().read_bytes()

    assert_conflict_preserved(
        ws, tampered,
        "does not match the checksum recorded for attempt",
        "differs from a fresh reconciliation of the same evidence (fields: discrepancies, result)",
    )


def test_provisional_report_from_another_attempt_is_a_conflict(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    in_progress(ws, monkeypatch)
    report = ws.report()
    report["attempt"] = "0000000000000000"
    ws.report_path().write_text(json.dumps(report, indent=2) + "\n", "utf-8")
    other = ws.report_path().read_bytes()

    assert_conflict_preserved(ws, other, "belongs to attempt 0000000000000000")


def test_provisional_report_contradicted_by_the_target_is_a_conflict(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The report says PASS, but a fresh comparison of the same database fails.
    report = in_progress(ws, monkeypatch)
    real_read = reconcile.read_target

    def misread(path: Path) -> reconcile.TargetSnapshot:
        snapshot = real_read(path)
        return reconcile.TargetSnapshot(
            snapshot.borrowers, snapshot.applications[1:], snapshot.parties
        )

    monkeypatch.setattr(reconcile, "read_target", misread)

    assert_conflict_preserved(ws, report, "differs from a fresh reconciliation")


def test_unreadable_provisional_report_is_a_conflict(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    unfinalized(ws, monkeypatch)
    ws.report_path().write_bytes(b'{"truncated')

    assert_conflict_preserved(ws, b'{"truncated', "cannot be read")


def test_missing_unfinalized_report_is_a_conflict(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    unfinalized(ws, monkeypatch)
    ws.report_path().unlink()

    assert_conflict_preserved(ws, None, "records an unfinalized reports/reconciliation.json")
    assert not ws.report_path().exists()


def test_final_report_tampered_after_reconciliation_fails_verification(
    ws: Workspace,
) -> None:
    ws.convert()
    ws.reconcile()
    assert verified(ws).verified
    report = ws.report()
    report["totals"]["requested_amount"]["loaded"] = "9999999.00"
    ws.report_path().write_text(json.dumps(report, indent=2) + "\n", "utf-8")
    tampered = ws.report_path().read_bytes()

    problems = verified(ws).problems

    assert problems == (
        "reports/reconciliation.json does not match the checksum in the manifest.",
    )
    assert "Status is RECONCILED, not LOADED." in ws.refused().problems
    assert ws.report_path().read_bytes() == tampered


def test_verification_detects_database_and_source_changes(ws: Workspace) -> None:
    ws.convert()
    ws.reconcile()
    with closing(sqlite3.connect(ws.database())) as conn:
        conn.execute("UPDATE borrower SET legal_name = 'Changed' WHERE id = 1")
        conn.commit()
    archived = ws.evidence() / "source" / "borrowers.csv"
    archived.write_bytes(archived.read_bytes() + b"\n")

    problems = verified(ws).problems

    assert "The conversion database no longer matches its recorded checksum." in problems
    assert "The archived borrowers.csv does not match the recorded checksums." in problems
    assert ws.manifest()["status"] == "RECONCILED"


def test_fail_run_does_not_change_a_settled_reconciliation(ws: Workspace) -> None:
    ws.convert()
    ws.reconcile()

    with pytest.raises(ValueError, match="already RECONCILED"):
        fail_run("run-1", "no", evidence_root=ws.evidence_root)


def test_cli_reports_unfinalized_and_conflicting_evidence(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ws.convert()
    args = ["run-1", "--evidence-root", str(ws.evidence_root)]
    with monkeypatch.context() as patch:
        failing_writes(patch, is_final_manifest)
        assert reconcile_cli.main(args) == reconcile_cli.EXIT_NOT_FINALIZED
    assert "retry reconciliation" in capsys.readouterr().err

    ws.report_path().write_bytes(b"{}")

    assert reconcile_cli.main(args) == reconcile_cli.EXIT_CONFLICT
    err = capsys.readouterr().err
    assert "in conflict and was preserved" in err
    assert ws.report_path().read_bytes() == b"{}"


def test_report_is_written_atomically_as_exact_json(ws: Workspace) -> None:
    ws.convert()
    ws.reconcile()

    files = sorted(p.name for p in (ws.evidence() / "reports").iterdir())
    assert files == [
        "exceptions.csv", "exclusions.csv", "load_result.json", "reconciliation.json",
        "warnings.csv",
    ]
    text = ws.report_path().read_text("utf-8")
    report = json.loads(text, parse_float=lambda s: pytest.fail(f"float {s} in report"))
    assert report["run_id"] == "run-1"
    assert report["database"]["sha256"] == ws.manifest()["database"]["sha256"]
    assert report["source"]["sha256"] == {
        name: entry["archived_sha256"] for name, entry in ws.manifest()["source"]["files"].items()
    }


# --- Command line ----------------------------------------------------------------------------


def test_cli_reconciles_and_reports(ws: Workspace, capsys: pytest.CaptureFixture[str]) -> None:
    ws.convert()

    code = reconcile_cli.main(["run-1", "--evidence-root", str(ws.evidence_root)])

    out = capsys.readouterr().out
    assert code == reconcile_cli.EXIT_RECONCILED
    assert "RECONCILED" in out
    assert "has not been released" in out
    assert reconcile_cli.main(["run-1", "--evidence-root", str(ws.evidence_root)]) == (
        reconcile_cli.EXIT_REFUSED
    )


def test_cli_reports_a_mismatch(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    loaded_with(
        ws, monkeypatch,
        f"UPDATE loan_application SET term_months = 239 WHERE id = {application_id('0000500101')}",
    )

    code = reconcile_cli.main(["run-1", "--evidence-root", str(ws.evidence_root)])

    err = capsys.readouterr().err
    assert code == reconcile_cli.EXIT_MISMATCH
    assert "RC-07 applications.csv:2 0000500101 term_months: expected 240, actual 239" in err
    assert "cannot be released" in err
