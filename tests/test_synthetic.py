import subprocess
import sys
from collections import Counter
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from loan_lab.db import create_db_engine
from loan_lab.main import app
from loan_lab.models import (
    Base,
    Borrower,
    BorrowerType,
    Collateral,
    CollateralPledge,
    CollateralType,
    LienStatus,
    LoanApplication,
    LoanProduct,
    PartyRole,
)
from loan_lab.synthetic import (
    DEFAULT_SEED,
    PRESETS,
    SyntheticDataGenerator,
    count_rows,
    seed_database,
)
from loan_lab.synthetic import cli
from loan_lab.synthetic.cli import LABELS, find_project_root, main
from loan_lab.synthetic.generator import (
    APPRAISAL_PENDING_STATUSES,
    LINKED_PARTY_OWNER_PERCENT,
    OAK_RIDGE_GUARANTOR_NAME,
    OAK_RIDGE_NAME,
    PLEDGE_STATUS_FOR_APPLICATION,
    PRODUCT_SPECS,
    RATE_STEP,
)
from loan_lab.synthetic.seeding import INSERT_ORDER

SMALL = PRESETS["small"]


def sqlite_engine(path: Path | None = None) -> Engine:
    return create_db_engine(f"sqlite:///{path.as_posix()}" if path else "sqlite://")


def dump(engine: Engine) -> dict[str, list[tuple[object, ...]]]:
    with engine.connect() as conn:
        return {
            name: [tuple(row) for row in conn.execute(select(table).order_by(table.c.id))]
            for name, table in INSERT_ORDER
        }


def seeded(applications: int, seed: int = DEFAULT_SEED, batch_size: int = 1_000) -> Engine:
    engine = sqlite_engine()
    seed_database(engine, applications, seed=seed, batch_size=batch_size)
    return engine


@pytest.fixture(scope="module")
def small_engine(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Engine]:
    engine = sqlite_engine(tmp_path_factory.mktemp("seed") / "small.db")
    seed_database(engine, SMALL)
    yield engine
    engine.dispose()


@pytest.fixture
def small_session(small_engine: Engine) -> Iterator[Session]:
    with Session(small_engine) as session:
        yield session


# --- Presets and counts -------------------------------------------------------------------


def test_presets() -> None:
    assert PRESETS == {"small": 25, "demo": 5_000, "stress": 100_000}


EXPECTED_SMALL_COUNTS = [
    ("borrowers", 30),
    ("applications", 25),
    ("application_parties", 37),
    ("collateral", 20),
    ("collateral_pledges", 21),
    ("liens", 6),
]


def test_small_preset_counts(small_engine: Engine) -> None:
    summary = count_rows(small_engine)

    assert summary.applications == 25
    assert summary.items() == EXPECTED_SMALL_COUNTS


def test_summary_counts_match_generated_rows(small_engine: Engine) -> None:
    generated: Counter[str] = Counter()
    for batch in SyntheticDataGenerator(SMALL).batches():
        for name, _ in INSERT_ORDER:
            generated[name] += len(getattr(batch, name))

    assert dict(count_rows(small_engine).items()) == dict(generated)


def test_batches_are_bounded_and_lazy() -> None:
    batches = SyntheticDataGenerator(SMALL, batch_size=4).batches()

    assert isinstance(batches, Iterator)
    sizes = [len(batch.applications) for batch in batches]
    assert sizes == [4, 4, 4, 4, 4, 4, 1]


@pytest.mark.parametrize(("applications", "batch_size"), [(1, 10), (25, 0)])
def test_generator_rejects_invalid_settings(applications: int, batch_size: int) -> None:
    with pytest.raises(ValueError):
        SyntheticDataGenerator(applications, batch_size=batch_size)


# --- Determinism --------------------------------------------------------------------------


def test_same_settings_produce_identical_data(small_engine: Engine) -> None:
    assert dump(seeded(SMALL)) == dump(small_engine)


def test_batch_size_does_not_change_data(small_engine: Engine) -> None:
    assert dump(seeded(SMALL, batch_size=3)) == dump(small_engine)


def test_different_seed_produces_different_data(small_engine: Engine) -> None:
    other = dump(seeded(SMALL, seed=DEFAULT_SEED + 1))

    assert other["applications"] != dump(small_engine)["applications"]
    assert len(other["applications"]) == SMALL


