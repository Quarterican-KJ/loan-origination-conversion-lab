"""Phase 2: transactional loading of a conversion plan into an isolated database (spec section 11)."""

import dataclasses
import hashlib
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, event, select
from sqlalchemy.orm import Session

from loan_lab.conversion.legacy import (
    ConversionPlan,
    Disposition,
    MappedApplication,
    Rule,
    SourceRef,
    plan_conversion,
)
from loan_lab.conversion.legacy import cli, loader
from loan_lab.conversion.legacy.loader import (
    DATABASE_NAME,
    LoadCounts,
    LoadFailedError,
    LoadResult,
    TargetPreconditionError,
    load_plan,
    new_run_id,
)
from loan_lab.db import create_db_engine
from loan_lab.models import ApplicationParty, Borrower, LoanApplication
from loan_lab.models.enums import ApplicationStatus, BorrowerType, LoanProduct, PartyRole
from loan_lab.paths import default_conversion_root, default_database_path, find_project_root

SAMPLE_DIR = find_project_root(Path(__file__)) / "sample_data" / "legacy"
APPLICATIONS = "applications.csv"
PARTIES = "application_parties.csv"
LOS_TABLES = ("borrower", "loan_application", "application_party")

# Spec 15.3: loaded relationships per application.
EXPECTED_RELATIONSHIPS = {
    "0000500101": {("00010001", PartyRole.PRIMARY_BORROWER), ("00010002", PartyRole.GUARANTOR)},
    "0000500102": {("00010001", PartyRole.PRIMARY_BORROWER), ("00010002", PartyRole.GUARANTOR)},
    "0000500103": {("00010003", PartyRole.PRIMARY_BORROWER), ("00010004", PartyRole.CO_BORROWER)},
    "0000500104": {("00010005", PartyRole.PRIMARY_BORROWER), ("00010006", PartyRole.GUARANTOR)},
    "0000500105": {("00010007", PartyRole.PRIMARY_BORROWER)},
    "0000500106": {("00010010", PartyRole.PRIMARY_BORROWER)},
}
# applications.csv values, as the exact Decimals the target must hold.
EXPECTED_APPLICATIONS = {
    "0000500101": (LoanProduct.COMMERCIAL_REAL_ESTATE, "1250000.00", "6.5000", 240,
                   ApplicationStatus.IN_REVIEW),
    "0000500102": (LoanProduct.COMMERCIAL_TERM, "180000.00", "7.1250", 84,
                   ApplicationStatus.APPROVED),
    "0000500103": (LoanProduct.RESIDENTIAL_MORTGAGE, "412500.00", "5.8750", 360,
                   ApplicationStatus.SUBMITTED),
    "0000500104": (LoanProduct.COMMERCIAL_TERM, "350000.00", "6.7500", 120,
                   ApplicationStatus.IN_REVIEW),
    "0000500105": (LoanProduct.CONSUMER_AUTO, "28500.00", "8.2500", 60,
                   ApplicationStatus.DECLINED),
    "0000500106": (LoanProduct.HOME_EQUITY, "60000.00", "7.3750", 180,
                   ApplicationStatus.DRAFT),
}


@pytest.fixture(scope="module")
def plan() -> ConversionPlan:
    return plan_conversion(SAMPLE_DIR)


@pytest.fixture
def loaded(plan: ConversionPlan, tmp_path: Path) -> LoadResult:
    return load_plan(plan, "run-1", conversion_root=tmp_path, batch_size=4)


@pytest.fixture
def target(loaded: LoadResult) -> Iterator[Session]:
    engine = create_db_engine(f"sqlite:///{loaded.database_path.as_posix()}")
    with Session(engine) as session:
        yield session
    engine.dispose()


def raw_rows(database: Path, sql: str) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)) as conn:
        return conn.execute(sql).fetchall()