def test_small_preset_is_a_prefix_of_larger_runs(small_engine: Engine) -> None:
    larger = dump(seeded(60))

    assert larger["applications"][:SMALL] == dump(small_engine)["applications"]


# --- Oak Ridge scenario -------------------------------------------------------------------


def test_oak_ridge_scenario(small_session: Session) -> None:
    oak_ridge = small_session.scalars(select(Borrower).filter_by(legal_name=OAK_RIDGE_NAME)).one()
    assert oak_ridge.borrower_type is BorrowerType.BUSINESS
    assert len(oak_ridge.applications) == 2
    cre_loan, equipment_loan = oak_ridge.applications
    assert cre_loan.loan_product is LoanProduct.COMMERCIAL_REAL_ESTATE
    assert equipment_loan.loan_product is LoanProduct.COMMERCIAL_TERM

    for application in oak_ridge.applications:
        roles = {party.borrower.legal_name: party.role for party in application.parties}
        assert roles == {
            OAK_RIDGE_NAME: PartyRole.PRIMARY_BORROWER,
            OAK_RIDGE_GUARANTOR_NAME: PartyRole.GUARANTOR,
        }

    assert cre_loan.requested_amount == Decimal("500000.00")
    assert equipment_loan.requested_amount == Decimal("75000.00")

    building = next(c for c in cre_loan.collateral if c.collateral_type is CollateralType.REAL_ESTATE)
    assert building.applications == [cre_loan, equipment_loan]
    assert building.owner == oak_ridge
    assert building.appraised_value == Decimal("725000.00")
    assert [(l.priority, l.outstanding_balance, l.status) for l in building.liens] == [
        (1, Decimal("200000.00"), LienStatus.ACTIVE)
    ]
    assert building.liens[0].outstanding_balance != cre_loan.requested_amount

    equipment = next(
        c for c in equipment_loan.collateral if c.collateral_type is CollateralType.EQUIPMENT
    )
    assert equipment.owner == oak_ridge
    assert equipment.appraised_value == Decimal("150000.00")
    assert equipment.applications == [equipment_loan]
    assert {c.collateral_type for c in equipment_loan.collateral} == {
        CollateralType.REAL_ESTATE,
        CollateralType.EQUIPMENT,
    }
    pledged = {
        (pledge.application_id, pledge.collateral_id): pledge.pledged_amount
        for pledge in building.pledges + equipment.pledges
    }
    assert pledged == {
        (cre_loan.id, building.id): Decimal("500000.00"),
        (equipment_loan.id, building.id): None,
        (equipment_loan.id, equipment.id): Decimal("75000.00"),
    }


def test_oak_ridge_amounts_fit_product_ranges(small_session: Session) -> None:
    oak_ridge = small_session.scalars(select(Borrower).filter_by(legal_name=OAK_RIDGE_NAME)).one()
    for application in oak_ridge.applications:
        spec = PRODUCT_SPECS[application.loan_product]
        assert spec.min_amount <= application.requested_amount <= spec.max_amount
        assert spec.min_rate <= application.interest_rate <= spec.max_rate
        assert application.term_months in spec.terms_months


def test_oak_ridge_scenario_is_identical_across_seeds() -> None:
    def oak_ridge_rows(seed: int) -> dict[str, list[tuple[object, ...]]]:
        rows = dump(seeded(SMALL, seed=seed))
        return {
            "applications": rows["applications"][:2],
            "collateral": [r for r in rows["collateral"] if "Oak Ridge Parkway" in str(r)],
            "liens": [r for r in rows["liens"] if r[1] == 1],
        }

    assert oak_ridge_rows(DEFAULT_SEED) == oak_ridge_rows(DEFAULT_SEED + 99)


def test_oak_ridge_appears_exactly_once_in_larger_runs() -> None:
    with Session(seeded(300)) as session:
        names = session.scalars(select(Borrower.legal_name)).all()

    assert names.count(OAK_RIDGE_NAME) == 1
    assert names.count(OAK_RIDGE_GUARANTOR_NAME) == 1


# --- Relationships and loan terms ---------------------------------------------------------


@pytest.fixture(scope="module")
def medium_engine() -> Iterator[Engine]:
    engine = seeded(500)
    yield engine
    engine.dispose()


@pytest.fixture
def medium_session(medium_engine: Engine) -> Iterator[Session]:
    with Session(medium_engine) as session:
        yield session


def test_every_application_has_exactly_one_primary_borrower(medium_session: Session) -> None:
    for application in medium_session.scalars(select(LoanApplication)):
        roles = [party.role for party in application.parties]
        assert roles.count(PartyRole.PRIMARY_BORROWER) == 1


def test_party_types_match_product(medium_session: Session) -> None:
    for application in medium_session.scalars(select(LoanApplication)):
        spec = PRODUCT_SPECS[application.loan_product]
        for party in application.parties:
            if party.role is PartyRole.PRIMARY_BORROWER:
                expected = BorrowerType.BUSINESS if spec.commercial else BorrowerType.INDIVIDUAL
                assert party.borrower.borrower_type is expected
            elif party.role is PartyRole.GUARANTOR:
                assert spec.commercial
                assert party.borrower.borrower_type is BorrowerType.INDIVIDUAL
            else:
                assert not spec.commercial


def test_collateral_matches_product_and_is_owned_by_a_party(medium_session: Session) -> None:
    for application in medium_session.scalars(select(LoanApplication)):
        spec = PRODUCT_SPECS[application.loan_product]
        if spec.collateral_type is None:
            assert application.collateral_pledges == []
            continue
        assert spec.collateral_type in {c.collateral_type for c in application.collateral}
        party_ids = {party.borrower_id for party in application.parties}
        for collateral in application.collateral:
            assert collateral.owner_id in party_ids


def test_some_collateral_is_owned_by_a_linked_party(medium_session: Session) -> None:
    collateral = medium_session.scalars(select(Collateral)).all()
    owner_roles: Counter[PartyRole] = Counter()
    for item in collateral:
        first_application = min(item.pledges, key=lambda p: p.id).application
        owner_roles[
            next(p.role for p in first_application.parties if p.borrower_id == item.owner_id)
        ] += 1
        if item.owner_id != _primary_borrower_id(first_application):
            # Linked-party collateral is never offered to later applications.
            assert len(item.pledges) == 1

    linked = owner_roles[PartyRole.GUARANTOR] + owner_roles[PartyRole.CO_BORROWER]
    assert owner_roles[PartyRole.GUARANTOR] > 0
    assert owner_roles[PartyRole.CO_BORROWER] > 0
    assert 0 < linked <= len(collateral) * LINKED_PARTY_OWNER_PERCENT // 100
    assert owner_roles[PartyRole.PRIMARY_BORROWER] > linked


def _primary_borrower_id(application: LoanApplication) -> int:
    return next(p.borrower_id for p in application.parties if p.role is PartyRole.PRIMARY_BORROWER)


def test_reuse_produces_shared_borrowers_and_collateral(medium_session: Session) -> None:
    borrowers = medium_session.scalars(select(Borrower)).all()
    collateral = medium_session.scalars(select(Collateral)).all()

    assert sum(len(b.applications) > 1 for b in borrowers) > 1
    assert sum(len(c.applications) > 1 for c in collateral) > 1


def test_loan_terms_are_within_product_ranges(medium_session: Session) -> None:
    products = Counter[LoanProduct]()
    for application in medium_session.scalars(select(LoanApplication)):
        spec = PRODUCT_SPECS[application.loan_product]
        products[application.loan_product] += 1
        assert spec.min_amount <= application.requested_amount <= spec.max_amount
        assert application.requested_amount % spec.amount_step == 0
        assert spec.min_rate <= application.interest_rate <= spec.max_rate
        assert application.interest_rate % RATE_STEP == 0
        assert application.term_months in spec.terms_months

    assert set(products) == set(LoanProduct)


def test_liens_are_below_known_collateral_values(medium_session: Session) -> None:
    for collateral in medium_session.scalars(select(Collateral)):
        if collateral.appraised_value is None:
            continue
        active = sum(l.outstanding_balance for l in collateral.liens if l.status is LienStatus.ACTIVE)
        assert active < collateral.appraised_value
        assert all(
            l.outstanding_balance == 0 for l in collateral.liens if l.status is LienStatus.RELEASED
        )