def table_counts(database: Path) -> dict[str, int]:
    tables = {name for (name,) in raw_rows(database, "SELECT name FROM sqlite_master")}
    return {
        name: raw_rows(database, f"SELECT count(*) FROM {name}")[0][0]  # noqa: S608
        for name in LOS_TABLES
        if name in tables
    }


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replace_row(plan: ConversionPlan, ref: SourceRef, **changes: Any) -> ConversionPlan:
    """A deliberately inconsistent plan, standing in for a converter defect."""
    field = {APPLICATIONS: "applications", PARTIES: "parties"}[ref.file]
    rows = list(getattr(plan, field))
    rows[ref.line - 2] = dataclasses.replace(rows[ref.line - 2], **changes)
    return dataclasses.replace(plan, **{field: tuple(rows)})


# --- Successful load -----------------------------------------------------------------------


def test_sample_load_counts_and_requested_amount(loaded: LoadResult, tmp_path: Path) -> None:
    assert loaded.database_path == tmp_path / "run-1" / DATABASE_NAME
    assert loaded.counts == LoadCounts(borrowers=11, applications=6, parties=10)
    assert loaded.requested_amount == Decimal("2281000.00")
    assert str(loaded.requested_amount) == "2281000.00"
    assert table_counts(loaded.database_path) == {
        "borrower": 11, "loan_application": 6, "application_party": 10,
    }


def test_crosswalks_match_generated_ids(loaded: LoadResult, target: Session) -> None:
    borrowers = dict(target.execute(select(Borrower.source_system_id, Borrower.id)).all())
    applications = dict(
        target.execute(select(LoanApplication.source_system_id, LoanApplication.id)).all()
    )
    parties = {
        (p.application.source_system_id, p.borrower.source_system_id): p.id
        for p in target.scalars(select(ApplicationParty))
    }

    assert dict(loaded.borrower_ids) == borrowers
    assert dict(loaded.application_ids) == applications
    assert dict(loaded.party_ids) == parties
    with pytest.raises(TypeError):
        loaded.borrower_ids["00010001"] = 99  # type: ignore[index]


def test_only_eligible_records_enter_the_target(
    plan: ConversionPlan, loaded: LoadResult, target: Session
) -> None:
    def eligible_keys(rows: tuple) -> set[str]:  # type: ignore[type-arg]
        return {row.key for row in rows if row.disposition is Disposition.ELIGIBLE}

    assert set(target.scalars(select(Borrower.source_system_id))) == eligible_keys(plan.borrowers)
    assert set(target.scalars(select(LoanApplication.source_system_id))) == eligible_keys(
        plan.applications
    )
    assert {f"{a}/{c}" for a, c in loaded.party_ids} == eligible_keys(plan.parties)
    # Spot checks for each kind of non-loaded row in spec section 15.
    assert "00010012" not in loaded.borrower_ids  # excluded, EX-01
    assert "00010013" not in loaded.borrower_ids  # rejected, SV-09
    assert "0000500107" not in loaded.application_ids  # rejected, own failure
    assert "0000500109" not in loaded.application_ids  # rejected unit
    assert "0000500110" not in loaded.application_ids  # excluded, EX-02
    assert ("0000500104", "00010007") not in loaded.party_ids  # signer, EX-04
    assert ("0000500107", "00010009") not in loaded.party_ids  # rejected dependent, RF-05
    assert ("0000500110", "00010008") not in loaded.party_ids  # excluded dependent, EX-05


def test_source_identity_and_leading_zeros_are_preserved(loaded: LoadResult) -> None:
    for table in ("borrower", "loan_application"):
        rows = raw_rows(
            loaded.database_path,
            f"SELECT source_system, source_system_id, typeof(source_system_id) FROM {table}",  # noqa: S608
        )
        assert {(system, kind) for system, _, kind in rows} == {("LEGACY_LOS", "text")}
        assert all(key.startswith("000") for _, key, _ in rows)
    assert ("00010001",) in raw_rows(loaded.database_path, "SELECT source_system_id FROM borrower")
    assert ("0000500101",) in raw_rows(
        loaded.database_path, "SELECT source_system_id FROM loan_application"
    )