def test_missing_valuations_are_null_and_only_on_early_applications(
    medium_session: Session,
) -> None:
    missing = medium_session.scalars(
        select(Collateral).where(Collateral.appraised_value.is_(None))
    ).all()

    assert missing
    for collateral in missing:
        assert collateral.valuation_date is None
        first_pledge = min(collateral.pledges, key=lambda p: p.id)
        assert first_pledge.application.status in APPRAISAL_PENDING_STATUSES
        assert first_pledge.pledged_amount is None
    assert medium_session.scalars(
        select(Collateral).where(
            Collateral.appraised_value.is_not(None), Collateral.valuation_date.is_(None)
        )
    ).all() == []


def test_pledge_status_follows_application_status(medium_session: Session) -> None:
    for pledge in medium_session.scalars(select(CollateralPledge)):
        status = pledge.application.status
        assert pledge.status is PLEDGE_STATUS_FOR_APPLICATION[status]


# --- Financial precision ------------------------------------------------------------------


def test_financial_values_are_exact_decimals(medium_session: Session) -> None:
    applications = medium_session.scalars(select(LoanApplication)).all()
    collateral = medium_session.scalars(select(Collateral)).all()
    liens = [lien for c in collateral for lien in c.liens]

    for application in applications:
        assert type(application.requested_amount) is Decimal
        assert application.requested_amount.as_tuple().exponent == -2
        assert application.interest_rate.as_tuple().exponent == -4
    for value in [c.appraised_value for c in collateral if c.appraised_value is not None]:
        assert value.as_tuple().exponent == -2
    assert any(lien.outstanding_balance % 1 != 0 for lien in liens)


def test_money_is_stored_as_scaled_integers(medium_engine: Engine) -> None:
    with medium_engine.connect() as conn:
        typeof = conn.execute(
            text(
                "SELECT DISTINCT typeof(requested_amount), typeof(interest_rate) "
                "FROM loan_application"
            )
        ).all()
        lien_types = conn.execute(text("SELECT DISTINCT typeof(outstanding_balance) FROM lien")).all()

    assert typeof == [("integer", "integer")]
    assert lien_types == [("integer",)]


# --- CLI safety ---------------------------------------------------------------------------


@pytest.fixture
def dev_db(tmp_path: Path) -> Path:
    return tmp_path / "dev" / "loan_lab_dev.db"


def run_cli(dev_db: Path, *extra: str) -> int:
    return main(["--preset", "small", "--database", str(dev_db), *extra])


def dump_file(path: Path) -> dict[str, list[tuple[object, ...]]]:
    engine = sqlite_engine(path)
    try:
        return dump(engine)
    finally:
        engine.dispose()


class _FakeTerminal:
    def isatty(self) -> bool:
        return True


def test_cli_seeds_and_reports_counts(dev_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(dev_db) == 0

    output = capsys.readouterr().out
    for name, count in EXPECTED_SMALL_COUNTS:
        label = LABELS[name]
        assert any(line.split() == [*label.split(), str(count)] for line in output.splitlines())


def test_cli_refuses_populated_database_without_reset(
    dev_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(dev_db) == 0
    before = dump_file(dev_db)

    assert run_cli(dev_db, "--seed", "1") == 1
    assert "--reset" in capsys.readouterr().err
    assert dump_file(dev_db) == before


def test_cli_reset_requires_matching_confirmation(dev_db: Path) -> None:
    assert run_cli(dev_db) == 0
    before = dump_file(dev_db)

    assert run_cli(dev_db, "--seed", "1", "--reset", "--confirm-reset", "wrong.db") == 1
    assert dump_file(dev_db) == before


def test_cli_reset_refuses_without_confirmation_when_not_interactive(
    dev_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_cli(dev_db) == 0
    before = dump_file(dev_db)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("must not prompt"))

    assert run_cli(dev_db, "--seed", "1", "--reset") == 1
    assert dump_file(dev_db) == before


@pytest.mark.parametrize(("answer", "exit_code"), [("loan_lab_dev.db", 0), ("yes", 1)])
def test_cli_reset_interactive_confirmation(
    dev_db: Path, monkeypatch: pytest.MonkeyPatch, answer: str, exit_code: int
) -> None:
    assert run_cli(dev_db) == 0
    before = dump_file(dev_db)
    monkeypatch.setattr(sys, "stdin", _FakeTerminal())
    monkeypatch.setattr("builtins.input", lambda _: answer)

    assert run_cli(dev_db, "--seed", "1", "--reset") == exit_code
    assert (dump_file(dev_db) == before) is (exit_code == 1)


def test_cli_confirmed_reset_regenerates_identical_data(dev_db: Path) -> None:
    assert run_cli(dev_db) == 0
    before = dump_file(dev_db)

    assert run_cli(dev_db, "--reset", "--confirm-reset", dev_db.name) == 0
    assert dump_file(dev_db) == before


def test_cli_reset_on_empty_database_needs_no_confirmation(dev_db: Path) -> None:
    assert run_cli(dev_db, "--reset") == 0
    assert len(dump_file(dev_db)["applications"]) == SMALL


def test_cli_seeds_existing_empty_schema_without_reset(dev_db: Path) -> None:
    dev_db.parent.mkdir(parents=True)
    engine = sqlite_engine(dev_db)
    Base.metadata.create_all(engine)
    engine.dispose()

    assert run_cli(dev_db) == 0
    assert len(dump_file(dev_db)["applications"]) == SMALL


def test_cli_refuses_database_with_foreign_tables(dev_db: Path) -> None:
    dev_db.parent.mkdir(parents=True)
    engine = sqlite_engine(dev_db)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE something_else (id INTEGER PRIMARY KEY)"))

    assert run_cli(dev_db, "--reset", "--confirm-reset", dev_db.name) == 1
    with engine.connect() as conn:
        tables = conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'")).all()
    engine.dispose()
    assert tables == [("something_else",)]


def test_cli_confirm_reset_requires_reset(dev_db: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        run_cli(dev_db, "--confirm-reset", dev_db.name)
    assert excinfo.value.code == 2


# --- Default database path ----------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]


def make_fake_project(root: Path, name: str = cli.PROJECT_NAME) -> Path:
    """Create a minimal project tree and return the path a cli.py inside it would have."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n', encoding="utf-8")
    return root / "src" / "loan_lab" / "synthetic" / "cli.py"


def test_project_root_is_found_from_installed_package() -> None:
    assert find_project_root(Path(cli.__file__)) == REPO_ROOT
    assert cli.default_database_path() == REPO_ROOT / "data" / "loan_lab_dev.db"


def test_project_root_skips_other_pyproject_files(tmp_path: Path) -> None:
    make_fake_project(tmp_path / "lab")
    nested = make_fake_project(tmp_path / "lab" / "vendor" / "other", name="some-other-project")

    assert find_project_root(nested) == tmp_path / "lab"


def test_project_root_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        find_project_root(tmp_path / "nowhere" / "cli.py")


def test_default_path_does_not_depend_on_working_directory(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from loan_lab.synthetic.cli import default_database_path; "
            "print(default_database_path())",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )

    assert Path(result.stdout.strip()) == REPO_ROOT / "data" / "loan_lab_dev.db"


def test_cli_default_database_resolves_under_project_root_from_other_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    monkeypatch.setattr(cli, "__file__", str(make_fake_project(project)))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert main(["--preset", "small"]) == 0
    expected = project / "data" / "loan_lab_dev.db"
    assert expected.is_file()
    assert list(elsewhere.iterdir()) == []
    assert str(expected) in capsys.readouterr().out

    # Reset protections apply to the resolved default path too.
    assert main(["--preset", "small"]) == 1
    assert main(["--preset", "small", "--reset", "--confirm-reset", "wrong.db"]) == 1
    assert len(dump_file(expected)["applications"]) == SMALL


def test_cli_without_project_root_requires_explicit_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "__file__", str(tmp_path / "site-packages" / "loan_lab" / "cli.py"))

    with pytest.raises(SystemExit) as excinfo:
        main(["--preset", "small"])
    assert excinfo.value.code == 2


def test_cli_subprocess_respects_explicit_relative_database(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable, "-m", "loan_lab.synthetic", "--preset", "small",
            "--database", str(Path("relative") / "scratch.db"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    database = tmp_path / "relative" / "scratch.db"
    assert database.is_file()
    assert len(dump_file(database)["applications"]) == SMALL


def test_seed_database_refuses_populated_tables(small_engine: Engine) -> None:
    with pytest.raises(ValueError, match="already contain data"):
        seed_database(small_engine, SMALL)


# --- FastAPI isolation --------------------------------------------------------------------


def test_fastapi_startup_does_not_seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}

    assert list(tmp_path.iterdir()) == []


def test_fastapi_app_does_not_import_generator() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, loan_lab.main; print('loan_lab.synthetic' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False"