def test_financial_values_are_exact(loaded: LoadResult, target: Session) -> None:
    for application in target.scalars(select(LoanApplication)):
        product, amount, rate, term, status = EXPECTED_APPLICATIONS[application.source_system_id]
        assert application.loan_product is product
        assert application.requested_amount == Decimal(amount)
        assert str(application.requested_amount) == amount
        assert str(application.interest_rate) == rate
        assert application.term_months == term
        assert application.status is status
    # On SQLite, ExactDecimal stores value * 10**scale as an INTEGER, never a REAL.
    assert raw_rows(
        loaded.database_path,
        "SELECT requested_amount, typeof(requested_amount), interest_rate, typeof(interest_rate) "
        "FROM loan_application WHERE source_system_id = '0000500101'",
    ) == [(125000000, "integer", 65000, "integer")]
    total = raw_rows(loaded.database_path, "SELECT sum(requested_amount) FROM loan_application")
    assert Decimal(total[0][0]).scaleb(-2) == loaded.requested_amount


def test_borrower_names_and_types(target: Session) -> None:
    borrowers = {b.source_system_id: b for b in target.scalars(select(Borrower))}

    assert borrowers["00010010"].legal_name == "Grace Haverford"
    assert borrowers["00010002"].legal_name == "Elena R. Marsh"
    assert borrowers["00010001"].borrower_type is BorrowerType.BUSINESS
    assert sum(b.borrower_type is BorrowerType.INDIVIDUAL for b in borrowers.values()) == 8


def test_relationships_resolve_to_the_right_records(target: Session) -> None:
    actual = {
        application.source_system_id: {
            (party.borrower.source_system_id, party.role) for party in application.parties
        }
        for application in target.scalars(select(LoanApplication))
    }

    assert actual == EXPECTED_RELATIONSHIPS


def test_standalone_borrowers_load_without_relationships(
    plan: ConversionPlan, loaded: LoadResult, target: Session
) -> None:
    standalone = ("00010008", "00010009", "00010015")

    assert loaded.borrowers_without_relationships == standalone
    assert tuple(row.key for row in plan.customers_without_applications) == standalone
    for key in standalone:
        borrower = target.scalars(select(Borrower).where(Borrower.source_system_id == key)).one()
        assert borrower.parties == []


@pytest.mark.parametrize("batch_size", [1, 3, 1000])
def test_batch_size_does_not_change_the_result(
    plan: ConversionPlan, tmp_path: Path, batch_size: int
) -> None:
    result = load_plan(plan, f"batch-{batch_size}", conversion_root=tmp_path, batch_size=batch_size)

    assert result.counts == LoadCounts(11, 6, 10)
    assert list(result.borrower_ids.values()) == list(range(1, 12))
    assert list(result.party_ids.values()) == list(range(1, 11))


def test_loading_does_not_change_the_plan_or_the_source(plan: ConversionPlan, tmp_path: Path) -> None:
    before = {path.name: digest(path) for path in SAMPLE_DIR.iterdir()}

    load_plan(plan, "run-1", conversion_root=tmp_path)

    assert {path.name: digest(path) for path in SAMPLE_DIR.iterdir()} == before
    assert plan == plan_conversion(SAMPLE_DIR)


def test_no_journal_or_other_files_are_left(loaded: LoadResult) -> None:
    assert sorted(p.name for p in loaded.database_path.parent.iterdir()) == [DATABASE_NAME]


# --- RUN-07: a new, empty database per run -------------------------------------------------


def test_second_load_with_the_same_run_id_is_refused(
    plan: ConversionPlan, loaded: LoadResult, tmp_path: Path
) -> None:
    before = digest(loaded.database_path)

    with pytest.raises(TargetPreconditionError) as raised:
        load_plan(plan, "run-1", conversion_root=tmp_path)

    assert raised.value.issue.rule is Rule.RUN_07
    assert "never reused" in raised.value.issue.message
    assert digest(loaded.database_path) == before
    assert table_counts(loaded.database_path)["borrower"] == 11


def test_existing_empty_run_directory_is_refused(plan: ConversionPlan, tmp_path: Path) -> None:
    (tmp_path / "run-1").mkdir()

    with pytest.raises(TargetPreconditionError):
        load_plan(plan, "run-1", conversion_root=tmp_path)

    assert list((tmp_path / "run-1").iterdir()) == []


def test_existing_database_file_is_never_overwritten(
    plan: ConversionPlan, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_directory = tmp_path / "run-1"
    existing = run_directory / DATABASE_NAME
    real_mkdir = Path.mkdir

    def mkdir_then_race(self: Path, *args: Any, **kwargs: Any) -> None:
        real_mkdir(self, *args, **kwargs)
        if self == run_directory:
            existing.write_bytes(b"another process got here first")

    monkeypatch.setattr(Path, "mkdir", mkdir_then_race)

    with pytest.raises(TargetPreconditionError, match="never overwritten"):
        load_plan(plan, "run-1", conversion_root=tmp_path)

    assert existing.read_bytes() == b"another process got here first"


def test_database_with_existing_schema_is_refused(
    plan: ConversionPlan, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_create = loader._create_database_file  # noqa: SLF001

    def create_with_content(run_directory: Path) -> Path:
        path = real_create(run_directory)
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TABLE leftover (id INTEGER)")
            conn.commit()
        return path

    monkeypatch.setattr(loader, "_create_database_file", create_with_content)

    with pytest.raises(TargetPreconditionError, match="not empty") as raised:
        load_plan(plan, "run-1", conversion_root=tmp_path)

    assert raised.value.issue.rule is Rule.RUN_07
    assert table_counts(tmp_path / "run-1" / DATABASE_NAME) == {}


@pytest.mark.parametrize("run_id", ["", "..", ".hidden", "a/b", "a\\b", "../escape", "x" * 65,
                                    "run 1", "run:1"])
def test_unsafe_run_ids_are_rejected_before_anything_is_created(
    plan: ConversionPlan, tmp_path: Path, run_id: str
) -> None:
    with pytest.raises(ValueError, match="Invalid run ID"):
        load_plan(plan, run_id, conversion_root=tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_new_run_ids_are_valid_and_distinct() -> None:
    ids = {new_run_id() for _ in range(20)}

    assert len(ids) == 20
    assert all(loader.RUN_ID_PATTERN.fullmatch(run_id) for run_id in ids)


def test_default_location_is_isolated_from_the_development_database() -> None:
    root = default_conversion_root()

    assert root == find_project_root(Path(__file__)) / "data" / "conversion"
    assert default_database_path().parent == root.parent
    assert DATABASE_NAME != default_database_path().name


def test_development_database_is_untouched(plan: ConversionPlan, tmp_path: Path) -> None:
    dev = default_database_path()
    before = digest(dev) if dev.exists() else None

    load_plan(plan, "run-1", conversion_root=tmp_path)

    assert (digest(dev) if dev.exists() else None) == before


# --- Rollback after injected failures ------------------------------------------------------


def inject_failure(monkeypatch: pytest.MonkeyPatch, on: Callable[[str], bool]) -> None:
    """Make the loader's engine raise just before a matching SQL statement runs."""
    real_create = loader.create_db_engine

    def create(url: str, **kwargs: Any) -> Engine:
        engine = real_create(url, **kwargs)

        def fail_statement(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
            if on(statement):
                raise RuntimeError(f"injected failure on: {statement.split('(')[0].strip()}")

        event.listen(engine, "before_cursor_execute", fail_statement)
        return engine

    monkeypatch.setattr(loader, "create_db_engine", create)


def assert_rolled_back(
    raised: LoadFailedError, tmp_path: Path, planned_amount: str = "2281000.00"
) -> None:
    database = tmp_path / "run-1" / DATABASE_NAME
    assert raised.run_id == "run-1"
    assert raised.database_path == database
    assert raised.planned == LoadCounts(11, 6, 10)
    assert raised.planned_amount == Decimal(planned_amount)
    assert raised.__cause__ is not None
    # The schema stays as evidence, but no converted row was committed.
    assert table_counts(database) == {name: 0 for name in LOS_TABLES}
    assert sorted(p.name for p in database.parent.iterdir()) == [DATABASE_NAME]


def test_failure_on_the_last_insert_rolls_back_everything(
    plan: ConversionPlan, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inject_failure(monkeypatch, lambda sql: sql.startswith("INSERT INTO application_party"))

    with pytest.raises(LoadFailedError) as raised:
        load_plan(plan, "run-1", conversion_root=tmp_path)

    assert "injected failure on: INSERT INTO application_party" in raised.value.error
    assert_rolled_back(raised.value, tmp_path)


def test_failure_in_a_later_batch_rolls_back_earlier_batches(
    plan: ConversionPlan, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def third_application_batch(sql: str) -> bool:
        if sql.startswith("INSERT INTO loan_application"):
            calls.append(sql)
        return len(calls) == 3

    inject_failure(monkeypatch, third_application_batch)

    with pytest.raises(LoadFailedError) as raised:
        load_plan(plan, "run-1", conversion_root=tmp_path, batch_size=2)

    # Six borrower batches and two application batches had already been inserted.
    assert len(calls) == 3
    assert_rolled_back(raised.value, tmp_path)


def test_failure_at_commit_rolls_back_everything(
    plan: ConversionPlan, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commits = []
    real_create = loader.create_db_engine

    def create(url: str, **kwargs: Any) -> Engine:
        engine = real_create(url, **kwargs)

        def fail_second_commit(_conn: Any) -> None:
            commits.append(1)
            if len(commits) == 2:  # the first commit is the schema
                raise RuntimeError("injected commit failure")

        event.listen(engine, "commit", fail_second_commit)
        return engine

    monkeypatch.setattr(loader, "create_db_engine", create)

    with pytest.raises(LoadFailedError, match="injected commit failure") as raised:
        load_plan(plan, "run-1", conversion_root=tmp_path)

    assert_rolled_back(raised.value, tmp_path)


def test_database_constraint_failure_rolls_back(
    plan: ConversionPlan, tmp_path: Path
) -> None:
    ref = SourceRef(APPLICATIONS, 7)  # 0000500106, the last eligible application
    target = plan.row(ref).target
    assert isinstance(target, MappedApplication)
    broken = replace_row(
        plan, ref, target=dataclasses.replace(target, requested_amount=Decimal("0.00"))
    )

    with pytest.raises(LoadFailedError, match="IntegrityError") as raised:
        load_plan(broken, "run-1", conversion_root=tmp_path)

    assert "requested_amount_positive" in raised.value.error
    assert_rolled_back(raised.value, tmp_path, planned_amount="2221000.00")


def test_inexact_decimal_is_refused_not_rounded(plan: ConversionPlan, tmp_path: Path) -> None:
    ref = SourceRef(APPLICATIONS, 2)
    target = plan.row(ref).target
    assert isinstance(target, MappedApplication)
    broken = replace_row(
        plan, ref, target=dataclasses.replace(target, interest_rate=Decimal("6.50001"))
    )

    with pytest.raises(LoadFailedError, match="more than 4 decimal places") as raised:
        load_plan(broken, "run-1", conversion_root=tmp_path)

    assert_rolled_back(raised.value, tmp_path)


def test_party_of_an_unloaded_application_fails_instead_of_being_skipped(
    plan: ConversionPlan, tmp_path: Path
) -> None:
    # Line 13 (0000500107/00010009) is rejected dependent; force it eligible as a defect would.
    broken = replace_row(
        plan, SourceRef(PARTIES, 13), disposition=Disposition.ELIGIBLE, dependent=False,
        issues=(),
    )

    with pytest.raises(LoadFailedError, match="application 0000500107, which is not loaded") as e:
        load_plan(broken, "run-1", conversion_root=tmp_path)

    assert e.value.planned == LoadCounts(11, 6, 11)
    database = tmp_path / "run-1" / DATABASE_NAME
    assert table_counts(database) == {name: 0 for name in LOS_TABLES}


def test_duplicate_key_in_plan_fails_the_load(plan: ConversionPlan, tmp_path: Path) -> None:
    duplicate = dataclasses.replace(plan, borrowers=(*plan.borrowers, plan.borrowers[0]))

    with pytest.raises(LoadFailedError, match="Duplicate source key in the borrower"):
        load_plan(duplicate, "run-1", conversion_root=tmp_path)

    assert table_counts(tmp_path / "run-1" / DATABASE_NAME) == {name: 0 for name in LOS_TABLES}


def test_schema_creation_failure_leaves_no_tables(
    plan: ConversionPlan, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inject_failure(monkeypatch, lambda sql: "CREATE TABLE application_party" in sql)

    with pytest.raises(LoadFailedError):
        load_plan(plan, "run-1", conversion_root=tmp_path)

    # DDL ran inside the transaction, so tables created before the failure were rolled back too.
    assert table_counts(tmp_path / "run-1" / DATABASE_NAME) == {}


def cli_roots(tmp_path: Path) -> list[str]:
    return ["--conversion-root", str(tmp_path), "--evidence-root", str(tmp_path / "evidence")]


def test_cli_loads_and_reports(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main([str(SAMPLE_DIR), "--run-id", "cli-1", *cli_roots(tmp_path)])

    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert "Run cli-1 LOADED" in out
    assert "Borrowers           16      11         1         4" in out
    assert "Applications        13       6         2         5" in out
    assert "Parties             20      10         3         7" in out
    assert "Requested amount loaded: 2281000.00" in out
    assert "00010008, 00010009, 00010015" in out
    assert table_counts(tmp_path / "cli-1" / DATABASE_NAME)["loan_application"] == 6


def test_cli_refuses_an_existing_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = [str(SAMPLE_DIR), "--run-id", "cli-1", *cli_roots(tmp_path)]
    assert cli.main(args) == cli.EXIT_OK

    assert cli.main(args) == cli.EXIT_TARGET_NOT_NEW
    assert "RUN-07" in capsys.readouterr().err


def test_cli_stops_on_invalid_source(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(
        [str(tmp_path / "missing"), "--run-id", "bad-1", *cli_roots(tmp_path / "conv")]
    )

    assert code == cli.EXIT_SOURCE_INVALID
    assert "RUN-01" in capsys.readouterr().err
    # The failed run keeps its evidence, but no database is ever created for it.
    assert sorted(p.name for p in (tmp_path / "conv").iterdir()) == ["evidence"]
    assert (tmp_path / "conv" / "evidence" / "bad-1" / "manifest.json").is_file()


def test_cli_reports_a_load_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    inject_failure(monkeypatch, lambda sql: sql.startswith("INSERT INTO application_party"))

    code = cli.main([str(SAMPLE_DIR), "--run-id", "cli-1", *cli_roots(tmp_path)])

    assert code == cli.EXIT_LOAD_FAILED
    assert "FAILED at stage load" in capsys.readouterr().err
    assert table_counts(tmp_path / "cli-1" / DATABASE_NAME) == {name: 0 for name in LOS_TABLES}


def test_failed_run_id_is_spent(
    plan: ConversionPlan, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inject_failure(monkeypatch, lambda sql: sql.startswith("INSERT INTO borrower"))
    with pytest.raises(LoadFailedError):
        load_plan(plan, "run-1", conversion_root=tmp_path)
    monkeypatch.undo()

    with pytest.raises(TargetPreconditionError):
        load_plan(plan, "run-1", conversion_root=tmp_path)
    assert load_plan(plan, "run-2", conversion_root=tmp_path).counts == LoadCounts(11, 6, 10)
